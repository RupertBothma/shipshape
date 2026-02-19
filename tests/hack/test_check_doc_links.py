from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module() -> object:
    module_path = Path(__file__).resolve().parents[2] / "hack" / "check_doc_links.py"
    spec = importlib.util.spec_from_file_location("check_doc_links", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load check_doc_links module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operations_anchors_returns_none_when_operations_doc_missing(tmp_path: Path) -> None:
    module = _load_module()
    missing_path = tmp_path / "docs" / "operations.md"
    actual = module._operations_anchors(missing_path)  # type: ignore[attr-defined]
    assert actual is None


def test_validate_url_skips_operations_anchor_checks_when_doc_missing() -> None:
    module = _load_module()
    validate = module._validate_url  # type: ignore[attr-defined]

    reason = validate("docs/operations.md#runbook-foo", operations_anchors=None)
    assert reason is None

    reason = validate(
        "https://github.com/<your-org>/shipshape/blob/main/docs/operations.md#runbook-foo",
        operations_anchors=None,
    )
    assert reason is None


def test_collectors_return_empty_when_optional_files_missing(tmp_path: Path) -> None:
    module = _load_module()

    assert module._collect_security_links(tmp_path / "SECURITY.md") == []  # type: ignore[attr-defined]
    issue_template = tmp_path / ".github" / "ISSUE_TEMPLATE" / "config.yml"
    assert module._collect_issue_template_links(issue_template) == []  # type: ignore[attr-defined]
    runbook_path = tmp_path / "k8s" / "monitoring" / "prometheusrule.yaml"
    assert module._collect_runbook_urls(runbook_path) == []  # type: ignore[attr-defined]
