#!/usr/bin/env bash
# Shared functions for E2E test scripts (Kind and Minikube).
# Source this file; do not execute directly.

E2E_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${E2E_LIB_DIR}/.." && pwd)}"

# Load dynamic registry configuration if it exists
if [[ -f "${REPO_ROOT}/.local-registry-env" ]]; then
  # shellcheck source=/dev/null
  source "${REPO_ROOT}/.local-registry-env"
fi
REGISTRY_URL="${REGISTRY_URL:-localhost:5000}"

NAMESPACE="${NAMESPACE:-shipshape}"
K8S_VERSION="${K8S_VERSION:-v1.34.3}"
ISTIO_VERSION="${ISTIO_VERSION:-1.28.3}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.16.2}"
PROMETHEUS_OPERATOR_VERSION="${PROMETHEUS_OPERATOR_VERSION:-v0.89.0}"
APP_IMAGE_TEST="${APP_IMAGE_TEST:-${REGISTRY_URL}/shipshape-helloworld:test}"
APP_IMAGE_PROD="${APP_IMAGE_PROD:-${REGISTRY_URL}/shipshape-helloworld:prod}"
CONTROLLER_IMAGE="${CONTROLLER_IMAGE:-${REGISTRY_URL}/shipshape-controller:0.1.0}"
ISTIOCTL_BIN="${ISTIOCTL_BIN:-istioctl}"
INGRESS_USE_PORT_FORWARD="${INGRESS_USE_PORT_FORWARD:-true}"
INGRESS_HTTPS_PORT="${INGRESS_HTTPS_PORT:-8443}"
ISTIO_INSTALL_RETRIES="${ISTIO_INSTALL_RETRIES:-3}"
ISTIO_ROLLOUT_TIMEOUT="${ISTIO_ROLLOUT_TIMEOUT:-300s}"
ISTIO_ROLLOUT_RETRIES="${ISTIO_ROLLOUT_RETRIES:-3}"
APISERVER_READY_RETRIES="${APISERVER_READY_RETRIES:-30}"
APISERVER_READY_SLEEP_SECONDS="${APISERVER_READY_SLEEP_SECONDS:-5}"

PF_PID=""
PF_TARGET=""
LAST_INGRESS_RESPONSE=""
cleanup_port_forward() {
  [[ -n "${PF_PID}" ]] && kill "${PF_PID}" 2>/dev/null || true
  PF_PID=""
  PF_TARGET=""
}

run_python() {
  if command -v uv >/dev/null 2>&1; then
    uv run python "$@"
  else
    python3 "$@"
  fi
}

validate_rendered_manifests() {
  run_python "${REPO_ROOT}/hack/validate_manifests.py" \
    --overlay test \
    --overlay prod \
    --controller-egress-patch "${REPO_ROOT}/examples/controller-apiserver-cidr-patch.yaml" \
    --controller-egress-patch "${REPO_ROOT}/examples/controller-egress/eks.patch.yaml" \
    --controller-egress-patch "${REPO_ROOT}/examples/controller-egress/gke.patch.yaml" \
    --controller-egress-patch "${REPO_ROOT}/examples/controller-egress/aks.patch.yaml"
}

resolve_ingressgateway_pod() {
  local jsonpath='{range .items[*]}'
  jsonpath+='{.metadata.name}{"\t"}{.status.phase}{"\t"}'
  jsonpath+='{range .status.conditions[*]}{.type}={.status}{";"}{end}'
  jsonpath+='{"\n"}{end}'

  kubectl -n istio-system get pods -l istio=ingressgateway -o jsonpath="${jsonpath}" \
    | awk '$2 == "Running" && /Ready=True/ { print $1; exit }'
}

