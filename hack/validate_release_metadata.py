#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
CHANGELOG_VERSION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")
RUNTIME_VERSION_CONSTANTS: tuple[tuple[str, str], ...] = (
    ("app/src/main.py", "APP_VERSION"),
    ("controller/src/__main__.py", "RUNTIME_VERSION"),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate that pyproject, changelog, runtime version constants, "
            "and release tag versions are consistent"
        )
    )
    parser.add_argument(
        "--tag",
        help=(
            "Tag to validate (e.g. v0.2.0 or refs/tags/v0.2.0). "
            "If omitted, uses GitHub tag env vars when available."
        ),
    )
    return parser.parse_args()


def _normalize_tag(raw_tag: str) -> str:
    tag = raw_tag.strip()
    if tag.startswith("refs/tags/"):
        tag = tag.removeprefix("refs/tags/")
    if tag.startswith("v"):
        tag = tag[1:]
    if not SEMVER_RE.match(tag):
        raise ValueError(f"Tag must be semver (vX.Y.Z), got: {raw_tag!r}")
    return tag


def _load_pyproject_version(pyproject_path: Path) -> str:
    in_project = False
    version: str | None = None

    for line in pyproject_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if not in_project:
            continue

        match = re.match(r'^version\s*=\s*"([^"]+)"$', stripped)
        if match:
            version = match.group(1).strip()
            break

    if not version:
        raise ValueError("Missing project.version in [project] table of pyproject.toml")
    if not SEMVER_RE.match(version):
        raise ValueError(f"project.version must be semver (X.Y.Z), got: {version!r}")
    return version


def _load_latest_changelog_version(changelog_path: Path) -> str:
    for line in changelog_path.read_text(encoding="utf-8").splitlines():
        match = CHANGELOG_VERSION_RE.match(line.strip())
        if match:
            return match.group(1)
    raise ValueError("Could not find a semver heading in CHANGELOG.md (expected: ## [X.Y.Z])")


def _load_runtime_constant_version(source_path: Path, constant_name: str) -> str:
    assignment_re = re.compile(rf"^{re.escape(constant_name)}\s*=\s*\"([^\"]+)\"$")
    for line in source_path.read_text(encoding="utf-8").splitlines():
        match = assignment_re.match(line.strip())
        if match:
            version = match.group(1).strip()
            if not SEMVER_RE.match(version):
                message = (
                    f"{source_path} constant {constant_name} must be semver "
                    f"(X.Y.Z), got: {version!r}"
                )
                raise ValueError(message)
            return version
    raise ValueError(f"Missing {constant_name} assignment in {source_path}")


def _discover_tag_from_env() -> str | None:
    ref_type = os.getenv("GITHUB_REF_TYPE", "")
    ref_name = os.getenv("GITHUB_REF_NAME", "")
    if ref_type == "tag" and ref_name:
        return ref_name

    ref = os.getenv("GITHUB_REF", "")
    if ref.startswith("refs/tags/"):
        return ref

    return None


def main() -> int:
    args = _parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    pyproject_version = _load_pyproject_version(repo_root / "pyproject.toml")
    changelog_version = _load_latest_changelog_version(repo_root / "CHANGELOG.md")

    errors: list[str] = []
    runtime_constant_count = 0
    tag_version: str | None = None

    if pyproject_version != changelog_version:
        errors.append(
            "pyproject.toml and CHANGELOG.md versions differ: "
            f"pyproject={pyproject_version}, changelog={changelog_version}"
        )

    for relative_path, constant_name in RUNTIME_VERSION_CONSTANTS:
        source_path = repo_root / relative_path
        try:
            runtime_version = _load_runtime_constant_version(source_path, constant_name)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        runtime_constant_count += 1
        if runtime_version != pyproject_version:
            errors.append(
                "Runtime version constant does not match project version: "
                f"{relative_path}:{constant_name}={runtime_version}, pyproject={pyproject_version}"
            )

    raw_tag = args.tag or _discover_tag_from_env()
    if raw_tag:
        try:
            tag_version = _normalize_tag(raw_tag)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if tag_version != pyproject_version:
                errors.append(
                    "Tag version does not match project version: "
                    f"tag={tag_version}, pyproject={pyproject_version}"
                )

    if errors:
        print("Release metadata validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    parts = [f"version={pyproject_version}"]
    if tag_version is not None:
        parts.append(f"tag=v{tag_version}")
    parts.append(f"runtime_constants={runtime_constant_count}")
    print(f"Release metadata validation passed ({', '.join(parts)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
