#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

REQUIRED_KINDS = {
    "Service": "helloworld-{env}",
    "Deployment": "helloworld-{env}",
    "ConfigMap": "helloworld-config-{env}",
    "NetworkPolicy": "helloworld-{env}",
    "Gateway": "helloworld-gateway-{env}",
    "VirtualService": "helloworld-virtualservice-{env}",
    "DestinationRule": "helloworld-destinationrule-{env}",
    "Certificate": "helloworld-cert-{env}",
}
EXPECTED_ISSUER = {
    "test": "letsencrypt-staging",
    "prod": "letsencrypt-prod",
}
EXPECTED_DNS_PEER_SELECTORS = (
    {"k8s-app": "kube-dns"},
    {"app.kubernetes.io/name": "coredns"},
    {"k8s-app": "node-local-dns"},
)
EXPECTED_INGRESS_GATEWAY_SELECTOR = {"istio": "ingressgateway"}
CONTROLLER_API_EGRESS_PLACEHOLDER_CIDR = "127.255.255.255/32"
PROD_APP_ALERTS = {
    "HelloworldHighErrorRate",
    "HelloworldHighLatencyP95",
    "HelloworldErrorBudgetBurnFast",
    "HelloworldErrorBudgetBurnSlow",
}
PROD_INGRESS_ALERTS = {
    "IstioGateway5xxRate",
    "IstioGateway429Saturation",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate rendered overlay manifest invariants")
    parser.add_argument(
        "--overlay",
        dest="overlays",
        action="append",
        choices=["test", "prod"],
        help="Overlay to validate (defaults to test and prod)",
    )
    parser.add_argument(
        "--controller-egress-patch",
        dest="controller_egress_patches",
        action="append",
        help=(
            "Strategic merge patch file used to replace the controller API egress placeholder. "
            "Can be provided multiple times."
        ),
    )
    return parser.parse_args()


def _expected_host(env: str) -> str:
    return f"{env}.helloworld.shipshape.example.com"


def _render_kustomization(path: Path) -> list[dict[str, Any]]:
    cmd = ["kustomize", "build", str(path)]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("kustomize is required but was not found in PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        raise RuntimeError(f"kustomize build failed for path '{path}': {stderr}") from exc

    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _render_overlay(repo_root: Path, overlay: str) -> list[dict[str, Any]]:
    overlay_path = repo_root / "k8s" / "overlays" / overlay
    return _render_kustomization(overlay_path)


def _resolve_existing_path(repo_root: Path, raw_path: str) -> Path | None:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    candidate = candidate.resolve()
    if candidate.is_file():
        return candidate
    return None


def _render_controller_with_egress_patch(repo_root: Path, patch_path: Path) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(
        prefix="controller-egress-",
        dir=repo_root,
    ) as temp_dir:
        temp_path = Path(temp_dir)
        kustomization_path = temp_path / "kustomization.yaml"
        controller_resource = os.path.relpath(repo_root / "k8s" / "controller", start=temp_path)
        copied_patch_path = temp_path / "controller-egress.patch.yaml"
        shutil.copyfile(patch_path, copied_patch_path)
        kustomization = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": [controller_resource],
            "patches": [{"path": copied_patch_path.name}],
        }
        kustomization_path.write_text(
            yaml.safe_dump(kustomization, sort_keys=False), encoding="utf-8"
        )
        return _render_kustomization(temp_path)


def _metadata_name(doc: dict[str, Any]) -> str:
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    name = metadata.get("name")
    return name if isinstance(name, str) else ""


def _metadata_labels(doc: dict[str, Any]) -> dict[str, str]:
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    labels = metadata.get("labels")
    if not isinstance(labels, dict):
        return {}
    return {k: v for k, v in labels.items() if isinstance(k, str) and isinstance(v, str)}


def _kind(doc: dict[str, Any]) -> str:
    kind = doc.get("kind")
    return kind if isinstance(kind, str) else ""


def _find_resource(docs: Iterable[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    for doc in docs:
        if _kind(doc) == kind and _metadata_name(doc) == name:
            return doc
    return None


def _peer_labels(
    entry: dict[str, Any], selector_key: str
) -> tuple[str | None, dict[str, str] | None]:
    """Extract the namespace name and pod matchLabels from a NetworkPolicy peer entry.

    Returns (namespace_name, pod_match_labels) where either may be None if the
    expected structure is absent.
    """
    namespace_selector = entry.get("namespaceSelector")
    namespace_labels = (
        namespace_selector.get("matchLabels") if isinstance(namespace_selector, dict) else None
    )
    namespace_name = (
        namespace_labels.get("kubernetes.io/metadata.name")
        if isinstance(namespace_labels, dict)
        else None
    )
    pod_selector = entry.get(selector_key)
    pod_labels = pod_selector.get("matchLabels") if isinstance(pod_selector, dict) else None
    return namespace_name, pod_labels


def _validate_labels(doc: dict[str, Any], env: str, errors: list[str]) -> None:
    labels = _metadata_labels(doc)
    kind = _kind(doc)
    name = _metadata_name(doc)
    if labels.get("app") != "helloworld":
        errors.append(f"{kind}/{name} is missing required label app=helloworld")
    if labels.get("env") != env:
        errors.append(f"{kind}/{name} is missing required label env={env}")


def _validate_virtual_service(doc: dict[str, Any], env: str, errors: list[str]) -> None:
    expected_gateway = f"helloworld-gateway-{env}"
    expected_service_host = f"helloworld-{env}"
    expected_host = _expected_host(env)

    if doc.get("apiVersion") != "networking.istio.io/v1beta1":
        errors.append("VirtualService must use apiVersion networking.istio.io/v1beta1")

    spec = doc.get("spec")
    if not isinstance(spec, dict):
        errors.append("VirtualService spec is missing")
        return

    gateways = spec.get("gateways")
    if not isinstance(gateways, list) or expected_gateway not in gateways:
        errors.append(f"VirtualService gateways must include {expected_gateway}")

    hosts = spec.get("hosts")
    if not isinstance(hosts, list) or expected_host not in hosts:
        errors.append(f"VirtualService hosts must include {expected_host}")

    http_rules = spec.get("http")
    if not isinstance(http_rules, list):
        errors.append("VirtualService spec.http must be a list")
        return

    destination_hosts: list[str] = []
    for http_rule in http_rules:
        if not isinstance(http_rule, dict):
            continue
        routes = http_rule.get("route")
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict):
                continue
            destination = route.get("destination")
            if not isinstance(destination, dict):
                continue
            host = destination.get("host")
            if isinstance(host, str):
                destination_hosts.append(host)

    if expected_service_host not in destination_hosts:
        errors.append(f"VirtualService route destination host must include {expected_service_host}")


def _validate_gateway(doc: dict[str, Any], env: str, errors: list[str]) -> None:
    expected_host = _expected_host(env)
    expected_secret = f"helloworld-{env}-tls"

    if doc.get("apiVersion") != "networking.istio.io/v1beta1":
        errors.append("Gateway must use apiVersion networking.istio.io/v1beta1")

    spec = doc.get("spec")
    if not isinstance(spec, dict):
        errors.append("Gateway spec is missing")
        return

    servers = spec.get("servers")
    if not isinstance(servers, list) or not servers:
        errors.append("Gateway spec.servers must be a non-empty list")
        return

    saw_https_server = False
    for server in servers:
        if not isinstance(server, dict):
            continue
        hosts = server.get("hosts")
        if not isinstance(hosts, list) or expected_host not in hosts:
            errors.append(f"Gateway server hosts must include {expected_host}")
        port = server.get("port")
        if not isinstance(port, dict):
            continue
        if port.get("number") == 443:
            saw_https_server = True
            tls = server.get("tls")
            if not isinstance(tls, dict) or tls.get("credentialName") != expected_secret:
                errors.append(
                    f"Gateway HTTPS server must use tls credentialName {expected_secret}"
                )

    if not saw_https_server:
        errors.append("Gateway is missing an HTTPS server on port 443")


def _validate_certificate(doc: dict[str, Any], env: str, errors: list[str]) -> None:
    expected_host = _expected_host(env)
    expected_secret = f"helloworld-{env}-tls"
    expected_issuer = EXPECTED_ISSUER[env]

    if doc.get("apiVersion") != "cert-manager.io/v1":
        errors.append("Certificate must use apiVersion cert-manager.io/v1")

    spec = doc.get("spec")
    if not isinstance(spec, dict):
        errors.append("Certificate spec is missing")
        return

    if spec.get("secretName") != expected_secret:
        errors.append(f"Certificate secretName must be {expected_secret}")

    dns_names = spec.get("dnsNames")
    if not isinstance(dns_names, list) or expected_host not in dns_names:
        errors.append(f"Certificate dnsNames must include {expected_host}")

    issuer_ref = spec.get("issuerRef")
    if not isinstance(issuer_ref, dict):
        errors.append("Certificate issuerRef is missing")
    else:
        if issuer_ref.get("kind") != "ClusterIssuer":
            errors.append("Certificate issuerRef.kind must be ClusterIssuer")
        if issuer_ref.get("name") != expected_issuer:
            errors.append(f"Certificate issuerRef.name must be {expected_issuer}")


def _validate_destination_rule(doc: dict[str, Any], env: str, errors: list[str]) -> None:
    if doc.get("apiVersion") != "networking.istio.io/v1beta1":
        errors.append("DestinationRule must use apiVersion networking.istio.io/v1beta1")

    spec = doc.get("spec")
    if not isinstance(spec, dict):
        errors.append("DestinationRule spec is missing")
        return

    expected_host = f"helloworld-{env}"
    if spec.get("host") != expected_host:
        errors.append(f"DestinationRule host must be {expected_host}")


def _validate_service(doc: dict[str, Any], env: str, errors: list[str]) -> None:
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        errors.append("Service spec is missing")
        return
    selector = spec.get("selector")
    if not isinstance(selector, dict):
        errors.append("Service selector is missing")
        return
    if selector.get("app") != "helloworld":
        errors.append("Service selector app must be helloworld")
    if selector.get("env") != env:
        errors.append(f"Service selector env must be {env}")


def _validate_network_policy(doc: dict[str, Any], errors: list[str]) -> None:
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        errors.append("NetworkPolicy spec is missing")
        return

    ingress_rules = spec.get("ingress")
    if not isinstance(ingress_rules, list) or not ingress_rules:
        errors.append("NetworkPolicy ingress rules are missing")
        return

    has_ingress_gateway_source = False
    has_monitoring_source = False
    ingress_gateway_selector_ok = False
    for rule in ingress_rules:
        if not isinstance(rule, dict):
            continue
        from_entries = rule.get("from")
        if not isinstance(from_entries, list) or not from_entries:
            errors.append("NetworkPolicy ingress rules must define explicit from selectors")
            continue
        for entry in from_entries:
            if not isinstance(entry, dict):
                continue
            namespace_name, pod_labels = _peer_labels(entry, "podSelector")
            pod_istio = pod_labels.get("istio") if isinstance(pod_labels, dict) else None

            if namespace_name == "istio-system" and pod_istio == "ingressgateway":
                has_ingress_gateway_source = True
                if pod_labels == EXPECTED_INGRESS_GATEWAY_SELECTOR:
                    ingress_gateway_selector_ok = True
            if namespace_name == "monitoring":
                has_monitoring_source = True

    if not has_ingress_gateway_source:
        errors.append(
            "NetworkPolicy ingress must include source selector for istio-system/ingressgateway"
        )
    elif not ingress_gateway_selector_ok:
        errors.append(
            "NetworkPolicy ingress gateway podSelector must remain exactly "
            f"{EXPECTED_INGRESS_GATEWAY_SELECTOR} after render"
        )
    if not has_monitoring_source:
        errors.append("NetworkPolicy ingress must include source selector for monitoring namespace")

    egress_rules = spec.get("egress")
    if not isinstance(egress_rules, list) or not egress_rules:
        errors.append("NetworkPolicy egress rules are missing")
        return

    observed_dns_selectors: list[dict[str, str]] = []
    for rule in egress_rules:
        if not isinstance(rule, dict):
            continue
        to_entries = rule.get("to")
        if not isinstance(to_entries, list):
            continue
        for entry in to_entries:
            if not isinstance(entry, dict):
                continue
            namespace_name, pod_labels = _peer_labels(entry, "podSelector")
            if namespace_name != "kube-system":
                continue
            if isinstance(pod_labels, dict):
                normalized = {
                    key: value
                    for key, value in pod_labels.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                observed_dns_selectors.append(normalized)

    for expected in EXPECTED_DNS_PEER_SELECTORS:
        if expected not in observed_dns_selectors:
            errors.append(
                "NetworkPolicy egress DNS podSelector mutated or missing; "
                f"expected exact selector {expected}"
            )


def _validate_deployment(doc: dict[str, Any], env: str, errors: list[str]) -> None:
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        errors.append("Deployment spec is missing")
        return

    selector = spec.get("selector")
    if not isinstance(selector, dict):
        errors.append("Deployment selector is missing")
        return
    match_labels = selector.get("matchLabels")
    if not isinstance(match_labels, dict):
        errors.append("Deployment selector.matchLabels is missing")
        return
    if match_labels.get("app") != "helloworld":
        errors.append("Deployment selector.matchLabels.app must be helloworld")
    if match_labels.get("env") != env:
        errors.append(f"Deployment selector.matchLabels.env must be {env}")


def _validate_configmap(doc: dict[str, Any], errors: list[str]) -> None:
    data = doc.get("data")
    if not isinstance(data, dict):
        errors.append("ConfigMap data is missing")
        return
    if "MESSAGE" not in data:
        errors.append("ConfigMap data.MESSAGE is missing")


def validate_overlay(repo_root: Path, overlay: str) -> list[str]:
    errors: list[str] = []
    docs = _render_overlay(repo_root, overlay)

    for doc in docs:
        if _kind(doc) == "Namespace":
            errors.append(
                "Overlay render must not emit Namespace resources; use k8s/namespace only"
            )

    for kind, pattern in REQUIRED_KINDS.items():
        expected_name = pattern.format(env=overlay)
        resource = _find_resource(docs, kind, expected_name)
        if resource is None:
            errors.append(f"Missing required resource {kind}/{expected_name}")
            continue

        _validate_labels(resource, overlay, errors)

        if kind == "VirtualService":
            _validate_virtual_service(resource, overlay, errors)
        elif kind == "Gateway":
            _validate_gateway(resource, overlay, errors)
        elif kind == "Certificate":
            _validate_certificate(resource, overlay, errors)
        elif kind == "DestinationRule":
            _validate_destination_rule(resource, overlay, errors)
        elif kind == "NetworkPolicy":
            _validate_network_policy(resource, errors)
        elif kind == "Service":
            _validate_service(resource, overlay, errors)
        elif kind == "Deployment":
            _validate_deployment(resource, overlay, errors)
        elif kind == "ConfigMap":
            _validate_configmap(resource, errors)

    return errors


def validate_controller_networkpolicy(
    repo_root: Path,
    *,
    docs: list[dict[str, Any]] | None = None,
    require_non_placeholder_api_cidrs: bool = False,
) -> list[str]:
    errors: list[str] = []
    rendered_docs = docs if docs is not None else _render_kustomization(
        repo_root / "k8s" / "controller"
    )
    policy = _find_resource(rendered_docs, "NetworkPolicy", "helloworld-controller")
    if policy is None:
        return ["Missing required resource NetworkPolicy/helloworld-controller"]

    spec = policy.get("spec")
    if not isinstance(spec, dict):
        return ["Controller NetworkPolicy spec is missing"]

    pod_selector = spec.get("podSelector")
    match_labels = pod_selector.get("matchLabels") if isinstance(pod_selector, dict) else None
    if match_labels != {"app": "helloworld-controller"}:
        errors.append(
            "Controller NetworkPolicy podSelector must remain exactly "
            "{'app': 'helloworld-controller'} after render"
        )

    egress_rules = spec.get("egress")
    if not isinstance(egress_rules, list):
        errors.append("Controller NetworkPolicy egress rules are missing")
        return errors

    observed_dns_selectors: list[dict[str, str]] = []
    saw_api_ipblock_443 = False
    observed_api_cidrs: list[str] = []
    for rule in egress_rules:
        if not isinstance(rule, dict):
            continue
        to_entries = rule.get("to")
        if not isinstance(to_entries, list):
            continue
        ports = rule.get("ports")
        allows_tcp_443 = isinstance(ports, list) and any(
            isinstance(p, dict) and p.get("protocol") == "TCP" and p.get("port") == 443
            for p in ports
        )
        for entry in to_entries:
            if not isinstance(entry, dict):
                continue
            namespace_name, pod_labels = _peer_labels(entry, "podSelector")
            if namespace_name == "default":
                errors.append(
                    "Controller NetworkPolicy must not allow broad default "
                    "namespace egress on TCP/443"
                )
            if namespace_name != "kube-system":
                ip_block = entry.get("ipBlock")
                cidr = ip_block.get("cidr") if isinstance(ip_block, dict) else None
                if allows_tcp_443 and isinstance(cidr, str) and cidr:
                    saw_api_ipblock_443 = True
                    observed_api_cidrs.append(cidr)
                continue
            if isinstance(pod_labels, dict):
                normalized = {
                    key: value
                    for key, value in pod_labels.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                observed_dns_selectors.append(normalized)

    for expected in EXPECTED_DNS_PEER_SELECTORS:
        if expected not in observed_dns_selectors:
            errors.append(
                "Controller NetworkPolicy DNS selector mutated or missing; "
                f"expected exact selector {expected}"
            )

    if not saw_api_ipblock_443:
        errors.append(
            "Controller NetworkPolicy must include explicit ipBlock egress "
            "on TCP/443 for API access"
        )
    elif require_non_placeholder_api_cidrs and (
        CONTROLLER_API_EGRESS_PLACEHOLDER_CIDR in observed_api_cidrs
    ):
        errors.append(
            "Controller NetworkPolicy still includes placeholder API CIDR "
            f"{CONTROLLER_API_EGRESS_PLACEHOLDER_CIDR}; replace it with environment-specific "
            "control-plane endpoint CIDRs via examples/controller-egress/*.patch.yaml "
            "or examples/controller-apiserver-cidr-patch.yaml"
        )

    return errors


def validate_monitoring_ownership(repo_root: Path) -> list[str]:
    errors: list[str] = []

    monitoring_docs = _render_kustomization(repo_root / "k8s" / "monitoring")
    monitor = _find_resource(monitoring_docs, "ServiceMonitor", "helloworld")
    if monitor is None:
        errors.append("Missing required monitoring resource ServiceMonitor/helloworld")
    else:
        spec = monitor.get("spec")
        target_labels = spec.get("targetLabels") if isinstance(spec, dict) else None
        if not isinstance(target_labels, list) or "env" not in target_labels:
            errors.append(
                "ServiceMonitor/helloworld must copy service label env via spec.targetLabels"
            )

    rule = _find_resource(monitoring_docs, "PrometheusRule", "helloworld")
    if rule is None:
        errors.append("Missing required monitoring resource PrometheusRule/helloworld")
    else:
        spec = rule.get("spec")
        groups = spec.get("groups") if isinstance(spec, dict) else None
        alert_by_name: dict[str, dict[str, Any]] = {}
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                rules = group.get("rules")
                if not isinstance(rules, list):
                    continue
                for candidate in rules:
                    if not isinstance(candidate, dict):
                        continue
                    name = candidate.get("alert")
                    if isinstance(name, str):
                        alert_by_name[name] = candidate

        for alert_name in PROD_APP_ALERTS:
            candidate = alert_by_name.get(alert_name)
            if candidate is None:
                errors.append(f"PrometheusRule must define alert {alert_name}")
                continue
            expr = candidate.get("expr")
            if not isinstance(expr, str) or 'env="prod"' not in expr:
                errors.append(
                    f"PrometheusRule alert {alert_name} must scope metrics to env=\"prod\""
                )

        for alert_name in PROD_INGRESS_ALERTS:
            candidate = alert_by_name.get(alert_name)
            if candidate is None:
                errors.append(f"PrometheusRule must define alert {alert_name}")
                continue
            expr = candidate.get("expr")
            if (
                not isinstance(expr, str)
                or 'request_host="prod.helloworld.shipshape.example.com"' not in expr
            ):
                errors.append(
                    f"PrometheusRule alert {alert_name} must scope metrics to request_host="
                    "\"prod.helloworld.shipshape.example.com\""
                )

        for alert_name, candidate in alert_by_name.items():
            if not alert_name.startswith("HelloworldTest"):
                continue
            labels = candidate.get("labels")
            severity = labels.get("severity") if isinstance(labels, dict) else None
            if severity == "critical":
                errors.append(
                    f"PrometheusRule alert {alert_name} must not use critical severity "
                    "(test alerts must be non-paging)"
                )

    for overlay in ("test", "prod"):
        overlay_docs = _render_overlay(repo_root, overlay)
        for doc in overlay_docs:
            kind = _kind(doc)
            if kind not in {"ServiceMonitor", "PrometheusRule"}:
                continue
            errors.append(
                f"{overlay} overlay must not render {kind}/{_metadata_name(doc)}; "
                "deploy app monitoring from k8s/monitoring only"
            )

    return errors


def main() -> int:
    args = _parse_args()
    overlays = args.overlays or ["test", "prod"]
    controller_egress_patches = args.controller_egress_patches or []
    repo_root = Path(__file__).resolve().parents[1]

    all_errors: list[str] = []
    for overlay in overlays:
        errors = validate_overlay(repo_root=repo_root, overlay=overlay)
        if errors:
            all_errors.extend(f"[{overlay}] {error}" for error in errors)
        else:
            print(f"[{overlay}] manifest invariants passed")

    controller_errors = validate_controller_networkpolicy(repo_root=repo_root)
    if controller_errors:
        all_errors.extend(f"[controller] {error}" for error in controller_errors)
    else:
        print("[controller] networkpolicy selector invariants passed")

    for raw_patch_path in controller_egress_patches:
        patch_path = _resolve_existing_path(repo_root=repo_root, raw_path=raw_patch_path)
        patch_context = f"[controller-egress:{raw_patch_path}]"
        if patch_path is None:
            all_errors.append(f"{patch_context} patch file not found")
            continue
        try:
            patched_docs = _render_controller_with_egress_patch(
                repo_root=repo_root, patch_path=patch_path
            )
        except RuntimeError as exc:
            all_errors.append(
                f"{patch_context} failed to render patched controller manifest: {exc}"
            )
            continue

        patched_errors = validate_controller_networkpolicy(
            repo_root=repo_root,
            docs=patched_docs,
            require_non_placeholder_api_cidrs=True,
        )
        if patched_errors:
            all_errors.extend(f"{patch_context} {error}" for error in patched_errors)
        else:
            print(f"{patch_context} placeholder CIDR override invariants passed")

    monitoring_errors = validate_monitoring_ownership(repo_root=repo_root)
    if monitoring_errors:
        all_errors.extend(f"[monitoring] {error}" for error in monitoring_errors)
    else:
        print("[monitoring] single-source ownership invariants passed")

    if all_errors:
        print("Manifest invariant validation failed:", file=sys.stderr)
        for error in all_errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
