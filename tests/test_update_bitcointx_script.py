from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    script_path = REPO_ROOT / "scripts" / "update-bitcointx.py"
    spec = importlib.util.spec_from_file_location("update_bitcointx", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release_payload(
    version: str = "2.1.1",
    digest: str = "b" * 64,
) -> dict[str, object]:
    tag = f"python-bitcointx-v{version}"
    name = f"python_bitcointx-{version}-py3-none-any.whl"
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    "https://github.com/m0wer/python-bitcointx/releases/download/"
                    f"{tag}/{name}"
                ),
                "digest": f"sha256:{digest}",
            }
        ],
    }


def _requirement_url(version: str, digest: str) -> str:
    return (
        "https://github.com/m0wer/python-bitcointx/releases/download/"
        f"python-bitcointx-v{version}/"
        f"python_bitcointx-{version}-py3-none-any.whl#sha256={digest}"
    )


def _source_url(commit: str, digest: str) -> str:
    return (
        "https://codeload.github.com/m0wer/python-bitcointx/tar.gz/"
        f"{commit}#sha256={digest}"
    )


def _write_source_tree(
    root: Path,
    module: Any,
    version: str,
    digest: str,
    commit: str = "",
) -> None:
    requirement = (
        _source_url(commit, digest) if commit else _requirement_url(version, digest)
    )
    for relative_path in module.DIRECT_PIN_FILES:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"python-bitcointx @ {requirement}\n", encoding="utf-8")

    test_path = root / module.SECURITY_TEST_PATH
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        f'BITCOINTX_VERSION = "{version}"\n'
        f'BITCOINTX_SOURCE_COMMIT = "{commit}"\n'
        f'BITCOINTX_SHA256 = (\n    "{digest}"\n)\n',
        encoding="utf-8",
    )


def test_parse_release_accepts_expected_wheel_and_digest() -> None:
    module = _load_module()

    pin = module.parse_release(_release_payload())

    assert pin.version == "2.1.1"
    assert pin.sha256 == "b" * 64
    assert pin.wheel_url.endswith("python_bitcointx-2.1.1-py3-none-any.whl")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(tag_name="v2.1.1"),
        lambda payload: payload.update(prerelease=True),
        lambda payload: payload["assets"].clear(),
        lambda payload: payload["assets"].append(payload["assets"][0].copy()),
        lambda payload: payload["assets"][0].update(digest="sha256:not-a-digest"),
        lambda payload: payload["assets"][0].update(
            browser_download_url="https://example.com/python_bitcointx.whl"
        ),
    ],
)
def test_parse_release_rejects_untrusted_release_data(mutation) -> None:
    module = _load_module()
    payload = _release_payload()
    mutation(payload)

    with pytest.raises(module.UpdateError):
        module.parse_release(payload)


def test_update_sources_updates_every_allowlisted_pin_and_then_is_a_noop(
    tmp_path: Path,
) -> None:
    module = _load_module()
    old_digest = "a" * 64
    new_digest = "b" * 64
    _write_source_tree(tmp_path, module, "2.1.0", old_digest)
    latest = module.parse_release(_release_payload(digest=new_digest))

    result = module.update_sources(tmp_path, latest)

    assert result.current_version == "2.1.0"
    assert len(result.changed_paths) == len(module.DIRECT_PIN_FILES) + 1
    for relative_path in module.DIRECT_PIN_FILES:
        assert latest.requirement_url in (tmp_path / relative_path).read_text(
            encoding="utf-8"
        )
    test_text = (tmp_path / module.SECURITY_TEST_PATH).read_text(encoding="utf-8")
    assert 'BITCOINTX_VERSION = "2.1.1"' in test_text
    assert f'    "{new_digest}"' in test_text

    noop_result = module.update_sources(tmp_path, latest)
    assert noop_result.current_version == "2.1.1"
    assert noop_result.changed_paths == ()


