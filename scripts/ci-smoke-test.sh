#!/bin/bash
set -e

# This script simulates a CI run. 
# It sets up a fresh registry on a non-standard port to avoid conflicts,
# builds images, pushes them, and generates the immutable manifests.
# Finally, it tears down the registry.

echo "=== STARTING CI SMOKE TEST ==="

# 1. Randomize Port and Name to ensure no conflicts on a shared machine/CI runner
CI_ID="ci-$(date +%s)"
export REGISTRY_NAME="registry-$CI_ID"
# Find a free port between 10000 and 11000
export REGISTRY_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')

echo "CI ID: $CI_ID"
echo "Using Registry Port: $REGISTRY_PORT"

# 2. Start Registry
echo "--- Starting Registry ---"
./scripts/setup-local-registry.sh

# Cleanup function to run on exit
cleanup() {
    echo "--- Teardown ---"
    echo "Stopping registry container $REGISTRY_NAME..."
    docker rm -f $REGISTRY_NAME >/dev/null
    echo "=== CI SMOKE TEST COMPLETED ==="
}
trap cleanup EXIT

# 3. Build and Push Images (Immutable Mode)
echo "--- Building & Pushing Images (Immutable Mode) ---"
# We reference the script directly. 
# Note: localhost:$REGISTRY_PORT is accessible from the host.
./scripts/manage-images.sh --registry "localhost:$REGISTRY_PORT" --push --immutable

# 4. Verification
echo "--- Verifying Kustomize Output ---"

# Check if kustomization.yaml contains the digest (sha256)
if grep -q "sha256:" k8s/base/kustomization.yaml; then
    echo "✅ App Image digest found in k8s/base/kustomization.yaml"
else
    echo "❌ App Image digest MISSING in k8s/base/kustomization.yaml"
    exit 1
fi

if grep -q "sha256:" k8s/controller/kustomization.yaml; then
    echo "✅ Controller Image digest found in k8s/controller/kustomization.yaml"
else
    echo "❌ Controller Image digest MISSING in k8s/controller/kustomization.yaml"
    exit 1
fi

echo "--- Dry Run Deployment Check ---"
# Verify that `kustomize build` succeeds
kustomize build k8s/overlays/prod > /dev/null
if [ $? -eq 0 ]; then
    echo "✅ 'kustomize build k8s/overlays/prod' succeeded"
else
    echo "❌ 'kustomize build k8s/overlays/prod' FAILED"
    exit 1
fi

echo "✅ Smoke Test Passed!"
