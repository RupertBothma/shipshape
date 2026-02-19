#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

NAMESPACE="${NAMESPACE:-shipshape}"
DELETE_NAMESPACE="true"
DRY_RUN="false"
STOP_TILT="true"
STOP_SKAFFOLD="true"
DELETE_KIND_CLUSTER="false"
DELETE_MINIKUBE_PROFILE="false"
KIND_CLUSTER_NAME="${CLUSTER_NAME:-shipshape}"
MINIKUBE_PROFILE="${MINIKUBE_PROFILE:-minikube}"

usage() {
  cat <<'EOF'
Usage: ./hack/cleanup-local-dev.sh [options]

Removes local Shipshape dev resources from the current Kubernetes context.

Options:
  --keep-namespace            Keep the namespace; remove workloads and quota/limits only
  --dry-run                   Print commands without executing them
  --skip-tilt                 Do not run 'tilt down'
  --skip-skaffold             Do not run 'skaffold delete'
  --delete-kind-cluster       Also delete a Kind cluster
  --kind-cluster-name <name>  Kind cluster name (default: shipshape)
  --delete-minikube-profile   Also delete a Minikube profile
  --minikube-profile <name>   Minikube profile name (default: minikube)
  -h, --help                  Show this help text

Examples:
  ./hack/cleanup-local-dev.sh
  ./hack/cleanup-local-dev.sh --keep-namespace
  ./hack/cleanup-local-dev.sh --delete-kind-cluster --kind-cluster-name shipshape
  ./hack/cleanup-local-dev.sh --dry-run
EOF
}

log() {
  echo "==> $*"
}

warn() {
  echo "WARNING: $*" >&2
}

run_or_warn() {
  printf "+"; printf " %q" "$@"; printf "\n"
  [[ "${DRY_RUN}" == "true" ]] && return 0
  "$@" || warn "Command failed (continuing)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-namespace)
      DELETE_NAMESPACE="false"
      shift
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --skip-tilt)
      STOP_TILT="false"
      shift
      ;;
    --skip-skaffold)
      STOP_SKAFFOLD="false"
      shift
      ;;
    --delete-kind-cluster)
      DELETE_KIND_CLUSTER="true"
      shift
      ;;
    --kind-cluster-name)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --kind-cluster-name requires a value" >&2
        exit 1
      fi
      KIND_CLUSTER_NAME="$2"
      shift 2
      ;;
    --delete-minikube-profile)
      DELETE_MINIKUBE_PROFILE="true"
      shift
      ;;
    --minikube-profile)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --minikube-profile requires a value" >&2
        exit 1
      fi
      MINIKUBE_PROFILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

run_if_enabled() {
  local enabled="$1" tool="$2" label="$3"
  shift 3
  [[ "${enabled}" == "true" ]] || return 0
  if ! command -v "${tool}" >/dev/null 2>&1; then
    warn "${tool} is not installed; skipping"
    return 0
  fi
  log "${label}"
  run_or_warn "${tool}" "$@"
}

run_if_enabled "${STOP_TILT}" tilt "Stopping Tilt" down
run_if_enabled "${STOP_SKAFFOLD}" skaffold "Running Skaffold cleanup" delete

if command -v kubectl >/dev/null 2>&1 && kubectl version --request-timeout=5s >/dev/null 2>&1; then
  log "Deleting Shipshape manifests in reverse deployment order"
  for kustomize_dir in k8s/controller k8s/monitoring k8s/overlays/prod k8s/overlays/test k8s/istio-ingress; do
    run_or_warn kubectl delete -k "${kustomize_dir}" --ignore-not-found=true --wait=false
  done

  if [[ "${DELETE_NAMESPACE}" == "true" ]]; then
    log "Deleting namespace '${NAMESPACE}'"
    run_or_warn kubectl delete namespace "${NAMESPACE}" --ignore-not-found=true --wait=false
  else
    log "Keeping namespace '${NAMESPACE}'; removing quotas/limits only"
    run_or_warn kubectl -n "${NAMESPACE}" delete limitrange shipshape-defaults --ignore-not-found=true
    run_or_warn kubectl -n "${NAMESPACE}" delete resourcequota shipshape-quota --ignore-not-found=true
  fi

  log "Deleting local E2E ingress TLS secrets (if present)"
  run_or_warn kubectl -n istio-system delete secret helloworld-test-tls helloworld-prod-tls --ignore-not-found=true
else
  warn "kubectl is unavailable or cluster is unreachable; skipping Kubernetes resource cleanup"
fi

if [[ "${DELETE_KIND_CLUSTER}" == "true" ]]; then
  if command -v kind >/dev/null 2>&1; then
    log "Deleting Kind cluster '${KIND_CLUSTER_NAME}'"
    run_or_warn kind delete cluster --name "${KIND_CLUSTER_NAME}"
  else
    warn "kind is not installed; skipping Kind cluster deletion"
  fi
fi

if [[ "${DELETE_MINIKUBE_PROFILE}" == "true" ]]; then
  if command -v minikube >/dev/null 2>&1; then
    log "Deleting Minikube profile '${MINIKUBE_PROFILE}'"
    run_or_warn minikube delete -p "${MINIKUBE_PROFILE}"
  else
    warn "minikube is not installed; skipping Minikube profile deletion"
  fi
fi

log "Cleanup completed"
