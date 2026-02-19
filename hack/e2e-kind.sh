#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=hack/e2e-lib.sh
source "${SCRIPT_DIR}/e2e-lib.sh"
cd "${REPO_ROOT}"

trap cleanup_port_forward EXIT
trap print_diagnostics ERR

CLUSTER_NAME="${CLUSTER_NAME:-shipshape}"
MIN_KIND_VERSION="${MIN_KIND_VERSION:-v0.27.0}"
KIND_INGRESS_HTTPS_NODEPORT="${KIND_INGRESS_HTTPS_NODEPORT:-30443}"
KIND_INGRESS_HOST_HTTPS_PORT="${KIND_INGRESS_HOST_HTTPS_PORT:-8443}"

ensure_kind_version() {
  local installed required
  installed="$(kind version | awk '{print $2}')"
  if [[ -z "${installed}" ]]; then
    echo "ERROR: Unable to determine kind version"
    exit 1
  fi

  installed="${installed#v}"
  required="${MIN_KIND_VERSION#v}"
  if [[ "$(printf '%s\n%s\n' "${required}" "${installed}" | sort -V | head -n1)" != "${required}" ]]; then
    echo "ERROR: kind v${installed} is too old. Require >= ${MIN_KIND_VERSION} for containerd 2.x node images."
    exit 1
  fi
}

create_kind_config_with_ingress_mapping() {
  local cfg
  cfg="$(mktemp)"
  cat >"${cfg}" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: ${KIND_INGRESS_HTTPS_NODEPORT}
        hostPort: ${KIND_INGRESS_HOST_HTTPS_PORT}
        protocol: TCP
EOF
  echo "${cfg}"
}

configure_kind_ingress_nodeport() {
  echo "==> Configuring Istio ingress NodePort ${KIND_INGRESS_HTTPS_NODEPORT} for direct host probes"
  local current patch
  current="$(kubectl -n istio-system get svc istio-ingressgateway -o json)"
  patch="$(
    KIND_INGRESS_HTTPS_NODEPORT="${KIND_INGRESS_HTTPS_NODEPORT}" python3 -c '
import json
import os
import sys

svc = json.load(sys.stdin)
ports = svc.get("spec", {}).get("ports", [])
nodeport = int(os.environ["KIND_INGRESS_HTTPS_NODEPORT"])

matched = False
for port in ports:
    if port.get("port") == 443 and port.get("protocol", "TCP") == "TCP":
        port["nodePort"] = nodeport
        matched = True
        break

if not matched:
    raise SystemExit("ERROR: Could not find TCP service port 443 on istio-ingressgateway")

print(json.dumps({"spec": {"type": "NodePort", "ports": ports}}, separators=(",", ":")))
' <<<"${current}"
  )"
  kubectl -n istio-system patch svc istio-ingressgateway --type merge -p "${patch}"
  kubectl -n istio-system get svc istio-ingressgateway -o wide
  kubectl -n istio-system get svc istio-ingressgateway \
    -o jsonpath='{range .spec.ports[*]}{.name}:{.port}->{.targetPort} nodePort={.nodePort}{"\n"}{end}'
}

ensure_registry_image_on_kind_nodes() {
  local image="$1"
  local node node_count=0

  while IFS= read -r node; do
    [[ -n "${node}" ]] || continue
    (( ++node_count ))
    if docker exec --privileged "${node}" ctr --namespace=k8s.io images ls --quiet | grep -Fxq "${image}"; then
      echo "  ${node}: ${image} already present"
      continue
    fi
    echo "  ${node}: pulling ${image} with node containerd"
    docker exec --privileged "${node}" ctr --namespace=k8s.io images pull "${image}" >/dev/null
  done < <(kind get nodes --name "${CLUSTER_NAME}")

  if (( node_count == 0 )); then
    echo "ERROR: No nodes found for kind cluster ${CLUSTER_NAME}"
    return 1
  fi
}

echo "==> Validating rendered manifest invariants"
validate_rendered_manifests

echo "==> Verifying kind version compatibility"
ensure_kind_version

