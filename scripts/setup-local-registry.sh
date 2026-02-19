#!/bin/bash
set -e

# Configuration
REGISTRY_NAME="${REGISTRY_NAME:-registry}"
DEFAULT_PORT="${REGISTRY_PORT:-5000}"
STATE_FILE=".local-registry-env"

find_free_port() {
    local port=$1
    while lsof -Pi :$port -sTCP:LISTEN -t >/dev/null; do
        port=$((port + 1))
    done
    echo $port
}

echo "Checking for existing registry..."

CURRENT_PORT=""

# Check if a container with the name already exists
if [ "$(docker ps -aq -f name=^/${REGISTRY_NAME}$)" ]; then
    if [ "$(docker ps -q -f name=^/${REGISTRY_NAME}$)" ]; then
        echo "Registry '${REGISTRY_NAME}' is already running."
    else
        echo "Registry '${REGISTRY_NAME}' exists but is stopped. Starting it..."
        docker start ${REGISTRY_NAME} >/dev/null
    fi
    # Inspect the running container to find its actual mapped port
    CURRENT_PORT=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "5000/tcp") 0).HostPort}}' ${REGISTRY_NAME})
else
    # Find a free port starting from DEFAULT_PORT
    echo "Finding free port starting from ${DEFAULT_PORT}..."
    CURRENT_PORT=$(find_free_port ${DEFAULT_PORT})
    
    echo "Starting local registry '${REGISTRY_NAME}' on port ${CURRENT_PORT}..."
    docker run -d -p ${CURRENT_PORT}:5000 --restart=always --name ${REGISTRY_NAME} registry:2 >/dev/null
fi

# Connect to kind network if it exists (for local Kubernetes access)
if docker network inspect kind >/dev/null 2>&1; then
    # Check if already connected
    if ! docker network inspect kind | grep -q "${REGISTRY_NAME}"; then
        echo "Connecting registry to 'kind' network..."
        docker network connect kind ${REGISTRY_NAME} || true
    fi
fi

REGISTRY_URL="localhost:${CURRENT_PORT}"
echo "Registry available at ${REGISTRY_URL}"

# Persist to state file for other scripts/Makefile to consume
echo "REGISTRY_URL=${REGISTRY_URL}" > "${STATE_FILE}"
echo "Saved registry configuration to ${STATE_FILE}"
