#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="${1:-}"
IMAGE_REPOSITORY="${HEALTHOS_IMAGE_REPOSITORY:-docker.io/ziyaolin/healthos}"
CONTEXT="${HEALTHOS_BUILD_CONTEXT:-$(cd "$(dirname "$0")/../../.." && pwd)}"
DOCKERFILE="${HEALTHOS_DOCKERFILE:-${CONTEXT}/deploy/healthos/Dockerfile}"

if [ -z "$VERSION" ]; then
  echo "Usage: $0 <version>" >&2
  echo "Override the default repository with HEALTHOS_IMAGE_REPOSITORY if needed." >&2
  exit 2
fi

REVISION=$(git -C "$CONTEXT" rev-parse HEAD)
if [ -n "$(git -C "$CONTEXT" status --porcelain)" ]; then
  echo "Release builds require a clean committed worktree." >&2
  exit 2
fi

docker buildx build \
  --file "$DOCKERFILE" \
  --build-arg "HEALTHOS_VERSION=${VERSION}" \
  --build-arg "HEALTHOS_REVISION=${REVISION}" \
  --tag "${IMAGE_REPOSITORY}:${VERSION}" \
  --tag "${IMAGE_REPOSITORY}:latest" \
  --push \
  "$CONTEXT"
echo "Published ${IMAGE_REPOSITORY}:${VERSION} and ${IMAGE_REPOSITORY}:latest"
