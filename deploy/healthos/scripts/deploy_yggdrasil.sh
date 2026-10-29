#!/usr/bin/env bash
set -Eeuo pipefail

# Production deployment keeps exactly one HealthOS container on the host.
# The registry is the source of release history; old containers are not kept.
CONTAINER_NAME="${HEALTHOS_CONTAINER_NAME:-healthos}"
IMAGE_REPOSITORY="${HEALTHOS_IMAGE_REPOSITORY:-docker.io/ziyaolin/healthos}"
IMAGE_TAG="${HEALTHOS_IMAGE_TAG:-latest}"
IMAGE="${HEALTHOS_IMAGE:-${IMAGE_REPOSITORY}:${IMAGE_TAG}}"
NETWORK="${HEALTHOS_NETWORK:-healthos_default}"
DATA_DIR="${HEALTHOS_DATA_DIR:-/mnt/user/appdata/healthos}"
TOKEN_FILE="${HEALTHOS_TOKEN_FILE:-/mnt/user/appdata/healthos-secrets/healthos-api-token}"
PORT="${HEALTHOS_PORT:-8792}"
TRAINING_SOURCE="${HEALTHOS_TRAINING_SOURCE:-local}"
PULL_IMAGE="${HEALTHOS_PULL_IMAGE:-1}"
PRUNE_OLD_IMAGE="${HEALTHOS_PRUNE_OLD_IMAGE:-1}"

work_id="healthos-deploy-$$"
env_file="/tmp/${work_id}.env"
source_env_file="/tmp/${work_id}.source.env"
health_file="/tmp/${work_id}.health"
previous_image=""
previous_image_id=""

cleanup() {
  rm -f "$env_file" "$source_env_file" "$health_file"
}
trap cleanup EXIT

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  docker inspect "$CONTAINER_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$source_env_file"
  previous_image=$(docker inspect "$CONTAINER_NAME" --format '{{.Config.Image}}')
  previous_image_id=$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')
  case "$TRAINING_SOURCE" in
    local)
      grep -v '^HEALTHOS_IRONBOOK_BASE_URL=' "$source_env_file" > "$env_file" || true
      ;;
    ironbook)
      cp "$source_env_file" "$env_file"
      ;;
    *)
      echo "HEALTHOS_TRAINING_SOURCE must be local or ironbook" >&2
      exit 2
      ;;
  esac
else
  echo "Container $CONTAINER_NAME does not exist; provide an env file with HEALTHOS_ENV_FILE for first install." >&2
  if [ -z "${HEALTHOS_ENV_FILE:-}" ] || [ ! -f "$HEALTHOS_ENV_FILE" ]; then
    exit 2
  fi
  cp "$HEALTHOS_ENV_FILE" "$env_file"
fi

if [ "$PULL_IMAGE" = "1" ]; then
  docker pull "$IMAGE"
fi

docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null

run_healthos() {
  docker run -d \
    --name "$CONTAINER_NAME" \
    --network "$NETWORK" \
    --restart unless-stopped \
    -p "127.0.0.1:${PORT}:${PORT}" \
    --env-file "$env_file" \
    -v "${DATA_DIR}:/app/data:rw" \
    -v "${TOKEN_FILE}:/run/secrets/healthos-api-token:ro" \
    "$1" serve --db /app/data/healthos.sqlite3 --raw-dir /app/data/raw \
    --host 0.0.0.0 --port "$PORT" >/dev/null
}

rollback() {
  if [ -z "$previous_image" ]; then
    echo "HealthOS rollout failed; no previous image is available for rollback." >&2
    return 0
  fi
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  run_healthos "$previous_image"
  echo "HealthOS rollout failed; recreated $CONTAINER_NAME from $previous_image" >&2
}

# Remove historical backup containers left by the former deployment flow.
stale_backups=$(docker ps -aq --filter "name=^${CONTAINER_NAME}-backup-")
if [ -n "$stale_backups" ]; then
  docker rm -f $stale_backups >/dev/null
fi

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  docker stop "$CONTAINER_NAME" >/dev/null
  docker rm "$CONTAINER_NAME" >/dev/null
fi

if ! run_healthos "$IMAGE"; then
  rollback
  exit 1
fi

healthy=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/healthz" > "$health_file"; then
    healthy=1
    break
  fi
  sleep 2
done

if [ "$healthy" -ne 1 ]; then
  docker logs --tail 100 "$CONTAINER_NAME" >&2 || true
  rollback
  exit 1
fi

if [ "$PRUNE_OLD_IMAGE" = "1" ] && [ -n "$previous_image_id" ]; then
  current_image_id=$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')
  if [ "$previous_image_id" != "$current_image_id" ]; then
    docker image rm "$previous_image_id" >/dev/null 2>&1 || true
  fi
  if [ "$previous_image" != "$IMAGE" ]; then
    docker image rm "$previous_image" >/dev/null 2>&1 || true
  fi
fi

echo "HealthOS rollout succeeded: $CONTAINER_NAME using $IMAGE"
cat "$health_file"
