#!/usr/bin/env bash
# One-command local setup + full validation (CI-core checks + Kind e2e).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly SCRIPT_DIR
readonly REPO_ROOT

SKIP_BOOTSTRAP=0
SKIP_BOOTSTRAP_VERIFY=0
SKIP_E2E=0

usage() {
  cat <<'EOF'
Usage: ./scripts/quickstart.sh [--skip-bootstrap] [--skip-bootstrap-verify] [--skip-e2e]

Options:
  --skip-bootstrap         Skip running bootstrap.
  --skip-bootstrap-verify  Pass --skip-verify to bootstrap.
  --skip-e2e               Skip Kind end-to-end validation.
  -h, --help               Show this help text.
EOF
}

log() {
  printf '[quickstart] %s\n' "$*"
}

die() {
  printf '[quickstart] ERROR: %s\n' "$*" >&2
  exit 1
}

nix_cmd() {
  nix --extra-experimental-features "nix-command flakes" "$@"
}

while (($# > 0)); do
  case "$1" in
    --skip-bootstrap)
      SKIP_BOOTSTRAP=1
      ;;
    --skip-bootstrap-verify)
      SKIP_BOOTSTRAP_VERIFY=1
      ;;
    --skip-e2e)
      SKIP_E2E=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
  shift
done

case "$(uname -s)" in
  Linux*|Darwin*)
    ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    die "Windows shell detected. Run powershell -ExecutionPolicy Bypass -File .\\scripts\\quickstart.ps1"
    ;;
  *)
    log "Unrecognized platform: $(uname -s). Attempting setup anyway."
    ;;
esac

if [[ "${SKIP_BOOTSTRAP}" -eq 0 ]]; then
  log "Running bootstrap setup."
  if [[ "${SKIP_BOOTSTRAP_VERIFY}" -eq 1 ]]; then
    "${REPO_ROOT}/scripts/bootstrap-dev.sh" --skip-verify
  else
    "${REPO_ROOT}/scripts/bootstrap-dev.sh"
  fi
else
  log "Skipping bootstrap setup."
fi

if ! command -v nix >/dev/null 2>&1; then
  # First-time Nix install: bootstrap ran in a subprocess so PATH wasn't
  # propagated back.  Source the profile script to pick up nix in this shell.
  for _nix_profile in \
    "/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh" \
    "${HOME}/.nix-profile/etc/profile.d/nix.sh" \
    "${HOME}/.nix-profile/etc/profile.d/nix-daemon.sh"
  do
    if [[ -f "${_nix_profile}" ]]; then
      # shellcheck disable=SC1090
      source "${_nix_profile}"
      break
    fi
  done
  unset _nix_profile
fi

if ! command -v nix >/dev/null 2>&1; then
  die "nix is required but was not found on PATH after bootstrap. Start a new shell and retry."
fi

if [[ "${SKIP_E2E}" -eq 0 ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    log "docker CLI not found."
    if grep -qi microsoft /proc/version 2>/dev/null; then
      # shellcheck source=ensure-docker-wsl.sh
      source "${SCRIPT_DIR}/ensure-docker-wsl.sh"
      ensure_docker_wsl "quickstart"
    else
      die "docker CLI is required for Kind e2e. Install Docker and retry, or use --skip-e2e."
    fi
  fi
  if ! docker info >/dev/null 2>&1; then
    log "docker daemon is not reachable."
    if grep -qi microsoft /proc/version 2>/dev/null; then
      log "Running in WSL. Check Docker Desktop:"
      log "  1. Docker Desktop must be running on Windows"
      log "  2. Settings > Resources > WSL Integration > enable '$(hostname -f 2>/dev/null || echo "your distro")'"
      log "  3. Restart Docker Desktop after enabling"
    fi
    die "Start Docker and retry, or use --skip-e2e."
  fi
fi

log "Running CI-core checks (lint, typecheck, metadata, coverage, manifests)."
# shellcheck disable=SC2016
nix_cmd develop "${REPO_ROOT}" --command env REPO_ROOT="${REPO_ROOT}" bash -lc '
  set -euo pipefail
  cd "$REPO_ROOT"
  uv sync --extra dev
  if command -v make >/dev/null 2>&1; then
    make check-ci-core
  elif command -v gmake >/dev/null 2>&1; then
    gmake check-ci-core
  else
    echo "[quickstart] ERROR: make/gmake is required to run check-ci-core" >&2
    exit 1
  fi
'

if [[ "${SKIP_E2E}" -eq 0 ]]; then
  log "Running Kind end-to-end validation."
  # shellcheck disable=SC2016
  nix_cmd develop "${REPO_ROOT}" --command env REPO_ROOT="${REPO_ROOT}" bash -lc '
    set -euo pipefail
    cd "$REPO_ROOT"
    ./hack/e2e-kind.sh
  '
else
  log "Skipping end-to-end validation."
fi

log "Completed successfully."
