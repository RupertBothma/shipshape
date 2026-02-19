from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module() -> object:
    module_path = Path(__file__).resolve().parents[2] / "hack" / "check_immutable_images.py"
    spec = importlib.util.spec_from_file_location("check_immutable_images", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load check_immutable_images module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rejects_all_zero_digest() -> None:
    module = _load_module()
    image = "localhost:5000/shipshape-helloworld@sha256:" + ("0" * 64)
    err = module._validate_image_reference(image)  # type: ignore[attr-defined]
    assert err == "uses all-zero sha256 digest placeholder"


def test_rejects_placeholder_registry() -> None:
    module = _load_module()
    image = "registry.example.com/shipshape-helloworld@sha256:" + ("1" * 64)
    err = module._validate_image_reference(image)  # type: ignore[attr-defined]
    assert err == "uses placeholder registry path"


def test_accepts_digest_pinned_non_placeholder_image() -> None:
    module = _load_module()
    image = "localhost:5000/shipshape-helloworld@sha256:" + ("a" * 64)
    err = module._validate_image_reference(image)  # type: ignore[attr-defined]
    assert err is None