echo "==> Creating Kind cluster (if needed)"
if ! kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  kind_args=(--name "${CLUSTER_NAME}" --image "kindest/node:${K8S_VERSION}" --wait 180s)
  if [[ "${CI:-}" == "true" ]]; then
    echo "==> CI mode: enabling direct ingress via Kind host port ${KIND_INGRESS_HOST_HTTPS_PORT}"
    KIND_CFG="$(create_kind_config_with_ingress_mapping)"
    kind_args+=(--config "${KIND_CFG}")
  fi
  kind create cluster "${kind_args[@]}"
  [[ -z "${KIND_CFG:-}" ]] || rm -f "${KIND_CFG}"
fi

echo "==> Selecting kubectl context kind-${CLUSTER_NAME}"
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null
echo "==> Waiting for Kind nodes to become Ready"
kubectl wait --for=condition=Ready nodes --all --timeout=180s

if [[ "${CI:-}" == "true" ]]; then
  # In CI, avoid kubectl port-forward flakiness by probing ingress directly via
  # Kind host port mapping + fixed Istio NodePort.
  export INGRESS_USE_PORT_FORWARD="false"
  export INGRESS_HTTPS_PORT="${KIND_INGRESS_HOST_HTTPS_PORT}"
fi

wait_for_apiserver_ready
install_istio
if [[ "${CI:-}" == "true" ]]; then
  configure_kind_ingress_nodeport
fi
install_cert_manager_crds
install_prometheus_operator_crds

if [[ "${SKIP_IMAGE_BUILD:-false}" == "true" ]]; then
  echo "==> Using pre-built images (SKIP_IMAGE_BUILD=true)"
else
  echo "==> Building Docker images"
  docker build -f "${REPO_ROOT}/app/Dockerfile" -t "${APP_IMAGE_TEST}" "${REPO_ROOT}"
  docker tag "${APP_IMAGE_TEST}" "${APP_IMAGE_PROD}"
  docker build -f "${REPO_ROOT}/controller/Dockerfile" -t "${CONTROLLER_IMAGE}" "${REPO_ROOT}"
fi

echo "==> Ensuring Istio sidecar image on Kind nodes"
ISTIO_PROXY="$(istio_proxy_image)"
ensure_registry_image_on_kind_nodes "${ISTIO_PROXY}"

echo "==> Loading images into Kind cluster"
kind load docker-image "${APP_IMAGE_TEST}" --name "${CLUSTER_NAME}"
kind load docker-image "${APP_IMAGE_PROD}" --name "${CLUSTER_NAME}"
kind load docker-image "${CONTROLLER_IMAGE}" --name "${CLUSTER_NAME}"

wait_for_apiserver_ready
deploy_all
start_port_forward_and_wait
verify_host_routing

echo "==> E2E Test: Patching test ConfigMap"
kubectl -n "${NAMESPACE}" patch configmap helloworld-config-test --type merge \
  -p '{"data":{"MESSAGE":"hello from test update"}}'

echo "==> Waiting for test deployment rollout after ConfigMap change"
sleep 5
kubectl -n "${NAMESPACE}" rollout status deployment/helloworld-test --timeout=180s

echo "==> Verifying controller restart annotation"
annotation=$(kubectl -n "${NAMESPACE}" get deployment helloworld-test \
  -o jsonpath='{.spec.template.metadata.annotations.shipshape\.io/restartedAt}')
if [[ -z "${annotation}" ]]; then
  echo "ERROR: Controller did not set restart annotation"
  exit 1
fi
echo "==> Controller annotation verified: ${annotation}"

echo "==> Verifying updated ingress response"
wait_for_ingress_response "test.helloworld.shipshape.example.com" "hello from test update"
echo "Updated response from test host: ${LAST_INGRESS_RESPONSE}"

echo "==> Final state"
kubectl -n "${NAMESPACE}" get deployments -l app=helloworld -o wide
kubectl -n "${NAMESPACE}" get configmaps -l app=helloworld

echo "Kind e2e checks completed successfully."