@pytest.mark.parametrize("release_version", ["2.1.1", "2.0.9"])
def test_update_sources_never_downgrades_source_pin_to_older_or_equal_release(
    tmp_path: Path, release_version: str
) -> None:
    """The source pin exists because the same-version wheel lacks MuSig2, so the
    updater must not replace it with an equal or older published release."""
    module = _load_module()
    commit = "d" * 40
    _write_source_tree(
        tmp_path, module, module.SOURCE_PIN_VERSION, "c" * 64, commit=commit
    )
    before = {
        path: path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    latest = module.parse_release(_release_payload(version=release_version))

    result = module.update_sources(tmp_path, latest)

    assert result.changed_paths == ()
    assert result.current_version == module.SOURCE_PIN_VERSION
    assert result.source_commit == commit
    assert before == {
        path: path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }


def test_update_sources_replaces_source_pin_with_newer_release(tmp_path: Path) -> None:
    module = _load_module()
    new_digest = "b" * 64
    _write_source_tree(
        tmp_path, module, module.SOURCE_PIN_VERSION, "c" * 64, commit="d" * 40
    )
    latest = module.parse_release(_release_payload(version="2.2.0", digest=new_digest))

    result = module.update_sources(tmp_path, latest)

    assert result.current_version == module.SOURCE_PIN_VERSION
    assert len(result.changed_paths) == len(module.DIRECT_PIN_FILES) + 1
    for relative_path in module.DIRECT_PIN_FILES:
        text = (tmp_path / relative_path).read_text(encoding="utf-8")
        assert text.strip() == f"python-bitcointx @ {latest.requirement_url}"
    test_text = (tmp_path / module.SECURITY_TEST_PATH).read_text(encoding="utf-8")
    assert 'BITCOINTX_VERSION = "2.2.0"' in test_text
    assert 'BITCOINTX_SOURCE_COMMIT = ""' in test_text
    assert f'    "{new_digest}"' in test_text


def test_update_sources_rejects_source_pin_inconsistent_with_security_test(
    tmp_path: Path,
) -> None:
    module = _load_module()
    _write_source_tree(
        tmp_path, module, module.SOURCE_PIN_VERSION, "c" * 64, commit="d" * 40
    )
    test_path = tmp_path / module.SECURITY_TEST_PATH
    test_path.write_text(
        test_path.read_text(encoding="utf-8").replace("d" * 40, "e" * 40),
        encoding="utf-8",
    )

    with pytest.raises(module.UpdateError, match="inconsistent"):
        module.update_sources(
            tmp_path, module.parse_release(_release_payload(version="2.2.0"))
        )


def test_update_sources_rejects_inconsistent_existing_pins_without_writes(
    tmp_path: Path,
) -> None:
    module = _load_module()
    old_digest = "a" * 64
    _write_source_tree(tmp_path, module, "2.1.0", old_digest)
    inconsistent_path = tmp_path / module.DIRECT_PIN_FILES[0]
    inconsistent_path.write_text(
        inconsistent_path.read_text(encoding="utf-8").replace("2.1.0", "2.0.0"),
        encoding="utf-8",
    )
    before = {
        path: path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(module.UpdateError, match="inconsistent"):
        module.update_sources(tmp_path, module.parse_release(_release_payload()))

    assert before == {
        path: path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }


def test_update_sources_validates_all_files_before_writing(tmp_path: Path) -> None:
    module = _load_module()
    _write_source_tree(tmp_path, module, "2.1.0", "a" * 64)
    invalid_path = tmp_path / module.DIRECT_PIN_FILES[-1]
    invalid_path.write_text("missing pin\n", encoding="utf-8")
    before = {
        path: path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(module.UpdateError, match="found 0"):
        module.update_sources(tmp_path, module.parse_release(_release_payload()))

    assert before == {
        path: path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }


def test_checked_in_sources_expose_exactly_one_recognizable_pin() -> None:
    """The updater silently stops working if formatting or a rename hides a pin,
    so the real repository files must stay recognizable to its patterns."""
    module = _load_module()

    for relative_path in module.DIRECT_PIN_FILES:
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        found = len(list(module.PIN_RE.finditer(text))) + len(
            list(module.SOURCE_PIN_RE.finditer(text))
        )
        assert found == 1, relative_path

    security_test = (REPO_ROOT / module.SECURITY_TEST_PATH).read_text(encoding="utf-8")
    for pattern in (
        module.TEST_VERSION_RE,
        module.TEST_DIGEST_RE,
        module.TEST_SOURCE_COMMIT_RE,
    ):
        assert len(list(pattern.finditer(security_test))) == 1, pattern.pattern


def test_update_deps_runs_bitcointx_updater_before_production_compile() -> None:
    script = (REPO_ROOT / "scripts" / "update-deps.sh").read_text(encoding="utf-8")

    invocation = 'run_python "$BITCOINTX_UPDATER"'
    assert script.count(invocation) == 1
    production_block = script.index('if [ "$UPDATE_PROD" = true ]; then')
    compile_call = script.index("run_pip_compile", production_block)
    assert production_block < script.index(invocation) < compile_call
