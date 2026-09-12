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
#   VITE_PRIMEVUE_LICENSE_KEY=...      # PrimeVue key for build/E2E jobs

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
require_command python3 "https://www.python.org/downloads/"
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
  --matrix python-version:3.13
  --matrix arch:amd64
  --concurrent-jobs "$CONCURRENT_JOBS"
  --rm
  # Required for the smoke-test job specifically: its "Create smoke-test
  # data directories" step writes files under ${{ github.workspace }},
  # and a later step bind-mounts that same path into `docker run -v` for
  # the addon container. Without --bind, act's default checkout is a
  # one-time `docker cp` snapshot into the job's own container filesystem
  # — writes there never reach the real host path the -v mount source
  # string resolves against (the docker.sock passthrough dispatches that
  # `docker run` to the real host daemon), so the addon container silently
  # gets an empty auto-created directory instead of the seeded
  # options.json, and starts in web-only mode. --bind makes the workspace
  # a live two-way mount of this real directory instead, the same fix
  # scripts/run-act-ha-integration.sh already uses for an analogous
  # mid-job-directory visibility gap — see that script's own comment.
  --bind
)

needs_primevue_license=false
for job in "${JOBS[@]}"; do
  case "$job" in
    build|frontend-e2e)
      needs_primevue_license=true
      ;;
  esac
done

if [[ "$needs_primevue_license" == true ]]; then
  if [[ -z "${VITE_PRIMEVUE_LICENSE_KEY:-}" ]] && [[ -n "${PRIMEVUE_LICENSE_KEY:-}" ]]; then
    VITE_PRIMEVUE_LICENSE_KEY="$PRIMEVUE_LICENSE_KEY"
    export VITE_PRIMEVUE_LICENSE_KEY
  fi
  if [[ -z "${VITE_PRIMEVUE_LICENSE_KEY:-}" ]]; then
    echo "Set VITE_PRIMEVUE_LICENSE_KEY before running build or frontend-e2e under act." >&2
    exit 1
  fi
  ACT_ARGS+=(--secret "PRIMEVUE_LICENSE_KEY=${VITE_PRIMEVUE_LICENSE_KEY}")
fi

if [[ -d "$PLAYWRIGHT_CACHE" ]]; then
  ACT_ARGS+=(--container-options "-v ${PLAYWRIGHT_CACHE}:/root/.cache/ms-playwright")
fi

echo "Running act jobs: ${JOBS[*]}"
echo "Using temporary Postgres host port: $ACT_POSTGRES_PORT"
# act's --job flag selects exactly one job per invocation: passing it
# multiple times (as this script used to, once per requested job) doesn't
# accumulate a job list -- silently only the LAST --job wins, confirmed via
# `act --job a --job b --list` showing only "b" selected, and there is no
# native multi-job selection in act at all (nektos/act#2250, still open as
# of 2026-09-10). Looping and invoking act separately per job is the only
# way every requested job actually runs, instead of silently only the last
# one named on the command line -- e.g. `run-act.sh lint test` used to run
# only `test`, never `lint`, with no error or warning either way.
for job in "${JOBS[@]}"; do
  echo "=== act job: $job ==="
  act "${ACT_ARGS[@]}" --job "$job"
done
