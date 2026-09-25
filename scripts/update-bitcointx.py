#!/usr/bin/env python3
"""Update the maintained python-bitcointx release pin in source files."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.request import Request, urlopen


REPOSITORY = "m0wer/python-bitcointx"
RELEASE_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
USER_AGENT = "joinmarket-ng-dependency-updater/1.0"
VERSION_PATTERN = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
DIRECT_PIN_FILES = (
    Path("jmcore/pyproject.toml"),
    Path("jmwallet/pyproject.toml"),
    Path("jmwalletd/pyproject.toml"),
    Path("scripts/derive_bond_pubkey.py"),
    Path("scripts/sign_bond_cert_reference.py"),
    Path("scripts/sign_bond_mnemonic.py"),
)
SECURITY_TEST_PATH = Path("tests/test_security_workflows.py")
PIN_RE = re.compile(
    rf"https://github\.com/{REPOSITORY}/releases/download/"
    rf"python-bitcointx-v(?P<tag_version>{VERSION_PATTERN})/"
    rf"python_bitcointx-(?P<wheel_version>{VERSION_PATTERN})-py3-none-any\.whl"
    r"#sha256=(?P<digest>[a-f0-9]{64})"
)
# Temporary source pin for https://github.com/m0wer/python-bitcointx/pull/1, used
# while the published wheel of SOURCE_PIN_VERSION lacks MuSig2 support. The sdist
# behind the pin declares SOURCE_PIN_VERSION, so only a strictly newer published
# release may replace it.
SOURCE_PIN_VERSION = "2.1.1"
SOURCE_PIN_RE = re.compile(
    rf"https://codeload\.github\.com/{REPOSITORY}/tar\.gz/(?P<commit>[a-f0-9]{{40}})"
    r"#sha256=(?P<source_digest>[a-f0-9]{64})"
)
TEST_VERSION_RE = re.compile(
    rf'(?m)^(BITCOINTX_VERSION = ")(?P<version>{VERSION_PATTERN})(")$'
)
TEST_DIGEST_RE = re.compile(
    r'(?m)^(BITCOINTX_SHA256 = (?:\(\n    )?")'
    r'(?P<digest>[a-f0-9]{64})("(?:\n\))?)$'
)
TEST_SOURCE_COMMIT_RE = re.compile(
    r'(?m)^(BITCOINTX_SOURCE_COMMIT = ")(?P<commit>[a-f0-9]{40}|)(")$'
)


class UpdateError(RuntimeError):
    pass


class ReleasePin(NamedTuple):
    version: str
    wheel_url: str
    sha256: str

    @property
    def requirement_url(self) -> str:
        return f"{self.wheel_url}#sha256={self.sha256}"


class CurrentPin(NamedTuple):
    version: str
    sha256: str
    source_commit: str  # empty for release wheel pins


class UpdateResult(NamedTuple):
    current_version: str
    changed_paths: tuple[Path, ...]
    source_commit: str = ""


def fetch_latest_release() -> dict[str, object]:
    request = Request(
        RELEASE_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urlopen(request, timeout=120) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise UpdateError("GitHub latest release response is not an object")
    return payload


def parse_release(payload: dict[str, object]) -> ReleasePin:
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise UpdateError(
            "Latest python-bitcointx release must be stable and published"
        )

    tag = payload.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("Latest python-bitcointx release has no tag")
    tag_match = re.fullmatch(rf"python-bitcointx-v(?P<version>{VERSION_PATTERN})", tag)
    if tag_match is None:
        raise UpdateError(f"Unexpected python-bitcointx release tag: {tag}")
    version = tag_match.group("version")

    expected_name = f"python_bitcointx-{version}-py3-none-any.whl"
    expected_url = (
        f"https://github.com/{REPOSITORY}/releases/download/{tag}/{expected_name}"
    )
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("Latest python-bitcointx release has no assets array")
    matching_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("name") == expected_name
    ]
    if len(matching_assets) != 1:
        raise UpdateError(
            f"Expected one {expected_name} release asset, found {len(matching_assets)}"
        )

    asset = matching_assets[0]
    wheel_url = asset.get("browser_download_url")
    if wheel_url != expected_url:
        raise UpdateError(f"Unexpected python-bitcointx wheel URL: {wheel_url}")
    digest = asset.get("digest")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", digest) is None
    ):
        raise UpdateError("python-bitcointx wheel asset has no valid SHA-256 digest")
    return ReleasePin(version, expected_url, digest.removeprefix("sha256:"))


def _one_match(pattern: re.Pattern[str], text: str, path: Path) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise UpdateError(
            f"Expected one python-bitcointx pin in {path}, found {len(matches)}"
        )
    return matches[0]


def _current_pin(text: str, path: Path) -> tuple[CurrentPin, tuple[int, int]]:
    """Return the single python-bitcointx pin in ``text`` and its span."""
    wheel_matches = list(PIN_RE.finditer(text))
    source_matches = list(SOURCE_PIN_RE.finditer(text))
    if len(wheel_matches) + len(source_matches) != 1:
        raise UpdateError(
            f"Expected one python-bitcointx pin in {path}, "
            f"found {len(wheel_matches) + len(source_matches)}"
        )
    if wheel_matches:
        match = wheel_matches[0]
        if match.group("tag_version") != match.group("wheel_version"):
            raise UpdateError(f"Inconsistent python-bitcointx versions in {path}")
        wheel_pin = CurrentPin(match.group("tag_version"), match.group("digest"), "")
        return wheel_pin, match.span()
    match = source_matches[0]
    pin = CurrentPin(
        SOURCE_PIN_VERSION, match.group("source_digest"), match.group("commit")
    )
    return pin, match.span()


def _is_newer(candidate: str, current: str) -> bool:
    return tuple(int(part) for part in candidate.split(".")) > tuple(
        int(part) for part in current.split(".")
    )


def _atomic_write(path: Path, text: str) -> None:
    mode = path.stat().st_mode
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temporary.write(text)
            temporary_path = Path(temporary.name)
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def update_sources(repo_root: Path, latest: ReleasePin) -> UpdateResult:
    prepared: dict[Path, str] = {}
    current_pins: list[CurrentPin] = []

    for relative_path in DIRECT_PIN_FILES:
        path = repo_root / relative_path
        text = path.read_text(encoding="utf-8")
        pin, (start, end) = _current_pin(text, relative_path)
        current_pins.append(pin)
        prepared[path] = text[:start] + latest.requirement_url + text[end:]

    test_path = repo_root / SECURITY_TEST_PATH
    test_text = test_path.read_text(encoding="utf-8")
    version_match = _one_match(TEST_VERSION_RE, test_text, SECURITY_TEST_PATH)
    digest_match = _one_match(TEST_DIGEST_RE, test_text, SECURITY_TEST_PATH)
    commit_match = _one_match(TEST_SOURCE_COMMIT_RE, test_text, SECURITY_TEST_PATH)
    current_pins.append(
        CurrentPin(
            version_match.group("version"),
            digest_match.group("digest"),
            commit_match.group("commit"),
        )
    )
    updated_test_text = TEST_VERSION_RE.sub(
        rf"\g<1>{latest.version}\g<3>", test_text, count=1
    )
    updated_test_text = TEST_DIGEST_RE.sub(
        rf"\g<1>{latest.sha256}\g<3>", updated_test_text, count=1
    )
    # Moving to a release wheel clears the temporary source revision.
    prepared[test_path] = TEST_SOURCE_COMMIT_RE.sub(
        r"\g<1>\g<3>", updated_test_text, count=1
    )

    if len(set(current_pins)) != 1:
        raise UpdateError("Existing python-bitcointx source pins are inconsistent")
    current = current_pins[0]
    current_version = current.version

    if current.source_commit and not _is_newer(latest.version, current_version):
        # The source pin carries fixes the published wheel of the same version
        # lacks, so it is never replaced by an equal or older release.
        return UpdateResult(current_version, (), current.source_commit)

    changed_paths = tuple(
        path
        for path, updated_text in prepared.items()
        if path.read_text(encoding="utf-8") != updated_text
    )
    for path in changed_paths:
        _atomic_write(path, prepared[path])
    return UpdateResult(current_version, changed_paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    try:
        latest = parse_release(fetch_latest_release())
        result = update_sources(args.repo_root.resolve(), latest)
    except (OSError, UpdateError, json.JSONDecodeError) as error:
        print(f"Error: {error}")
        return 1

    if result.changed_paths:
        print(
            f"Updated python-bitcointx {result.current_version} -> {latest.version} "
            f"in {len(result.changed_paths)} source files"
        )
    elif result.source_commit:
        print(
            f"python-bitcointx stays pinned to source {result.source_commit[:12]} "
            f"({result.current_version}); release {latest.version} is not newer"
        )
    else:
        print(f"python-bitcointx is up to date ({latest.version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
