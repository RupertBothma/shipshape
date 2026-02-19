#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
HTTP_TIMEOUT_SECONDS = 10
OPERATIONS_DOC_RELATIVE = "docs/operations.md#"
OPERATIONS_DOC_CANONICAL_RE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/blob/[^/]+/docs/operations\.md#(?P<anchor>.+)$"
)

IGNORED_DIRS = {".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
IGNORED_TOP_LEVEL_DIRS = {"build", "shipshape.egg-info"}


def _github_anchor(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(r"[^\w\s-]", "", lowered)
    lowered = re.sub(r"\s+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered)
    return lowered.strip("-")


def _extract_markdown_anchors(path: Path) -> set[str]:
    """Extract GitHub-style heading anchors from a Markdown file."""
    anchors: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue
        heading = line.lstrip("#").strip()
        if heading:
            anchors.add(_github_anchor(heading))
    return anchors


def _operations_anchors(path: Path) -> set[str] | None:
    """Return heading anchors from the operations doc, or None if it doesn't exist."""
    if not path.exists():
        return None
    return _extract_markdown_anchors(path)


def _collect_markdown_files(repo_root: Path) -> list[Path]:
    markdown_files: list[Path] = []
    for path in repo_root.rglob("*.md"):
        parts = path.relative_to(repo_root).parts
        if any(part in IGNORED_DIRS for part in parts):
            continue
        if parts and parts[0] in IGNORED_TOP_LEVEL_DIRS:
            continue
        markdown_files.append(path)
    return markdown_files


def _validate_repo_markdown_links(repo_root: Path) -> list[str]:
    markdown_files = _collect_markdown_files(repo_root)
    anchors_by_path = {path.resolve(): _extract_markdown_anchors(path) for path in markdown_files}
    errors: list[str] = []

    for markdown_file in markdown_files:
        content = markdown_file.read_text(encoding="utf-8")
        for link in MARKDOWN_LINK_RE.findall(content):
            normalized_link = link.strip().strip("<>")
            if not normalized_link:
                errors.append(f"{markdown_file}: empty markdown link")
                continue

            if normalized_link.startswith(("http://", "https://", "mailto:")):
                continue

            parsed = urllib.parse.urlparse(normalized_link)
            if parsed.scheme:
                errors.append(
                    f"{markdown_file}: unsupported markdown link scheme "
                    f"{parsed.scheme} ({normalized_link})"
                )
                continue

            target_part, _, fragment = normalized_link.partition("#")
            if target_part:
                if target_part.startswith("/"):
                    target_path = (repo_root / target_part.lstrip("/")).resolve()
                else:
                    target_path = (markdown_file.parent / target_part).resolve()
            else:
                target_path = markdown_file.resolve()

            if not target_path.exists():
                errors.append(
                    f"{markdown_file}: markdown link target does not exist ({normalized_link})"
                )
                continue

            if fragment and target_path.suffix.lower() == ".md":
                anchors = anchors_by_path.get(target_path, set())
                if fragment not in anchors:
                    errors.append(
                        f"{markdown_file}: missing markdown anchor #{fragment} in "
                        f"{target_path.relative_to(repo_root)} ({normalized_link})"
                    )
    return errors


def _http_open(url: str, method: str) -> str | None:
    """Issue an HTTP request and return an error string on failure, or None on success."""
    request = urllib.request.Request(url=url, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
            if resp.status >= 400:
                return f"HTTP status {resp.status}"
            return None
    except urllib.error.HTTPError as exc:
        return f"HTTP status {exc.code}"
    except urllib.error.URLError as exc:
        return str(exc.reason)


def _check_http(url: str) -> str | None:
    result = _http_open(url, "HEAD")
    if result == "HTTP status 405":
        # Some hosts block HEAD requests; retry with GET.
        return _http_open(url, "GET")
    return result


def _validate_url(url: str, *, operations_anchors: set[str] | None) -> str | None:
    if not url:
        return "empty URL"
    if url.startswith("mailto:"):
        return None

    # Check operations doc anchors for both relative and canonical links.
    anchor: str | None = None
    if url.startswith(OPERATIONS_DOC_RELATIVE):
        anchor = url.partition("#")[2]
    else:
        canonical_match = OPERATIONS_DOC_CANONICAL_RE.match(url)
        if canonical_match:
            anchor = canonical_match.group("anchor")

    if anchor is not None:
        if operations_anchors is not None and anchor not in operations_anchors:
            return f"missing docs/operations.md anchor #{anchor}"
        return None

    if url.startswith(("http://", "https://")):
        return _check_http(url)

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme:
        return f"unsupported URL scheme: {parsed.scheme}"
    return "URL must be mailto:, docs/operations.md#..., or absolute http(s)"


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _collect_security_links(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    return [
        (f"{path}:markdown-link", link)
        for link in MARKDOWN_LINK_RE.findall(path.read_text(encoding="utf-8"))
    ]


def _collect_issue_template_links(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    content = _load_yaml(path)
    if not isinstance(content, dict):
        return []
    contact_links = content.get("contact_links", [])
    if not isinstance(contact_links, list):
        return []
    findings: list[tuple[str, str]] = []
    for idx, entry in enumerate(contact_links):
        if isinstance(entry, dict):
            url = entry.get("url")
            if isinstance(url, str):
                findings.append((f"{path}:contact_links[{idx}]", url))
    return findings


def _collect_runbook_urls(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    findings: list[tuple[str, str]] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(doc, dict):
            continue
        groups = (doc.get("spec") or {}).get("groups", [])
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            rules = group.get("rules", [])
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                annotations = rule.get("annotations")
                if not isinstance(annotations, dict):
                    continue
                runbook = annotations.get("runbook_url")
                if isinstance(runbook, str):
                    alert = rule.get("alert", "<unknown>")
                    findings.append((f"{path}:{alert}", runbook))
    return findings


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    operations_path = repo_root / "docs" / "operations.md"
    operations_anchors = (
        _extract_markdown_anchors(operations_path) if operations_path.exists() else None
    )

    links: list[tuple[str, str]] = []
    links.extend(_collect_security_links(repo_root / "SECURITY.md"))
    links.extend(
        _collect_issue_template_links(
            repo_root / ".github" / "ISSUE_TEMPLATE" / "config.yml"
        )
    )
    links.extend(_collect_runbook_urls(repo_root / "k8s" / "monitoring" / "prometheusrule.yaml"))
    links.extend(_collect_runbook_urls(repo_root / "k8s" / "controller" / "prometheusrule.yaml"))

    errors = _validate_repo_markdown_links(repo_root)
    for source, url in links:
        reason = _validate_url(url, operations_anchors=operations_anchors)
        if reason is not None:
            errors.append(f"{source}: {reason} ({url})")

    if errors:
        print("Documentation link validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("Documentation link validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
