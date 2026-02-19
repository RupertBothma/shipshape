from __future__ import annotations

import importlib.util
from pathlib import Path

EKS_PATCH_VALIDATE_CMD = (
    "python3 hack/validate_manifests.py --overlay test --overlay prod "
    "--controller-egress-patch examples/controller-egress/eks.patch.yaml"
)
GKE_PATCH_VALIDATE_CMD = (
    "python3 hack/validate_manifests.py --overlay test --overlay prod "
    "--controller-egress-patch examples/controller-egress/gke.patch.yaml"
)
PROVIDER_PATCH_VALIDATE_CMD = (
    "python3 hack/validate_manifests.py --overlay test --overlay prod "
    "--controller-egress-patch examples/controller-egress/<provider>.patch.yaml"
)


def _load_module() -> object:
    module_path = Path(__file__).resolve().parents[2] / "hack" / "validate_production_evidence.py"
    spec = importlib.util.spec_from_file_location("validate_production_evidence", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load validate_production_evidence module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_capacity(path: Path, status: str) -> None:
    path.write_text(
        "\n".join(
            [
                "# Capacity Baselines",
                "",
                f"Current gate status (2026-02-09): `{status}`.",
            ]
        ),
        encoding="utf-8",
    )


def _write_drill(path: Path, status: str) -> None:
    path.write_text(
        "\n".join(
            [
                "# Disaster Recovery Drill Report",
                "",
                "## Status",
                f"`{status}`",
            ]
        ),
        encoding="utf-8",
    )


def _write_security(path: Path, table_rows: list[str]) -> None:
    body = [
        "# Security Controls Validation Evidence",
        "",
        "## Validation Matrix",
        "",
        "| Date (UTC) | Environment | Cluster | Encryption-at-rest check | "
        "Audit-log sink check | Result | Evidence link(s) | Operator |",
        "|---|---|---|---|---|---|---|---|",
    ]
    body.extend(table_rows)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def _write_egress(path: Path, status: str, table_rows: list[str]) -> None:
    body = [
        "# Controller API Egress Handoff Evidence",
        "",
        "## Current Status",
        f"`{status}`",
        "",
        "## Validation Matrix",
        "",
        "| Date (UTC) | Environment | Cluster | Applied patch file | API endpoint source | "
        "Verified API CIDRs | Render/validation command | Smoke check result | Reviewer |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    body.extend(table_rows)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def test_validate_production_evidence_rejects_blocked_artifacts(tmp_path: Path) -> None:
    module = _load_module()
    artifacts_dir = tmp_path / "docs" / "operations-artifacts"
    artifacts_dir.mkdir(parents=True)

    _write_capacity(artifacts_dir / "capacity-baselines.md", "BLOCKED")
    _write_drill(artifacts_dir / "dr-drill-20260209.md", "PENDING_EXECUTION")
    _write_security(
        artifacts_dir / "security-controls-validation.md",
        [
            "| 2026-02-09 | test | `<cluster-name>` | "
            "`PENDING_PLATFORM_VALIDATION` | `PENDING_PLATFORM_VALIDATION` | "
            "`BLOCKED` | `<ticket-or-doc-link>` | `<name>` |",
            "| 2026-02-09 | prod | `<cluster-name>` | "
            "`PENDING_PLATFORM_VALIDATION` | `PENDING_PLATFORM_VALIDATION` | "
            "`BLOCKED` | `<ticket-or-doc-link>` | `<name>` |",
        ],
    )
    _write_egress(
        artifacts_dir / "controller-egress-handoff.md",
        "PENDING_PLATFORM_VALIDATION",
        [
            "| 2026-02-09 | prod | `<managed-cluster-name>` | "
            "`examples/controller-egress/<provider>.patch.yaml` | "
            "`kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'` | "
            "`<cidr-1>, <cidr-2>` | "
            f"`{PROVIDER_PATCH_VALIDATE_CMD}` | "
            "`<PASS/BLOCKED>` | `<name>` |",
        ],
    )

    errors = module._validate_production_evidence(artifacts_dir)  # type: ignore[attr-defined]

    assert any("capacity-baselines.md" in error for error in errors)
    assert any("dr-drill-20260209.md" in error for error in errors)
    assert any("security-controls-validation.md" in error for error in errors)
    assert any("controller-egress-handoff.md" in error for error in errors)


def test_validate_production_evidence_accepts_completed_artifacts(tmp_path: Path) -> None:
    module = _load_module()
    artifacts_dir = tmp_path / "docs" / "operations-artifacts"
    artifacts_dir.mkdir(parents=True)

    _write_capacity(artifacts_dir / "capacity-baselines.md", "APPROVED")
    _write_drill(artifacts_dir / "dr-drill-20260101.md", "PENDING_EXECUTION")
    _write_drill(artifacts_dir / "dr-drill-20260209.md", "COMPLETED")
    _write_security(
        artifacts_dir / "security-controls-validation.md",
        [
            "| 2026-02-09 | test | shipshape-test | VERIFIED | VERIFIED | PASS | "
            "https://evidence.example/test | alice |",
            "| 2026-02-09 | prod | shipshape-prod | VERIFIED | VERIFIED | PASS | "
            "https://evidence.example/prod | bob |",
        ],
    )
    _write_egress(
        artifacts_dir / "controller-egress-handoff.md",
        "PASS",
        [
            "| 2026-02-09 | prod | shipshape-prod | examples/controller-egress/eks.patch.yaml | "
            "kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' | "
            "10.0.24.15/32,10.0.24.16/32 | "
            f"{EKS_PATCH_VALIDATE_CMD} | PASS | alice |",
        ],
    )

    errors = module._validate_production_evidence(artifacts_dir)  # type: ignore[attr-defined]

    assert errors == []


def test_validate_production_evidence_requires_drill_artifact(tmp_path: Path) -> None:
    module = _load_module()
    artifacts_dir = tmp_path / "docs" / "operations-artifacts"
    artifacts_dir.mkdir(parents=True)

    _write_capacity(artifacts_dir / "capacity-baselines.md", "APPROVED")
    _write_security(
        artifacts_dir / "security-controls-validation.md",
        [
            "| 2026-02-09 | test | shipshape-test | VERIFIED | VERIFIED | PASS | "
            "https://evidence.example/test | alice |",
        ],
    )
    _write_egress(
        artifacts_dir / "controller-egress-handoff.md",
        "PASS",
        [
            "| 2026-02-09 | prod | shipshape-prod | examples/controller-egress/eks.patch.yaml | "
            "kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' | "
            "10.0.24.15/32,10.0.24.16/32 | "
            f"{EKS_PATCH_VALIDATE_CMD} | PASS | alice |",
        ],
    )

    errors = module._validate_production_evidence(artifacts_dir)  # type: ignore[attr-defined]

    assert any("no drill artifact found" in error for error in errors)


def test_validate_production_evidence_requires_target_env_egress_row(tmp_path: Path) -> None:
    module = _load_module()
    artifacts_dir = tmp_path / "docs" / "operations-artifacts"
    artifacts_dir.mkdir(parents=True)

    _write_capacity(artifacts_dir / "capacity-baselines.md", "APPROVED")
    _write_drill(artifacts_dir / "dr-drill-20260209.md", "COMPLETED")
    _write_security(
        artifacts_dir / "security-controls-validation.md",
        [
            "| 2026-02-09 | prod | shipshape-prod | VERIFIED | VERIFIED | PASS | "
            "https://evidence.example/prod | alice |",
        ],
    )
    _write_egress(
        artifacts_dir / "controller-egress-handoff.md",
        "PASS",
        [
            "| 2026-02-09 | test | shipshape-test | examples/controller-egress/gke.patch.yaml | "
            "kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' | "
            "172.16.8.5/32,172.16.8.6/32 | "
            f"{GKE_PATCH_VALIDATE_CMD} | PASS | bob |",
        ],
    )

    errors = module._validate_production_evidence(artifacts_dir)  # type: ignore[attr-defined]
    assert any("missing validation matrix row for environment 'prod'" in error for error in errors)

    errors = module._validate_production_evidence(  # type: ignore[attr-defined]
        artifacts_dir, ["test"]
    )
    assert errors == []