ensure_port_forward() {
  if [[ -n "${PF_PID}" ]] && kill -0 "${PF_PID}" 2>/dev/null; then
    return 0
  fi
  cleanup_port_forward

  # Use pod port-forward (instead of service port-forward) to avoid service
  # endpoint selection races that can produce intermittent empty responses in CI.
  kubectl -n istio-system wait \
    --for=condition=Ready pod \
    -l istio=ingressgateway \
    --timeout=120s >/dev/null
  local ingress_pod
  ingress_pod="$(resolve_ingressgateway_pod)"
  if [[ -z "${ingress_pod}" ]]; then
    echo "ERROR: Could not find a Ready istio ingress gateway pod for port-forwarding" >&2
    kubectl -n istio-system get pods -l istio=ingressgateway -o wide >&2 || true
    return 1
  fi

  # Istio ingressgateway listens for HTTPS on pod port 8443.
  kubectl -n istio-system port-forward "pod/${ingress_pod}" 8443:8443 >/dev/null 2>&1 &
  PF_PID=$!
  PF_TARGET="${ingress_pod}"
  sleep 3

  if ! kill -0 "${PF_PID}" 2>/dev/null; then
    echo "WARNING: ingress pod port-forward exited immediately (pod=${PF_TARGET})" >&2
    cleanup_port_forward
    return 1
  fi
}

ensure_istioctl() {
  if command -v "${ISTIOCTL_BIN}" >/dev/null 2>&1; then
    ISTIOCTL_BIN="$(command -v "${ISTIOCTL_BIN}")"
    return
  fi

  echo "==> Installing istioctl ${ISTIO_VERSION}"
  curl -fsSL https://istio.io/downloadIstio | ISTIO_VERSION="${ISTIO_VERSION}" sh -
  ISTIOCTL_BIN="$(pwd)/istio-${ISTIO_VERSION}/bin/istioctl"
  if [[ ! -x "${ISTIOCTL_BIN}" ]]; then
    echo "ERROR: istioctl was not installed successfully"
    exit 1
  fi
}

wait_for_apiserver_ready() {
  local retries="${1:-${APISERVER_READY_RETRIES}}"
  local sleep_seconds="${2:-${APISERVER_READY_SLEEP_SECONDS}}"
  local attempt

  echo "==> Waiting for Kubernetes API readiness"
  for attempt in $(seq 1 "${retries}"); do
    if kubectl version --request-timeout=5s >/dev/null 2>&1 && \
       kubectl get --raw='/readyz' >/dev/null 2>&1; then
      echo "==> Kubernetes API is ready"
      return 0
    fi
    echo "  [attempt ${attempt}/${retries}] API server not ready yet"
    sleep "${sleep_seconds}"
  done

  echo "ERROR: Kubernetes API did not become ready after ${retries} attempts"
  return 1
}

wait_for_rollout_with_retries() {
  local namespace="$1"
  local resource="$2"
  local timeout="$3"
  local retries="$4"
  local attempt

  for attempt in $(seq 1 "${retries}"); do
    if kubectl -n "${namespace}" rollout status "${resource}" --timeout="${timeout}"; then
      return 0
    fi
    if [[ "${attempt}" -eq "${retries}" ]]; then
      echo "ERROR: rollout did not complete for ${resource} in namespace ${namespace}"
      return 1
    fi
    echo "WARNING: rollout check failed for ${resource} in namespace ${namespace} (attempt ${attempt}/${retries})"
    kubectl -n "${namespace}" get pods -o wide || true
    sleep 10
  done
}

istio_proxy_image() {
  # Return the proxyv2 image tag that matches the installed Istio version.
  echo "docker.io/istio/proxyv2:${ISTIO_VERSION}"
}

install_istio() {
  ensure_istioctl
  wait_for_apiserver_ready

  echo "==> Istio version check"
  "${ISTIOCTL_BIN}" version --short --remote=false 2>/dev/null || true

  if ! kubectl -n istio-system get deploy istiod >/dev/null 2>&1; then
    echo "==> Installing Istio control plane"
    local profile="demo"
    local -a install_args=(--set "profile=${profile}")
    if [[ "${CI:-}" == "true" ]]; then
      # In CI, disable egress gateway and native sidecars. Native sidecars
      # (init containers with restartPolicy=Always) block app startup until
      # the proxy passes its startup probe; on resource-constrained Kind
      # nodes this causes pods to hang at Init:1/2 indefinitely.
      install_args+=(--set "components.egressGateways[0].enabled=false")
      install_args+=(--set "values.pilot.env.ENABLE_NATIVE_SIDECARS=false")
    fi
    local attempt
    for attempt in $(seq 1 "${ISTIO_INSTALL_RETRIES}"); do
      if "${ISTIOCTL_BIN}" install "${install_args[@]}" -y; then
        break
      fi
      if [[ "${attempt}" -eq "${ISTIO_INSTALL_RETRIES}" ]]; then
        echo "ERROR: Istio install failed after ${ISTIO_INSTALL_RETRIES} attempts"
        return 1
      fi
      echo "WARNING: Istio install failed (attempt ${attempt}/${ISTIO_INSTALL_RETRIES}); retrying"
      sleep 15
      wait_for_apiserver_ready 6 5 || true
    done
  fi

  wait_for_rollout_with_retries "istio-system" "deploy/istiod" "${ISTIO_ROLLOUT_TIMEOUT}" "${ISTIO_ROLLOUT_RETRIES}"
  wait_for_rollout_with_retries "istio-system" "deploy/istio-ingressgateway" "${ISTIO_ROLLOUT_TIMEOUT}" "${ISTIO_ROLLOUT_RETRIES}"
}

