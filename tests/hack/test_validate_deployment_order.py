from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module() -> object:
    module_path = Path(__file__).resolve().parents[2] / "hack" / "validate_deployment_order.py"
    spec = importlib.util.spec_from_file_location("validate_deployment_order", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load validate_deployment_order module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_operations_order_reads_canonical_block() -> None:
    module = _load_module()
    text = """# Operations
### Deployment Order Drift Check
```bash
cat <<'EOF'
k8s/namespace
k8s/istio-ingress
k8s/overlays/test
k8s/overlays/prod
k8s/monitoring
k8s/controller
EOF
```
"""

    actual = module._parse_operations_order(text)  # type: ignore[attr-defined]
    assert actual == list(module.EXPECTED_ORDER)  # type: ignore[attr-defined]


def test_load_operations_order_returns_none_when_operations_doc_missing(tmp_path: Path) -> None:
    module = _load_module()
    missing_path = tmp_path / "docs" / "operations.md"
    actual = module._load_operations_order(missing_path)  # type: ignore[attr-defined]
    assert actual is None


def test_release_source_normalization_handles_tmp_kustomizations() -> None:
    module = _load_module()

    normalize = module._normalize_release_source  # type: ignore[attr-defined]
    assert normalize("${TMP_K8S}/overlays/test") == "k8s/overlays/test"
    assert normalize("${TMP_K8S}/overlays/prod") == "k8s/overlays/prod"
    assert normalize("${TMP_K8S}/controller") == "k8s/controller"
    assert normalize("k8s/namespace") == "k8s/namespace"
    assert normalize("k8s/unknown") is None


def test_parse_release_apply_order_rejects_malformed_lines() -> None:
    module = _load_module()
    text = """cat > "${BUNDLE_DIR}/apply-order.txt" <<'EOF'
1. kubectl apply -f 00-namespace.yaml
bad-line
EOF
"""
    with pytest.raises(ValueError, match="Malformed apply-order line"):
        module._parse_release_apply_order(text)  # type: ignore[attr-defined]
