#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

APPROVED_STATUSES = {"APPROVED", "PASS", "PASSED", "READY", "COMPLETED"}
BLOCKED_MARKERS = {"BLOCKED", "PENDING", "PENDING_EXECUTION", "PENDING_PLATFORM_VALIDATION"}
DRILL_FILE_RE = re.compile(r"^dr-drill-(\d{8})\.md$")
PLACEHOLDER_RE = re.compile(r"<[^>]+>")


def _normalize_status(value: str) -> str:
    return value.strip().strip("`").upper()


def _contains_blocked_marker(value: str) -> bool:
    normalized = _normalize_status(value)
    return normalized in BLOCKED_MARKERS or normalized.startswith("PENDING")


def _extract_backticked_status(markdown: str, heading: str) -> str | None:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n+`([^`]+)`",
        re.MULTILINE,
    )
    match = pattern.search(markdown)
    if match is None:
        return None
    return match.group(1).strip()


def _latest_drill_report(artifacts_dir: Path) -> Path | None:
    dated_reports: list[tuple[int, Path]] = []
    for candidate in artifacts_dir.glob("dr-drill-*.md"):
        match = DRILL_FILE_RE.match(candidate.name)
        if match is not None:
            dated_reports.append((int(match.group(1)), candidate))
    if not dated_reports:
        return None
    return max(dated_reports, key=lambda entry: entry[0])[1]


def _validate_capacity_baseline(path: Path) -> list[str]:
    if not path.exists():
        return [f"{path}: missing file"]

    markdown = path.read_text(encoding="utf-8")
    match = re.search(r"Current gate status[^\n]*?`([^`]+)`", markdown)
    if match is None:
        return [f"{path}: missing `Current gate status` with backticked status value"]

    status = _normalize_status(match.group(1))
    if status not in APPROVED_STATUSES:
        return [
            f"{path}: gate status is {status!r}; strict production gate requires one of "
            f"{sorted(APPROVED_STATUSES)}"
        ]
    return []


def _validate_drill_report(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"{path}: missing file"]

    markdown = path.read_text(encoding="utf-8")
    status = _extract_backticked_status(markdown, "Status")
    if status is None:
        return [f"{path}: missing `## Status` section with backticked status value"]

    normalized = _normalize_status(status)
    if normalized != "COMPLETED":
        errors.append(
            f"{path}: drill status is {normalized!r}; strict production gate requires 'COMPLETED'"
        )
    return errors


def _extract_section_lines(markdown: str, heading: str) -> list[tuple[int, str]]:
    lines = markdown.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == f"## {heading}":
            start = index + 1
            break

    if start is None:
        return []

    section: list[tuple[int, str]] = []
    for index in range(start, len(lines)):
        raw_line = lines[index]
        if raw_line.startswith("## "):
            break
        section.append((index + 1, raw_line))
    return section


def _iter_table_rows(section_lines: Iterable[tuple[int, str]]) -> Iterable[tuple[int, list[str]]]:
    for line_no, raw_line in section_lines:
        stripped = raw_line.strip()
        if not stripped.startswith("|"):
            continue
        if stripped.startswith("|---"):
            continue

        cells = [cell.strip() for cell in stripped.split("|")[1:-1]]
        if not cells:
            continue
        first_cell = _normalize_status(cells[0])
        if first_cell.startswith("DATE"):
            continue
        yield line_no, cells


def _validate_security_controls(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"{path}: missing file"]

    markdown = path.read_text(encoding="utf-8")
    matrix_section = _extract_section_lines(markdown, "Validation Matrix")
    if not matrix_section:
        return [f"{path}: missing `## Validation Matrix` section"]

    data_rows = 0
    for line_no, cells in _iter_table_rows(matrix_section):
        data_rows += 1
        stripped = " | ".join(cells)
        if PLACEHOLDER_RE.search(stripped):
            errors.append(
                f"{path}: line {line_no} contains placeholder values; "
                "replace with environment evidence"
            )

        # Columns: Date, Environment, Cluster, Encryption check, Audit-log check,
        # Result, Evidence link(s), Operator.
        status_cells = cells[3:5] if len(cells) >= 5 else cells
        for status_cell in status_cells:
            if _contains_blocked_marker(status_cell):
                errors.append(
                    f"{path}: line {line_no} contains blocked/pending status value {status_cell!r}"
                )

        if len(cells) >= 6:
            result = _normalize_status(cells[5])
            if result not in APPROVED_STATUSES:
                errors.append(
                    f"{path}: line {line_no} result is {result!r}; expected one of "
                    f"{sorted(APPROVED_STATUSES)}"
                )

    if data_rows == 0:
        errors.append(f"{path}: validation matrix has no data rows")

    return errors


def _validate_controller_egress_handoff(path: Path, target_environments: list[str]) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"{path}: missing file"]

    markdown = path.read_text(encoding="utf-8")
    current_status = _extract_backticked_status(markdown, "Current Status")
    if current_status is None:
        errors.append(f"{path}: missing `## Current Status` section with backticked status value")
    else:
        normalized_status = _normalize_status(current_status)
        if (
            _contains_blocked_marker(normalized_status)
            or normalized_status not in APPROVED_STATUSES
        ):
            errors.append(
                f"{path}: current status is {normalized_status!r}; strict production gate "
                f"requires one of {sorted(APPROVED_STATUSES)}"
            )

    matrix_section = _extract_section_lines(markdown, "Validation Matrix")
    if not matrix_section:
        errors.append(f"{path}: missing `## Validation Matrix` section")
        return errors

    normalized_targets = [env.strip().lower() for env in target_environments if env.strip()]
    if not normalized_targets:
        errors.append(f"{path}: no target environments were provided")
        return errors

    latest_rows_by_environment: dict[str, tuple[int, list[str]]] = {}
    for line_no, cells in _iter_table_rows(matrix_section):
        if len(cells) < 9:
            errors.append(
                f"{path}: line {line_no} has {len(cells)} columns; expected 9 columns "
                "in validation matrix"
            )
            continue
        environment = cells[1].strip().lower()
        if not environment:
            errors.append(f"{path}: line {line_no} has empty environment column")
            continue
        latest_rows_by_environment[environment] = (line_no, cells)

    for target_environment in normalized_targets:
        row_entry = latest_rows_by_environment.get(target_environment)
        if row_entry is None:
            errors.append(
                f"{path}: missing validation matrix row for environment {target_environment!r}"
            )
            continue

        line_no, cells = row_entry
        row_joined = " | ".join(cells)
        if PLACEHOLDER_RE.search(row_joined):
            errors.append(
                f"{path}: line {line_no} contains placeholder values; "
                f"replace with {target_environment!r} environment evidence"
            )

        smoke_result = _normalize_status(cells[7])
        if _contains_blocked_marker(smoke_result) or smoke_result not in APPROVED_STATUSES:
            errors.append(
                f"{path}: line {line_no} smoke check result is {smoke_result!r}; expected one of "
                f"{sorted(APPROVED_STATUSES)}"
            )

        reviewer = cells[8].strip()
        if not reviewer or PLACEHOLDER_RE.search(reviewer):
            errors.append(
                f"{path}: line {line_no} reviewer is missing/placeholder for environment "
                f"{target_environment!r}"
            )

    return errors


def _validate_production_evidence(
    artifacts_dir: Path, target_environments: list[str] | None = None
) -> list[str]:
    errors: list[str] = []
    environments = target_environments if target_environments is not None else ["prod"]

    capacity_path = artifacts_dir / "capacity-baselines.md"
    errors.extend(_validate_capacity_baseline(capacity_path))

    latest_drill = _latest_drill_report(artifacts_dir)
    if latest_drill is None:
        errors.append(
            f"{artifacts_dir}: no drill artifact found matching dr-drill-YYYYMMDD.md"
        )
    else:
        errors.extend(_validate_drill_report(latest_drill))

    security_controls_path = artifacts_dir / "security-controls-validation.md"
    errors.extend(_validate_security_controls(security_controls_path))

    controller_egress_path = artifacts_dir / "controller-egress-handoff.md"
    errors.extend(_validate_controller_egress_handoff(controller_egress_path, environments))

    return errors


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate strict production operational evidence gates."
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        help="Path to docs/operations-artifacts (defaults to repo path)",
    )
    parser.add_argument(
        "--environment",
        action="append",
        help=(
            "Target environment to validate for controller egress handoff evidence. "
            "Can be provided multiple times. Defaults to 'prod'."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.artifacts_dir is None:
        repo_root = Path(__file__).resolve().parents[1]
        artifacts_dir = repo_root / "docs" / "operations-artifacts"
    else:
        artifacts_dir = args.artifacts_dir.resolve()

    target_environments = args.environment if args.environment is not None else ["prod"]
    errors = _validate_production_evidence(artifacts_dir, target_environments)
    if errors:
        print("Production evidence validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("Production evidence validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
