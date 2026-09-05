#!/usr/bin/env bash
# Run the local-compatible portion of .github/workflows/ci.yaml with act.
#
# The workflow itself detects ACT=true and:
#   - rewrites the service mapping to a free host port instead of port 5432;
#   - skips GitHub artifact, Codecov, and SonarCloud integrations;
#   - keeps the locally-built AMD64 image in the host Docker daemon for smoke-test.
#
# Usage:
#   scripts/run-act.sh                 # lint, tests, E2E, image build, smoke test
#   scripts/run-act.sh lint test       # run selected job IDs only
#
# Environment:
#   ACT_CONCURRENT_JOBS=1              # override the default serialized run
#   ACT_PLATFORM_IMAGE=...             # override the act runner image
#   ACT_POSTGRES_PORT=...              # override the dynamically selected host port
#   ACT_PLAYWRIGHT_CACHE=...           # override the host Playwright browser cache

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s (%s)\n' "$1" "$2" >&2
    exit 1
  fi
}

require_command act "https://nektosact.com/installation/"
require_command docker "https://docs.docker.com/engine/install/"

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not reachable." >&2
  exit 1
fi

RUNNER_IMAGE="${ACT_PLATFORM_IMAGE:-catthehacker/ubuntu:act-latest}"
CONCURRENT_JOBS="${ACT_CONCURRENT_JOBS:-1}"
ACT_POSTGRES_PORT="${ACT_POSTGRES_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')}"
PLAYWRIGHT_CACHE="${ACT_PLAYWRIGHT_CACHE:-${HOME}/.cache/ms-playwright}"
EVENT_FILE=""
WORKFLOW_FILE=""
declare -A EXISTING_ACT_CONTAINERS=()

while IFS=$'\t' read -r container_id container_name; do
  [[ -z "$container_id" || -z "$container_name" ]] && continue
  EXISTING_ACT_CONTAINERS["$container_name"]=1
done < <(
  docker ps -a --format '{{.ID}}\t{{.Names}}' |
    awk -F '\t' '$2 ~ /^act-/'
)

cleanup() {
  while IFS=$'\t' read -r container_id container_name; do
    [[ -z "$container_id" || -z "$container_name" ]] && continue
    if [[ -z "${EXISTING_ACT_CONTAINERS[$container_name]+present}" ]]; then
      docker rm -f "$container_id" >/dev/null 2>&1 || true
    fi
  done < <(
    docker ps -aq --format '{{.ID}}\t{{.Names}}' |
      awk -F '\t' '$2 ~ /^act-/'
  )
  [[ -z "$EVENT_FILE" ]] || rm -f "$EVENT_FILE"
  [[ -z "$WORKFLOW_FILE" ]] || rm -f "$WORKFLOW_FILE"
}
trap cleanup EXIT

EVENT_FILE="$(mktemp)"
WORKFLOW_FILE="$(mktemp "$REPO_ROOT/.act-ci.XXXXXX.yaml")"

printf '%s\n' \
  '{"ref":"refs/heads/main","repository":{"full_name":"local/ha-blink-clip-downloader","default_branch":"main"},"sender":{"login":"local"}}' \
  >"$EVENT_FILE"

sed "s/5432:5432/${ACT_POSTGRES_PORT}:5432/g" \
  .github/workflows/ci.yaml >"$WORKFLOW_FILE"

if (($# > 0)); then
  JOBS=("$@")
else
  JOBS=(lint test frontend-e2e build smoke-test)
fi

ACT_ARGS=(
  --workflows "$WORKFLOW_FILE"
  --eventpath "$EVENT_FILE"
  --container-architecture linux/amd64
  --platform "ubuntu-latest=$RUNNER_IMAGE"
  --actor nektos/act
  --env ACT=true
  --env "ACT_POSTGRES_PORT=$ACT_POSTGRES_PORT"
  --env "ACT_TEST_DATABASE_DSN=postgresql://postgres:postgres@localhost:${ACT_POSTGRES_PORT}/blink_clips_test"
  --env "ACT_E2E_DATABASE_DSN=postgresql://postgres:postgres@localhost:${ACT_POSTGRES_PORT}/blink_clips_e2e"
  --matrix python-version:3.12
  --matrix arch:amd64
  --concurrent-jobs "$CONCURRENT_JOBS"
  --rm
)

if [[ -d "$PLAYWRIGHT_CACHE" ]]; then
  ACT_ARGS+=(--container-options "-v ${PLAYWRIGHT_CACHE}:/root/.cache/ms-playwright")
fi

for job in "${JOBS[@]}"; do
  ACT_ARGS+=(--job "$job")
done

echo "Running act jobs: ${JOBS[*]}"
echo "Using temporary Postgres host port: $ACT_POSTGRES_PORT"
act "${ACT_ARGS[@]}"