install_cert_manager_crds() {
  echo "==> Installing cert-manager CRDs (if needed)"
  if ! kubectl get crd certificates.cert-manager.io >/dev/null 2>&1; then
    kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.crds.yaml" || \
      echo "WARNING: Could not install cert-manager CRDs. Certificate resources will fail to apply."
  fi
}

install_prometheus_operator_crds() {
  local crd_base="https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/${PROMETHEUS_OPERATOR_VERSION}/example/prometheus-operator-crd"
  echo "==> Installing Prometheus Operator CRDs (if needed)"
  if ! kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
    kubectl apply --server-side -f "${crd_base}/monitoring.coreos.com_servicemonitors.yaml" || \
      echo "WARNING: Could not install ServiceMonitor CRD."
  fi
  if ! kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1; then
    kubectl apply --server-side -f "${crd_base}/monitoring.coreos.com_prometheusrules.yaml" || \
      echo "WARNING: Could not install PrometheusRule CRD."
  fi
}

create_ingress_tls_secrets() {
  local tmpdir cert key conf
  tmpdir="$(mktemp -d)"
  cert="${tmpdir}/tls.crt"
  key="${tmpdir}/tls.key"
  conf="${tmpdir}/openssl.cnf"

  cat >"${conf}" <<'EOF'
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = test.helloworld.shipshape.example.com

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = test.helloworld.shipshape.example.com
DNS.2 = prod.helloworld.shipshape.example.com
EOF

  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "${key}" -out "${cert}" -config "${conf}" -extensions v3_req

  # Istio's Gateway credentialName resolves secrets in the ingress gateway
  # pod's namespace (istio-system), not in the Gateway resource's namespace.
  kubectl -n istio-system create secret tls helloworld-test-tls \
    --cert="${cert}" --key="${key}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n istio-system create secret tls helloworld-prod-tls \
    --cert="${cert}" --key="${key}" --dry-run=client -o yaml | kubectl apply -f -

  rm -rf "${tmpdir}"
}

wait_for_ingress_response() {
  local host expected attempt
  host="$1"
  expected="$2"
  LAST_INGRESS_RESPONSE=""

  for attempt in $(seq 1 40); do
    if [[ "${INGRESS_USE_PORT_FORWARD}" == "true" ]]; then
      ensure_port_forward
    fi
    # Force host header without :8443 so VirtualService host matching works
    # when using a local 8443->443 port-forward tunnel.
    # Force HTTP/1.1 because HTTP/2 over kubectl port-forward can
    # intermittently reset streams and surface as empty ingress responses.
    LAST_INGRESS_RESPONSE="$(curl -sk --http1.1 --connect-timeout 3 --max-time 5 --noproxy '*' \
      -H "Host: ${host}" \
      --resolve "${host}:${INGRESS_HTTPS_PORT}:127.0.0.1" \
      "https://${host}:${INGRESS_HTTPS_PORT}/" || true)"
    if [[ "${LAST_INGRESS_RESPONSE}" == *"${expected}"* ]]; then
      return 0
    fi
    if [[ -z "${LAST_INGRESS_RESPONSE}" ]]; then
      if (( attempt % 5 == 1 )); then
        echo "  [attempt ${attempt}] diagnosing empty response for ${host}:" >&2
        curl -svk --http1.1 --connect-timeout 3 --max-time 5 --noproxy '*' \
          -H "Host: ${host}" \
          --resolve "${host}:${INGRESS_HTTPS_PORT}:127.0.0.1" \
          "https://${host}:${INGRESS_HTTPS_PORT}/" 2>&1 | head -40 >&2 || true
      elif [[ "${INGRESS_USE_PORT_FORWARD}" == "true" ]]; then
        echo "  [attempt ${attempt}] empty response for ${host}, recycling port-forward" >&2
      else
        echo "  [attempt ${attempt}] empty response for ${host}, retrying direct ingress" >&2
      fi
      if [[ "${INGRESS_USE_PORT_FORWARD}" == "true" ]]; then
        cleanup_port_forward
      fi
    fi
    sleep 2
  done

  echo "ERROR: Did not receive expected response for host ${host}" >&2
  echo "Last response: ${LAST_INGRESS_RESPONSE}" >&2
  return 1
}

