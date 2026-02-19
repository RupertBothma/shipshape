from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module() -> object:
    module_path = Path(__file__).resolve().parents[2] / "hack" / "validate_release_metadata.py"
    spec = importlib.util.spec_from_file_location("validate_release_metadata", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load validate_release_metadata module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_runtime_constant_version_reads_semver_assignment(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "runtime.py"
    source.write_text('APP_VERSION = "1.2.3"\n', encoding="utf-8")

    version = module._load_runtime_constant_version(source, "APP_VERSION")  # type: ignore[attr-defined]
    assert version == "1.2.3"


def test_load_runtime_constant_version_rejects_non_semver(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "runtime.py"
    source.write_text('APP_VERSION = "latest"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="must be semver"):
        module._load_runtime_constant_version(source, "APP_VERSION")  # type: ignore[attr-defined]


def test_runtime_constants_match_pyproject_version() -> None:
    module = _load_module()
    repo_root = Path(__file__).resolve().parents[2]
    pyproject_version = module._load_pyproject_version(repo_root / "pyproject.toml")  # type: ignore[attr-defined]

    for relative_path, constant_name in module.RUNTIME_VERSION_CONSTANTS:  # type: ignore[attr-defined]
        runtime_version = module._load_runtime_constant_version(  # type: ignore[attr-defined]
            repo_root / relative_path,
            constant_name,
        )
        assert runtime_version == pyproject_version
