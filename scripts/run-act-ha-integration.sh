#!/usr/bin/env bash
# Run .github/workflows/ha-integration.yaml locally with act, mirroring
# scripts/run-act.sh's conventions for the main CI workflow.
#
# This job runs its own nested Docker-in-Docker (a real dockerd inside a
# --privileged container, not just a socket passthrough) to bring up
# Supervisor + Home Assistant Core - act itself only needs to start that
# one outer container via the host's real Docker daemon, which act's
# default runner image already has access to.
#
# Usage:
#   scripts/run-act-ha-integration.sh
#
# Environment:
#   ACT_PLATFORM_IMAGE=...        # override the act runner image
#   ACT_PLAYWRIGHT_CACHE=...      # override the host Playwright browser cache
#
# Note: unlike run-act.sh's jobs, this one leaves real, uniquely-named
# containers/images on your host Docker daemon on both success and failure
# (CONTAINER_NAME=ha-integration-test, plus everything Supervisor starts
# inside it) - the workflow's own "Stop and remove the devcontainer" step
# handles that the same way it would on a real runner. Re-running this
# script removes any previous ha-integration-test container first.

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
PLAYWRIGHT_CACHE="${ACT_PLAYWRIGHT_CACHE:-${HOME}/.cache/ms-playwright}"

# A stale container from a previous local run (this script's or a manual
# one) would make the "Start Home Assistant devcontainer" step's `docker
# run --name ha-integration-test` fail with a name conflict before act
# even gets to exercise anything.
docker rm -f ha-integration-test >/dev/null 2>&1 || true

EVENT_FILE="$(mktemp)"
trap 'rm -f "$EVENT_FILE"' EXIT
printf '%s\n' \
  '{"ref":"refs/heads/main","repository":{"full_name":"local/ha-blink-clip-downloader","default_branch":"main"},"sender":{"login":"local"}}' \
  >"$EVENT_FILE"

ACT_ARGS=(
  --workflows .github/workflows/ha-integration.yaml
  --eventpath "$EVENT_FILE"
  --container-architecture linux/amd64
  --platform "ubuntu-latest=$RUNNER_IMAGE"
  --actor nektos/act
  --env ACT=true
  --job ha-integration-test
  --rm
)

if [[ -d "$PLAYWRIGHT_CACHE" ]]; then
  ACT_ARGS+=(--container-options "-v ${PLAYWRIGHT_CACHE}:/root/.cache/ms-playwright")
fi

echo "Running ha-integration-test under act..."
act "${ACT_ARGS[@]}"
