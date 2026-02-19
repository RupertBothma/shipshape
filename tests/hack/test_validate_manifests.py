from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module() -> object:
    module_path = Path(__file__).resolve().parents[2] / "hack" / "validate_manifests.py"
    spec = importlib.util.spec_from_file_location("validate_manifests", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load validate_manifests module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _controller_policy(egress: list[dict[str, object]]) -> dict[str, object]:
    return {
        "kind": "NetworkPolicy",
        "metadata": {"name": "helloworld-controller"},
        "spec": {
            "podSelector": {"matchLabels": {"app": "helloworld-controller"}},
            "egress": egress,
        },
    }


def test_validate_controller_networkpolicy_rejects_default_namespace_egress(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    module = _load_module()
    egress = [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "default"}
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 443}],
        }
    ]
    policy = _controller_policy(egress)
    monkeypatch.setattr(module, "_render_kustomization", lambda _: [policy])

    errors = module.validate_controller_networkpolicy(Path("/unused"))  # type: ignore[attr-defined]

    assert any("must not allow broad default namespace egress" in error for error in errors)


def test_validate_controller_networkpolicy_requires_api_ipblock(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    module = _load_module()
    egress = [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                },
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "coredns"}},
                },
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "node-local-dns"}},
                },
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        }
    ]
    policy = _controller_policy(egress)
    monkeypatch.setattr(module, "_render_kustomization", lambda _: [policy])

    errors = module.validate_controller_networkpolicy(Path("/unused"))  # type: ignore[attr-defined]

    assert any("must include explicit ipBlock egress on TCP/443" in error for error in errors)


def test_validate_controller_networkpolicy_rejects_placeholder_api_cidr(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    module = _load_module()
    egress = [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                },
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "coredns"}},
                },
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "node-local-dns"}},
                },
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        },
        {
            "to": [{"ipBlock": {"cidr": "127.255.255.255/32"}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        },
    ]
    policy = _controller_policy(egress)
    monkeypatch.setattr(module, "_render_kustomization", lambda _: [policy])

    errors = module.validate_controller_networkpolicy(  # type: ignore[attr-defined]
        Path("/unused"), require_non_placeholder_api_cidrs=True
    )

    assert any("still includes placeholder API CIDR" in error for error in errors)
