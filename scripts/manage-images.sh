#!/bin/bash
set -e

# Usage: ./manage-images.sh [--registry <url>] [--push] [--immutable]

# Attempt to load registry from environment file if exists
if [ -f ".local-registry-env" ]; then
    source .local-registry-env
fi

# Default to loaded value or localhost:5000
REGISTRY="${REGISTRY_URL:-localhost:5000}"
PUSH=false
IMMUTABLE=false

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --registry) REGISTRY="$2"; shift ;;
        --push) PUSH=true ;;
        --immutable) IMMUTABLE=true ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

echo "Using registry: $REGISTRY"
echo "Immutable references: $IMMUTABLE"

APP_IMAGE_NAME="shipshape-helloworld"
CONTROLLER_IMAGE_NAME="shipshape-controller"

APP_FULL_IMAGE="$REGISTRY/$APP_IMAGE_NAME"
CONTROLLER_FULL_IMAGE="$REGISTRY/$CONTROLLER_IMAGE_NAME"

# Use a timestamp/git hash for unique tagging to avoid local cache collision issues during builds
TAG="dev-$(date +%s)"
if git rev-parse --short HEAD >/dev/null 2>&1; then
    TAG="$(git rev-parse --short HEAD)-$(date +%s)"
fi

# Build
echo "Building App image: $APP_FULL_IMAGE:$TAG"
docker build -q -f app/Dockerfile -t "$APP_FULL_IMAGE:$TAG" -t "$APP_FULL_IMAGE:latest" .

echo "Building Controller image: $CONTROLLER_FULL_IMAGE:$TAG"
docker build -q -f controller/Dockerfile -t "$CONTROLLER_FULL_IMAGE:$TAG" -t "$CONTROLLER_FULL_IMAGE:latest" .

if [ "$PUSH" = true ]; then
    echo "Pushing images to $REGISTRY..."
    docker push "$APP_FULL_IMAGE:$TAG" >/dev/null
    docker push "$APP_FULL_IMAGE:latest" >/dev/null
    docker push "$CONTROLLER_FULL_IMAGE:$TAG" >/dev/null
    docker push "$CONTROLLER_FULL_IMAGE:latest" >/dev/null
fi

echo "Updating Kustomize configuration..."

# Function to get digest
get_digest() {
    local image=$1
    if [ "$PUSH" = true ]; then
        # If we pushed, we can pull the digest from the registry which is most reliable
        docker inspect --format='{{index .RepoDigests 0}}' "$image" | cut -d'@' -f2
    else
        # If local only, use the image ID (not a true repo digest, but works for local docker loading)
        # However, kustomize 'digest' field usually expects a repo digest. 
        # For local-only immutable dev, it's tricky. We usually fall back to tags.
        # But if the user asked for immutable, we assume they want the precise SHA identifier.
        docker inspect --format='{{.Id}}' "$image"
    fi
}

APP_REF="$APP_FULL_IMAGE:$TAG"
CONTROLLER_REF="$CONTROLLER_FULL_IMAGE:$TAG"

if [ "$IMMUTABLE" = true ]; then
    echo "Resolving image digests for immutability..."
    APP_DIGEST=$(get_digest "$APP_FULL_IMAGE:$TAG")
    CONTROLLER_DIGEST=$(get_digest "$CONTROLLER_FULL_IMAGE:$TAG")
    
    echo "App Digest: $APP_DIGEST"
    echo "Controller Digest: $CONTROLLER_DIGEST"
    
    APP_REF="$APP_FULL_IMAGE@$APP_DIGEST"
    CONTROLLER_REF="$CONTROLLER_FULL_IMAGE@$CONTROLLER_DIGEST"
fi

# Update base/kustomization.yaml for app
# We use the abstract name 'shipshape/helloworld' as the key
(cd k8s/base && kustomize edit set image shipshape/helloworld="$APP_REF")

# Update controller/kustomization.yaml for controller
(cd k8s/controller && kustomize edit set image shipshape/controller="$CONTROLLER_REF")

echo "Kustomize updated with: $APP_REF and $CONTROLLER_REF"
echo "Ready to deploy."
