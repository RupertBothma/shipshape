#!/usr/bin/env bash
# WSL-only helper: installs Docker Desktop on the Windows host if missing.
# Sourced by quickstart.sh when running inside WSL without docker.
# No-ops silently on native Linux/macOS — callers need not guard.

set -euo pipefail

ensure_docker_wsl() {
  # Only relevant inside WSL
  if ! grep -qi microsoft /proc/version 2>/dev/null; then
    return 1
  fi

  local log_prefix="${1:-setup}"

  _log() { printf '[%s] %s\n' "${log_prefix}" "$*"; }
  _die() { printf '[%s] ERROR: %s\n' "${log_prefix}" "$*" >&2; exit 1; }

  if command -v docker >/dev/null 2>&1; then
    return 0
  fi

  _log "Running in WSL — attempting to install Docker Desktop on the Windows host."

  if ! command -v winget.exe >/dev/null 2>&1; then
    _log "winget.exe not available from WSL."
    _log "Install Docker Desktop on Windows manually: https://www.docker.com/products/docker-desktop/"
    _log "Then enable WSL integration:"
    _log "  Settings > Resources > WSL Integration > enable '$(hostname -f 2>/dev/null || echo "your distro")'"
    _die "docker CLI is required for Kind e2e. Install Docker and retry, or use --skip-e2e."
  fi

  _log "Installing Docker Desktop via winget..."
  winget.exe install -e --id Docker.DockerDesktop \
    --accept-source-agreements --accept-package-agreements || true

  local docker_exe="/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe"
  if [[ ! -f "${docker_exe}" ]]; then
    _log "winget completed but Docker Desktop executable not found."
    _log "A reboot or log-out/log-in may be required. Re-run after restarting."
    _die "docker CLI is required for Kind e2e. Use --skip-e2e to skip."
  fi

  _log "Docker Desktop installed. Starting it..."
  cmd.exe /C "start \"\" \"C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe\"" 2>/dev/null || true
  _log "Waiting for Docker Desktop to start and expose the CLI to WSL..."
  _log "This may take 1-2 minutes on first launch."

  local timeout=120
  local elapsed=0
  while (( elapsed < timeout )); do
    sleep 5
    elapsed=$(( elapsed + 5 ))
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
      break
    fi
    if (( elapsed % 15 == 0 )); then
      _log "Still waiting for Docker Desktop... (${elapsed}s)"
    fi
  done

  if command -v docker >/dev/null 2>&1; then
    _log "Docker CLI is now available in WSL."
    return 0
  fi

  _log "Docker CLI not yet visible in WSL."
  _log "You may need to enable WSL integration in Docker Desktop:"
  _log "  Settings > Resources > WSL Integration > enable '$(hostname -f 2>/dev/null || echo "your distro")'"
  _log "Then restart Docker Desktop and re-run this script."
  _die "docker CLI is required for Kind e2e. Use --skip-e2e to skip."
}
