#!/usr/bin/env bash
# Smoke-tests a built Blink Clip Downloader add-on image: boots the
# container with fake Blink credentials, probes the media server over HTTP
# (curl/jq), runs a Playwright e2e check against the real web UI, then
# confirms the container is still alive with no unhandled exceptions.
#
# Runs identically in CI (.github/workflows/ci.yaml, smoke-test job) and
# locally, so there's one source of truth for what "the add-on boots and
# serves a working UI" means.
#
# Usage:
#   scripts/smoke-test.sh [image-tag]
#
# If image-tag is omitted, builds one from blink_clip_downloader/ tagged
# blink-clip-downloader:smoke-test-local.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${1:-blink-clip-downloader:smoke-test-local}"
CONTAINER_NAME="blink-clip-downloader-smoke-test"
PORT="${SMOKE_TEST_PORT:-8099}"
WORKDIR="$(mktemp -d)"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

if [ -z "${1:-}" ]; then
  echo "No image tag given; building ${IMAGE} from blink_clip_downloader/ ..."
  docker build --pull -t "$IMAGE" "$REPO_ROOT/blink_clip_downloader"
fi

mkdir -p "$WORKDIR/data" "$WORKDIR/share"
cat > "$WORKDIR/data/options.json" <<'EOF'
{
  "username": "test@example.com",
  "password": "test-password",
  "download_path": "/share/blink-clips",
  "poll_interval": 300,
  "max_clips_per_poll": 1,
  "retention_days": 1,
  "max_storage_gb": 1.0,
  "camera_filter": [],
  "download_thumbnails": false,
  "concurrent_downloads": 1,
  "retry_attempts": 1,
  "retry_delay": 1.0,
  "notify_ha": false,
  "create_clip_manifest": false,
  "enable_library_db": true,
  "enable_media_server": true,
  "watch_ha_events": false,
  "fast_poll_duration": 120,
  "fast_poll_interval": 15,
  "post_motion_delay": 30,
  "event_cameras": [],
  "digest_enabled": false,
  "archive_enabled": false,
  "log_level": "info"
}
EOF

echo "Starting container..."
docker run -d --name "$CONTAINER_NAME" \
  -e TZ=UTC \
  -p "${PORT}:8099" \
  -v "$WORKDIR/data:/data" \
  -v "$WORKDIR/share:/share" \
  "$IMAGE"

echo "Waiting for startup..."
sleep 15

echo "Validating container is running..."
if ! docker ps --filter "name=${CONTAINER_NAME}" --filter "status=running" | grep -q "$CONTAINER_NAME"; then
  docker logs "$CONTAINER_NAME"
  exit 1
fi
docker logs "$CONTAINER_NAME"

echo "Waiting for media server to respond..."
media_server_up=""
for i in $(seq 1 10); do
  if curl -fsS "http://localhost:${PORT}/health" >/dev/null; then
    echo "Media server is up"
    media_server_up=1
    break
  fi
  echo "Media server not reachable yet (attempt $i/10), retrying..."
  sleep 3
done
if [ -z "$media_server_up" ]; then
  echo "Media server never became reachable on port ${PORT}"
  docker logs "$CONTAINER_NAME"
  exit 1
fi

echo "GET / (web UI shell)"
index_body=$(curl -fsS "http://localhost:${PORT}/")
grep -q "<title>Blink Clip Library</title>" <<<"$index_body"

echo "GET /api/clips"
curl -fsS "http://localhost:${PORT}/api/clips" | jq -e 'type == "array"' >/dev/null

echo "GET /api/stats"
curl -fsS "http://localhost:${PORT}/api/stats" | jq -e 'has("total_count")' >/dev/null

echo "GET /api/cameras"
curl -fsS "http://localhost:${PORT}/api/cameras" | jq -e 'type == "array"' >/dev/null

echo "GET /api/auth/status"
curl -fsS "http://localhost:${PORT}/api/auth/status" | jq -e 'has("state")' >/dev/null

echo "All media server endpoints responded as expected"

if [ "${SMOKE_TEST_SKIP_E2E:-}" != "1" ]; then
  echo "Running Playwright e2e check..."
  (cd "$REPO_ROOT/e2e" && node smoke.mjs "http://localhost:${PORT}/")
else
  echo "SMOKE_TEST_SKIP_E2E=1 set; skipping Playwright e2e check"
fi

echo "Verifying container is still healthy..."
if ! docker ps --filter "name=${CONTAINER_NAME}" --filter "status=running" | grep -q "$CONTAINER_NAME"; then
  echo "Container is no longer running after smoke tests"
  docker logs "$CONTAINER_NAME"
  exit 1
fi
if docker logs "$CONTAINER_NAME" 2>&1 | grep -q "Traceback (most recent call last)"; then
  echo "Found an unhandled exception in container logs:"
  docker logs "$CONTAINER_NAME"
  exit 1
fi
echo "Container still running with no unhandled exceptions"

echo "Smoke test passed."
