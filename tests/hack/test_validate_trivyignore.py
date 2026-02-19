from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path


def _load_module() -> object:
    module_path = Path(__file__).resolve().parents[2] / "hack" / "validate_trivyignore.py"
    spec = importlib.util.spec_from_file_location("validate_trivyignore", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load validate_trivyignore module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validate_trivyignore_accepts_valid_metadata(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / ".trivyignore"
    path.write_text(
        "\n".join(
            [
                "# owner: platform-security",
                "# expires: 2026-03-01",
                "CVE-2026-0861",
            ]
        ),
        encoding="utf-8",
    )

    errors = module._validate_trivyignore(path, today=date(2026, 2, 8))  # type: ignore[attr-defined]
    assert errors == []


def test_validate_trivyignore_rejects_missing_owner(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / ".trivyignore"
    path.write_text(
        "\n".join(
            [
                "# expires: 2026-03-01",
                "CVE-2026-0861",
            ]
        ),
        encoding="utf-8",
    )

    errors = module._validate_trivyignore(path, today=date(2026, 2, 8))  # type: ignore[attr-defined]
    assert len(errors) == 1
    assert "missing required metadata `owner`" in errors[0]


def test_validate_trivyignore_rejects_expired_suppression(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / ".trivyignore"
    path.write_text(
        "\n".join(
            [
                "# owner: platform-security",
                "# expires: 2026-02-07",
                "CVE-2026-0861",
            ]
        ),
        encoding="utf-8",
    )

    errors = module._validate_trivyignore(path, today=date(2026, 2, 8))  # type: ignore[attr-defined]
    assert len(errors) == 1
    assert "expired on 2026-02-07" in errors[0]


def test_validate_trivyignore_rejects_unsupported_id_format(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / ".trivyignore"
    path.write_text(
        "\n".join(
            [
                "# owner: platform-security",
                "# expires: 2026-03-01",
                "NOT-A-CVE",
            ]
        ),
        encoding="utf-8",
    )

    errors = module._validate_trivyignore(path, today=date(2026, 2, 8))  # type: ignore[attr-defined]
    assert len(errors) == 1
    assert "unsupported suppression ID format" in errors[0]
