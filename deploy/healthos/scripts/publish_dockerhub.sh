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

docker build \
  --file "$DOCKERFILE" \
  --tag "${IMAGE_REPOSITORY}:${VERSION}" \
  --tag "${IMAGE_REPOSITORY}:latest" \
  "$CONTEXT"
docker push "${IMAGE_REPOSITORY}:${VERSION}"
docker push "${IMAGE_REPOSITORY}:latest"
echo "Published ${IMAGE_REPOSITORY}:${VERSION} and ${IMAGE_REPOSITORY}:latest"
