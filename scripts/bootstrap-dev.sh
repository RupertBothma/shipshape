#!/usr/bin/env bash
# Bootstrap local development for Shipshape.
# - Installs Nix when missing.
# - Enters the flake dev shell and installs Python dependencies.
# - Runs local manifest/tooling sanity checks.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly SCRIPT_DIR
readonly REPO_ROOT

SKIP_VERIFY=0
NIX_PROFILE_SCRIPT=""

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap-dev.sh [--skip-verify]

Options:
  --skip-verify   Skip post-setup sanity checks.
  -h, --help      Show this help text.
EOF
}

log() {
  printf '[bootstrap] %s\n' "$*"
}

die() {
  printf '[bootstrap] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

print_darwin_volume_repair_hint() {
  cat <<'EOF' >&2
[bootstrap] Detected a stale macOS "Nix Store" APFS volume from a previous install.
[bootstrap] Run these commands, then re-run ./scripts/bootstrap-dev.sh:
[bootstrap]   sudo launchctl bootout system/org.nixos.darwin-store || true
[bootstrap]   sudo launchctl bootout system/org.nixos.nix-daemon || true
[bootstrap]   sudo diskutil apfs deleteVolume "Nix Store"
EOF
}

is_darwin_stale_volume_error() {
  local installer_log="$1"
  [[ "$(uname -s)" == "Darwin" ]] && grep -q 'keychain lacks a password for the already existing "Nix Store" volume' "${installer_log}"
}

repair_darwin_nix_store_volume() {
  require_cmd sudo
  require_cmd diskutil
  require_cmd launchctl

  log "Attempting automatic cleanup of stale macOS Nix Store volume."
  sudo launchctl bootout system/org.nixos.darwin-store || true
  sudo launchctl bootout system/org.nixos.nix-daemon || true
  sudo diskutil apfs deleteVolume "Nix Store"
}

run_determinate_installer() {
  local installer_log="$1"
  sh <(curl --proto '=https' --tlsv1.2 -fsSL https://install.determinate.systems/nix) install --no-confirm 2>&1 | tee "${installer_log}"
}

load_nix_into_path() {
  local profile_script
  for profile_script in \
    "/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh" \
    "${HOME}/.nix-profile/etc/profile.d/nix.sh" \
    "${HOME}/.nix-profile/etc/profile.d/nix-daemon.sh"
  do
    if [[ -f "${profile_script}" ]]; then
      NIX_PROFILE_SCRIPT="${profile_script}"
      # shellcheck disable=SC1090
      source "${profile_script}"
      return 0
    fi
  done
  return 1
}

persist_nix_shell_init() {
  local profile_script="$1"
  local rc_file
  for rc_file in \
    "${HOME}/.profile" \
    "${HOME}/.zprofile" \
    "${HOME}/.zshrc" \
    "${HOME}/.bash_profile" \
    "${HOME}/.bash_login" \
    "${HOME}/.bashrc"
  do
    touch "${rc_file}"
    if grep -Fq "${profile_script}" "${rc_file}" || grep -Fq "shipshape-nix-bootstrap" "${rc_file}"; then
      continue
    fi
    cat >> "${rc_file}" <<EOF

# >>> shipshape-nix-bootstrap >>>
if [ -e '${profile_script}' ]; then
  . '${profile_script}'
fi
# <<< shipshape-nix-bootstrap <<<
EOF
    log "Added Nix profile init to ${rc_file}"
  done
}

ensure_nix_shell_persistence() {
  if [[ -n "${NIX_PROFILE_SCRIPT}" ]]; then
    persist_nix_shell_init "${NIX_PROFILE_SCRIPT}"
  fi
}

ensure_nix_experimental_features() {
  local nix_config_dir="${HOME}/.config/nix"
  local nix_config_file="${nix_config_dir}/nix.conf"

  mkdir -p "${nix_config_dir}"
  touch "${nix_config_file}"

  if grep -Fq "shipshape-nix-bootstrap-features" "${nix_config_file}"; then
    return 0
  fi

  if grep -Eq '^[[:space:]]*(extra-)?experimental-features[[:space:]]*=.*\bnix-command\b' "${nix_config_file}" \
    && grep -Eq '^[[:space:]]*(extra-)?experimental-features[[:space:]]*=.*\bflakes\b' "${nix_config_file}"; then
    return 0
  fi

  cat >> "${nix_config_file}" <<'EOF'

# >>> shipshape-nix-bootstrap-features >>>
extra-experimental-features = nix-command flakes
# <<< shipshape-nix-bootstrap-features <<<
EOF
  log "Enabled nix-command/flakes in ${nix_config_file}"
}

finalize_nix_setup() {
  if ! command -v nix >/dev/null 2>&1; then
    load_nix_into_path || true
  fi
  ensure_nix_shell_persistence
  ensure_nix_experimental_features
  command -v nix >/dev/null 2>&1 || die "Nix install finished but 'nix' is still unavailable. Start a new shell, then re-run this script."
}

install_nix_if_needed() {
  if command -v nix >/dev/null 2>&1; then
    load_nix_into_path || true
    finalize_nix_setup
    return 0
  fi

  if load_nix_into_path && command -v nix >/dev/null 2>&1; then
    finalize_nix_setup
    return 0
  fi

  require_cmd curl
  log "Nix not found. Installing via Determinate Systems installer."
  local installer_log
  installer_log="$(mktemp)"
  if ! run_determinate_installer "${installer_log}"; then
    if is_darwin_stale_volume_error "${installer_log}"; then
      if repair_darwin_nix_store_volume; then
        log "Retrying Nix installation after APFS cleanup."
        if run_determinate_installer "${installer_log}"; then
          rm -f "${installer_log}"
          finalize_nix_setup
          return 0
        fi
      fi
      print_darwin_volume_repair_hint
    fi
    rm -f "${installer_log}"
    cat <<'EOF' >&2
[bootstrap] ERROR: Automatic Nix installation failed.
[bootstrap] On a clean machine, this step usually requires interactive sudo/admin approval.
[bootstrap] Re-run this script in a normal interactive terminal session.
EOF
    exit 1
  fi
  rm -f "${installer_log}"

  finalize_nix_setup
}

nix_cmd() {
  nix --extra-experimental-features "nix-command flakes" "$@"
}

while (($# > 0)); do
  case "$1" in
    --skip-verify)
      SKIP_VERIFY=1
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
    die "Windows shell detected. Run powershell -ExecutionPolicy Bypass -File .\\scripts\\bootstrap-dev.ps1"
    ;;
  *)
    log "Unrecognized platform: $(uname -s). Attempting setup anyway."
    ;;
esac

install_nix_if_needed

ensure_nix_daemon() {
  # After a fresh install the nix-daemon may not be running yet — especially
  # in WSL where systemd may not have been enabled at install time.
  if [[ -S /nix/var/nix/daemon-socket/socket ]]; then
    return 0
  fi

  log "Nix daemon socket not found. Attempting to start nix-daemon..."

  if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running &>/dev/null; then
    sudo systemctl start nix-daemon 2>/dev/null || true
  else
    # No systemd — start the daemon directly in the background.
    sudo /nix/var/nix/profiles/default/bin/nix-daemon &>/dev/null &
  fi

  local timeout=30
  local elapsed=0
  while [[ ! -S /nix/var/nix/daemon-socket/socket ]] && (( elapsed < timeout )); do
    sleep 1
    elapsed=$(( elapsed + 1 ))
  done

  if [[ ! -S /nix/var/nix/daemon-socket/socket ]]; then
    die "Nix daemon failed to start (socket not found after ${timeout}s). Restart your shell and retry."
  fi
  log "Nix daemon is running."
}

ensure_nix_daemon

log "Resolving flake metadata."
nix_cmd flake metadata "${REPO_ROOT}" >/dev/null

log "Installing Python dependencies using uv in the Nix dev shell."
# shellcheck disable=SC2016
nix_cmd develop "${REPO_ROOT}" --command env REPO_ROOT="${REPO_ROOT}" bash -lc '
  set -euo pipefail
  cd "$REPO_ROOT"
  uv sync --extra dev
'

if [[ "${SKIP_VERIFY}" -eq 0 ]]; then
  log "Running post-setup sanity checks."
  # shellcheck disable=SC2016
  nix_cmd develop "${REPO_ROOT}" --command env REPO_ROOT="${REPO_ROOT}" bash -lc '
    set -euo pipefail
    cd "$REPO_ROOT"
    uv run python -c "import fastapi, kubernetes, prometheus_client"
    uv run python hack/validate_manifests.py --overlay test --overlay prod
    uv run python hack/check_immutable_images.py
    uv run python hack/check_doc_links.py
    uv run python hack/validate_trivyignore.py
    uv run python hack/validate_deployment_order.py
  '
fi

cat <<'EOF'
[bootstrap] Setup complete.
[bootstrap] Next commands:
[bootstrap]   nix develop
[bootstrap]   make check
[bootstrap] If this terminal still cannot find 'nix', run:
[bootstrap]   source /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
EOF
