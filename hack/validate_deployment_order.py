#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

EXPECTED_ORDER: tuple[str, ...] = (
    "k8s/namespace",
    "k8s/istio-ingress",
    "k8s/overlays/test",
    "k8s/overlays/prod",
    "k8s/monitoring",
    "k8s/controller",
)

EXPECTED_RELEASE_BUNDLE: tuple[tuple[str, str], ...] = (
    ("k8s/namespace", "00-namespace.yaml"),
    ("k8s/istio-ingress", "10-istio-ingress.yaml"),
    ("k8s/overlays/test", "20-test-overlay.yaml"),
    ("k8s/overlays/prod", "30-prod-overlay-release.yaml"),
    ("k8s/monitoring", "40-monitoring.yaml"),
    ("k8s/controller", "50-controller-release.yaml"),
)

_OPS_BLOCK_RE = re.compile(
    r"### Deployment Order Drift Check.*?cat <<'EOF'\n(?P<body>.*?)\nEOF",
    re.DOTALL,
)
_RELEASE_BUILD_RE = re.compile(
    r'^\s*kustomize build (?P<source>"[^"]+"|[^ >]+) > "\$\{BUNDLE_DIR\}/(?P<bundle>[^"]+)"\s*$',
    re.MULTILINE,
)
_RELEASE_APPLY_BLOCK_RE = re.compile(
    r'cat > "\$\{BUNDLE_DIR\}/apply-order\.txt" <<\'EOF\'\n(?P<body>.*?)\n\s*EOF',
    re.DOTALL,
)
_RELEASE_APPLY_LINE_RE = re.compile(r"^\s*\d+\.\s+kubectl apply -f (?P<file>\S+)\s*$")

_SUFFIX_TO_CANONICAL: dict[str, str] = {
    "/overlays/test": "k8s/overlays/test",
    "/overlays/prod": "k8s/overlays/prod",
    "/controller": "k8s/controller",
}


def _normalize_release_source(source: str) -> str | None:
    source = source.strip().strip('"')
    if source in {"k8s/namespace", "k8s/istio-ingress", "k8s/monitoring"}:
        return source
    for suffix, canonical in _SUFFIX_TO_CANONICAL.items():
        if source.endswith(suffix):
            return canonical
    return None


def _parse_operations_order(text: str) -> list[str]:
    match = _OPS_BLOCK_RE.search(text)
    if match is None:
        raise ValueError("Missing deployment-order reference block in docs/operations.md")
    return [line.strip() for line in match.group("body").splitlines() if line.strip()]


def _load_operations_order(path: Path) -> list[str] | None:
    """Read the operations doc and return the deployment order, or None if missing."""
    if not path.exists():
        return None
    return _parse_operations_order(path.read_text(encoding="utf-8"))


def _parse_release_bundle_builds(release_text: str) -> list[tuple[str, str]]:
    return [
        (normalized, m.group("bundle").strip())
        for m in _RELEASE_BUILD_RE.finditer(release_text)
        if (normalized := _normalize_release_source(m.group("source").strip())) is not None
    ]


def _parse_release_apply_order(release_text: str) -> list[str]:
    match = _RELEASE_APPLY_BLOCK_RE.search(release_text)
    if match is None:
        raise ValueError("Missing apply-order.txt block in .github/workflows/release.yml")

    files: list[str] = []
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        apply_match = _RELEASE_APPLY_LINE_RE.match(stripped)
        if apply_match is None:
            raise ValueError(f"Malformed apply-order line: {stripped}")
        files.append(apply_match.group("file"))
    return files


def _check_drift(
    errors: list[str],
    label: str,
    expected: object,
    actual: object,
) -> None:
    if actual != expected:
        errors.append(
            f"{label} drifted.\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}"
        )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    operations_path = repo_root / "docs" / "operations.md"
    release_path = repo_root / ".github" / "workflows" / "release.yml"

    release_text = release_path.read_text(encoding="utf-8")

    errors: list[str] = []

    if operations_path.exists():
        operations_text = operations_path.read_text(encoding="utf-8")
        operations_order = _parse_operations_order(operations_text)
        _check_drift(
            errors,
            "docs/operations.md deployment-order block",
            list(EXPECTED_ORDER),
            operations_order,
        )

    _check_drift(
        errors,
        ".github/workflows/release.yml kustomize bundle order",
        list(EXPECTED_RELEASE_BUNDLE),
        _parse_release_bundle_builds(release_text),
    )

    _check_drift(
        errors,
        ".github/workflows/release.yml apply-order.txt",
        [bundle for _, bundle in EXPECTED_RELEASE_BUNDLE],
        _parse_release_apply_order(release_text),
    )

    if errors:
        print("Deployment order validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("Deployment order validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