print_diagnostics() {
  echo "==> Failure diagnostics"
  echo "--- Runner resources ---"
  if command -v nproc >/dev/null 2>&1; then
    nproc || true
  elif command -v sysctl >/dev/null 2>&1; then
    sysctl -n hw.logicalcpu 2>/dev/null | sed 's/^/logical CPUs: /' || true
  fi
  free -m 2>/dev/null || vm_stat 2>/dev/null || true
  df -h / || true
  echo "--- Port-forward status (PF_PID=${PF_PID:-unset}, PF_TARGET=${PF_TARGET:-unset}) ---"
  if [[ -n "${PF_PID}" ]]; then
    kill -0 "${PF_PID}" 2>/dev/null && echo "port-forward alive" || echo "port-forward DEAD"
  fi
  kubectl config current-context || true
  kubectl get nodes -o wide || true
  kubectl get pods -A || true

  echo "--- Istio ingress gateway logs (last 60 lines) ---"
  kubectl -n istio-system logs deploy/istio-ingressgateway --tail=60 || true

  echo "--- Istio ingress gateway proxy-config clusters ---"
  local gw_pod
  gw_pod="$(kubectl -n istio-system get pods -l istio=ingressgateway \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
  if [[ -n "${gw_pod}" ]]; then
    echo "-- Routes --"
    "${ISTIOCTL_BIN}" proxy-config route "${gw_pod}" -n istio-system 2>/dev/null || true
    echo "-- Clusters --"
    "${ISTIOCTL_BIN}" proxy-config cluster "${gw_pod}" -n istio-system 2>/dev/null | grep helloworld || true
    echo "-- Listeners --"
    "${ISTIOCTL_BIN}" proxy-config listener "${gw_pod}" -n istio-system 2>/dev/null || true
  fi

  echo "--- Direct in-cluster curl bypass (from ingress gateway to app svc) ---"
  if [[ -n "${gw_pod}" ]]; then
    kubectl -n istio-system exec "${gw_pod}" -- \
      curl -sv --connect-timeout 3 --max-time 5 \
        "http://helloworld-test.${NAMESPACE}.svc.cluster.local:80/" 2>&1 | head -30 || true
  fi

  echo "--- Istio PeerAuthentication / AuthorizationPolicy ---"
  kubectl -n "${NAMESPACE}" get peerauthentication,authorizationpolicy -o yaml 2>/dev/null || true

  if kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
    kubectl -n "${NAMESPACE}" get deploy,po,svc,cm,netpol || true
    kubectl -n "${NAMESPACE}" get gateway,virtualservice,certificate || true
    echo "--- Deployment descriptions ---"
    kubectl -n "${NAMESPACE}" describe deploy helloworld-test || true
    kubectl -n "${NAMESPACE}" describe deploy helloworld-prod || true
    kubectl -n "${NAMESPACE}" describe deploy helloworld-controller || true
    echo "--- Pod descriptions (all helloworld pods) ---"
    kubectl -n "${NAMESPACE}" describe pods -l app=helloworld || true
    echo "--- Pod container logs ---"
    for pod in $(kubectl -n "${NAMESPACE}" get pods -l app=helloworld -o name 2>/dev/null); do
      echo "--- Logs: ${pod} ---"
      kubectl -n "${NAMESPACE}" logs "${pod}" --all-containers --tail=100 || true
      echo "--- Previous logs: ${pod} ---"
      kubectl -n "${NAMESPACE}" logs "${pod}" --all-containers --previous --tail=50 2>/dev/null || true
    done
    echo "--- Namespace events (sorted by time) ---"
    kubectl -n "${NAMESPACE}" get events --sort-by='.lastTimestamp' || true
    echo "--- Controller logs ---"
    kubectl -n "${NAMESPACE}" logs deploy/helloworld-controller --tail=200 || true
  else
    echo "Namespace ${NAMESPACE} was not created before failure."
  fi
}

