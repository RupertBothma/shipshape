#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=hack/e2e-lib.sh
source "${SCRIPT_DIR}/e2e-lib.sh"
cd "${REPO_ROOT}"

trap cleanup_port_forward EXIT
trap print_diagnostics ERR

echo "==> Validating rendered manifest invariants"
validate_rendered_manifests

wait_for_apiserver_ready
install_istio
install_cert_manager_crds
install_prometheus_operator_crds

echo "==> Building Docker images into Minikube"
eval "$(minikube docker-env)"
docker build -f "${REPO_ROOT}/app/Dockerfile" -t "${APP_IMAGE_TEST}" "${REPO_ROOT}"
docker tag "${APP_IMAGE_TEST}" "${APP_IMAGE_PROD}"
docker build -f "${REPO_ROOT}/controller/Dockerfile" -t "${CONTROLLER_IMAGE}" "${REPO_ROOT}"

wait_for_apiserver_ready
deploy_all
start_port_forward_and_wait
verify_host_routing

echo "==> E2E Test: Patching prod ConfigMap"
kubectl -n "${NAMESPACE}" patch configmap helloworld-config-prod --type merge \
  -p '{"data":{"MESSAGE":"hello from prod update"}}'

echo "==> Waiting for prod deployment rollout after ConfigMap change"
sleep 5
kubectl -n "${NAMESPACE}" rollout status deployment/helloworld-prod --timeout=180s

echo "==> Verifying updated ingress response"
wait_for_ingress_response "prod.helloworld.shipshape.example.com" "hello from prod update"
echo "Updated response from prod host: ${LAST_INGRESS_RESPONSE}"

echo "==> Final state"
kubectl -n "${NAMESPACE}" get deployments -l app=helloworld -o wide

echo "Minikube e2e checks completed successfully."
