#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

METADATA_RE = re.compile(r"^#\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*:\s*(.+)\s*$")
SUPPRESSION_ID_RE = re.compile(r"CVE-\d{4}-\d{4,}|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}")
REQUIRED_FIELDS = ("owner", "expires")


SuppressionEntry = tuple[str, int, dict[str, str]]


def _parse_entries(path: Path) -> list[SuppressionEntry]:
    entries: list[SuppressionEntry] = []
    comment_block: list[str] = []

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            comment_block = []
            continue
        if stripped.startswith("#"):
            comment_block.append(stripped)
            continue

        metadata: dict[str, str] = {}
        for comment in comment_block:
            if match := METADATA_RE.match(comment):
                metadata[match.group(1).lower()] = match.group(2).strip()

        entries.append((stripped, line_number, metadata))
        comment_block = []

    return entries


def _validate_entry(entry: SuppressionEntry, *, today: date) -> list[str]:
    identifier, line, metadata = entry
    errors: list[str] = []
    if not SUPPRESSION_ID_RE.fullmatch(identifier):
        errors.append(
            f"line {line}: unsupported suppression ID format ({identifier!r}); "
            "expected CVE-YYYY-NNNN or GHSA-xxxx-xxxx-xxxx"
        )

    for field in REQUIRED_FIELDS:
        if field not in metadata:
            errors.append(
                f"line {line}: suppression {identifier} is missing required metadata "
                f"`{field}` (comment format: '# {field}: ...')"
            )

    expires_raw = metadata.get("expires")
    if expires_raw is None:
        return errors

    try:
        expires_on = date.fromisoformat(expires_raw)
    except ValueError:
        errors.append(
            f"line {line}: suppression {identifier} has invalid expires date "
            f"{expires_raw!r}; expected YYYY-MM-DD"
        )
        return errors

    if expires_on < today:
        errors.append(
            f"line {line}: suppression {identifier} "
            f"expired on {expires_on.isoformat()} "
            f"(today is {today.isoformat()})"
        )
    return errors


def _validate_trivyignore(path: Path, *, today: date | None = None) -> list[str]:
    now = today or datetime.now(UTC).date()
    entries = _parse_entries(path)

    if not entries:
        return [f"{path}: no suppression entries found"]

    errors: list[str] = []
    for entry in entries:
        errors.extend(f"{path}: {error}" for error in _validate_entry(entry, today=now))
    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    trivyignore_path = repo_root / ".trivyignore"
    errors = _validate_trivyignore(trivyignore_path)
    if errors:
        print("Trivy suppression validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("Trivy suppression validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