apply_ci_probe_allow_policies() {
  local first_node_ip node_cidr
  first_node_ip="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
  if [[ -z "${first_node_ip}" ]]; then
    echo "WARNING: Unable to resolve node InternalIP for CI probe allow policies"
    return 0
  fi

  node_cidr="${first_node_ip}/32"
  echo "==> Applying CI probe allow policies for node CIDR ${node_cidr}"
  sed "s|10.0.0.0/16|${node_cidr}|g" "${REPO_ROOT}/examples/networkpolicy-probe-allow/app-kubelet-probes.yaml" | kubectl apply -f -
  sed "s|10.0.0.0/16|${node_cidr}|g" "${REPO_ROOT}/examples/networkpolicy-probe-allow/controller-kubelet-probes.yaml" | kubectl apply -f -
}

resolve_apiserver_cidrs() {
  local service_ip endpoint_ips ip
  local -a cidrs=()

  service_ip="$(kubectl -n default get svc kubernetes -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
  if [[ -n "${service_ip}" ]]; then
    cidrs+=("${service_ip}/32")
  fi

  endpoint_ips="$(kubectl -n default get endpoints kubernetes -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
  for ip in ${endpoint_ips}; do
    [[ -n "${ip}" ]] || continue
    cidrs+=("${ip}/32")
  done

  if [[ "${#cidrs[@]}" -eq 0 ]]; then
    return 1
  fi

  printf '%s\n' "${cidrs[@]}" | awk '!seen[$0]++'
}

apply_controller_apiserver_cidr_policy() {
  local cidrs cidr_csv patch

  echo "==> Applying strict controller API egress CIDR policy"
  cidrs="$(resolve_apiserver_cidrs || true)"
  if [[ -z "${cidrs}" ]]; then
    echo "ERROR: Could not resolve Kubernetes API service/endpoints CIDRs for controller policy patch"
    return 1
  fi

  cidr_csv="$(echo "${cidrs}" | paste -sd ' ' -)"

  patch="$(
    APISERVER_CIDRS="${cidr_csv}" python3 - <<'PY'
import json
import os

cidrs = [entry for entry in os.environ["APISERVER_CIDRS"].split(" ") if entry]
if not cidrs:
    raise SystemExit("Missing API CIDRs")

dns_rule = {
    "to": [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        },
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "coredns"}},
        },
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "node-local-dns"}},
        },
    ],
    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
}

api_rule = {
    "to": [{"ipBlock": {"cidr": cidr}} for cidr in cidrs],
    "ports": [{"protocol": "TCP", "port": 443}],
}

print(json.dumps({"spec": {"egress": [dns_rule, api_rule]}}, separators=(",", ":")))
PY
  )"

  kubectl -n "${NAMESPACE}" patch networkpolicy helloworld-controller --type merge -p "${patch}"
  echo "  controller API CIDRs: ${cidr_csv}"
}

