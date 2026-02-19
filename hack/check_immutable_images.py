#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_DIGEST_PATTERN = re.compile(r"^(?P<name>.+)@sha256:(?P<digest>[0-9a-fA-F]{64})$")
_ZERO_DIGEST = "0" * 64
_PLACEHOLDER_REGISTRY_PREFIXES = (
    "registry.example.com/",
    "docker.io/example/",
    "quay.io/example/",
)
_TARGETS: tuple[tuple[str, str], ...] = (
    ("k8s/overlays/prod", "prod overlay"),
    ("k8s/controller", "controller manifests"),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate immutable production image references")
    parser.add_argument(
        "--verify-remote",
        action="store_true",
        help=(
            "Additionally verify each image digest exists in the registry using "
            "'docker manifest inspect'."
        ),
    )
    parser.add_argument(
        "--remote-timeout-seconds",
        type=int,
        default=20,
        help="Timeout for remote digest verification checks (default: 20 seconds).",
    )
    return parser.parse_args()


def _render(repo_root: Path, target: str) -> list[dict[str, Any]]:
    cmd = ["kustomize", "build", str(repo_root / target)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _nested_dict(doc: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    """Traverse nested dicts safely, returning None if any key is missing or not a dict."""
    current: Any = doc
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _extract_images(doc: dict[str, Any]) -> list[tuple[str, str]]:
    metadata = _nested_dict(doc, "metadata")
    if metadata is None:
        return []
    deployment_name = metadata.get("name")
    if not isinstance(deployment_name, str) or not deployment_name:
        return []

    pod_spec = _nested_dict(doc, "spec", "template", "spec")
    if pod_spec is None:
        return []

    pairs: list[tuple[str, str]] = []
    for field in ("containers", "initContainers"):
        containers = pod_spec.get(field)
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            container_name = container.get("name")
            image = container.get("image")
            if isinstance(container_name, str) and isinstance(image, str):
                pairs.append((f"{deployment_name}/{container_name}", image))
    return pairs


def _validate_image_reference(image: str) -> str | None:
    # Allow abstract placeholders (unhydrated state)
    if image.startswith("shipshape/"):
        return None

    match = _DIGEST_PATTERN.match(image)
    if match is None:
        return "must use immutable digest format image@sha256:<64-hex>"

    name = match.group("name").lower()
    digest = match.group("digest").lower()

    if digest == _ZERO_DIGEST:
        return "uses all-zero sha256 digest placeholder"

    if name.startswith(_PLACEHOLDER_REGISTRY_PREFIXES) or "/example/" in name:
        return "uses placeholder registry path"

    return None


def _verify_remote_digest(image: str, timeout_seconds: int) -> str | None:
    cmd = ["docker", "manifest", "inspect", image]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout_seconds)
    except FileNotFoundError:
        return "docker CLI is required for --verify-remote checks"
    except subprocess.TimeoutExpired:
        return f"remote digest verification timed out after {timeout_seconds}s"
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip().splitlines()
        reason = details[0] if details else "registry lookup failed"
        return f"remote digest verification failed: {reason}"
    return None


def main() -> int:
    args = _parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    violations: list[str] = []

    for target, label in _TARGETS:
        try:
            docs = _render(repo_root, target)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip()
            print(f"ERROR: kustomize build failed for {target}: {stderr}", file=sys.stderr)
            return 1
        except FileNotFoundError:
            print("ERROR: kustomize is required but was not found in PATH", file=sys.stderr)
            return 1

        for doc in docs:
            if doc.get("kind") != "Deployment":
                continue
            for container_id, image in _extract_images(doc):
                error = _validate_image_reference(image)
                if not error and args.verify_remote:
                    error = _verify_remote_digest(image, args.remote_timeout_seconds)
                if error:
                    violations.append(f"[{label}] {container_id} {error}, got: {image}")

    if violations:
        print("Immutable image validation failed:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1

    print("Immutable image validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