verify_dns_resolution() {
  echo "==> Verifying in-cluster DNS resolution from app pod"
  kubectl -n "${NAMESPACE}" exec deploy/helloworld-test -- python - <<'PY'
import socket
import sys

targets = (
    "kubernetes.default.svc.cluster.local",
    "istiod.istio-system.svc.cluster.local",
)

for host in targets:
    try:
        resolved = sorted({entry[4][0] for entry in socket.getaddrinfo(host, None)})
    except OSError as exc:
        print(f"DNS lookup failed for {host}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"{host} -> {', '.join(resolved)}")
PY
}

verify_controller_api_connectivity() {
  echo "==> Verifying controller API connectivity over service account auth"
  kubectl -n "${NAMESPACE}" exec deploy/helloworld-controller -- python - <<'PY'
import ssl
import urllib.request

token = open(
    "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "r",
    encoding="utf-8",
).read().strip()
request = urllib.request.Request(
    "https://kubernetes.default.svc/version",
    headers={"Authorization": f"Bearer {token}"},
)
context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
with urllib.request.urlopen(request, context=context, timeout=5) as response:
    print(f"controller->apiserver status={response.status}")
    if response.status != 200:
        raise SystemExit(1)
PY
}

deploy_all() {
  local default_timeout="300s"
  [[ "${CI:-}" == "true" ]] && default_timeout="600s"
  ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-${default_timeout}}"

  echo "==> Creating namespace"
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -k "${REPO_ROOT}/k8s/namespace"
  kubectl wait --for=jsonpath='{.status.phase}'=Active "namespace/${NAMESPACE}" --timeout=60s
  create_ingress_tls_secrets

  # Deploy and wait for each component sequentially to avoid overwhelming
  # the single Kind node with too many simultaneous Istio sidecar startups.

  echo "==> Deploying test overlay"
  kubectl apply -k "${REPO_ROOT}/k8s/overlays/test"

  echo "==> Deploying prod overlay"
  kubectl apply -k "${REPO_ROOT}/k8s/overlays/prod"

  echo "==> Deploying app monitoring resources"
  kubectl apply -k "${REPO_ROOT}/k8s/monitoring"

  echo "==> Deploying controller"
  kubectl apply -k "${REPO_ROOT}/k8s/controller"

  apply_controller_apiserver_cidr_policy

  if [[ "${CI:-}" == "true" ]]; then
    # Keep NetworkPolicy/AuthZ enabled in CI and add explicit kubelet probe
    # allow rules for CNIs that require node-origin probe allowances.
    apply_ci_probe_allow_policies

    echo "==> Reducing replicas for CI environment"
    kubectl -n "${NAMESPACE}" patch hpa helloworld-prod -p '{"spec":{"minReplicas":1}}'
    kubectl -n "${NAMESPACE}" scale deployment/helloworld-prod --replicas=1
    kubectl -n "${NAMESPACE}" scale deployment/helloworld-controller --replicas=1
  fi

  kubectl -n "${NAMESPACE}" set image deployment/helloworld-test helloworld="${APP_IMAGE_TEST}"
  kubectl -n "${NAMESPACE}" set image deployment/helloworld-prod helloworld="${APP_IMAGE_PROD}"
  kubectl -n "${NAMESPACE}" set image deployment/helloworld-controller controller="${CONTROLLER_IMAGE}"

  for deploy in helloworld-test helloworld-prod helloworld-controller; do
    echo "==> Waiting for ${deploy} rollout (timeout=${ROLLOUT_TIMEOUT})"
    kubectl -n "${NAMESPACE}" rollout status "deployment/${deploy}" --timeout="${ROLLOUT_TIMEOUT}"
  done

  verify_dns_resolution
  verify_controller_api_connectivity

  echo "==> All deployments rolled out"
  kubectl -n "${NAMESPACE}" get pods -o wide || true
}

wait_for_envoy_routes() {
  echo "==> Waiting for Envoy route propagation on ingress gateway"
  local gw_pod
  for _ in $(seq 1 60); do
    gw_pod="$(kubectl -n istio-system get pods -l istio=ingressgateway \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    if [[ -n "${gw_pod}" ]] && \
       "${ISTIOCTL_BIN}" proxy-config route "${gw_pod}" -n istio-system 2>/dev/null \
         | grep -q "test.helloworld"; then
      echo "==> Envoy routes propagated"
      return 0
    fi
    sleep 2
  done
  echo "WARNING: Envoy route propagation not confirmed within 120s"
}

start_port_forward_and_wait() {
  wait_for_envoy_routes

  echo "==> Verifying host-based Istio ingress routing (test host warm-up)"
  wait_for_ingress_response "test.helloworld.shipshape.example.com" "hello from test"
  echo "  ingress gateway reachable for test host"
}

verify_host_routing() {
  # Call directly (not in a subshell) so ensure_port_forward can manage PF_PID.
  wait_for_ingress_response "test.helloworld.shipshape.example.com" "hello from test"
  echo "Response from test host: ${LAST_INGRESS_RESPONSE}"

  wait_for_ingress_response "prod.helloworld.shipshape.example.com" "hello from prod"
  echo "Response from prod host: ${LAST_INGRESS_RESPONSE}"
}
