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
#   VITE_PRIMEVUE_LICENSE_KEY=... # PrimeVue key used by the licensed image build
#   GHCR_TOKEN=...                # token with package read/write access for act
#   GHCR_USERNAME=...             # GitHub username associated with GHCR_TOKEN
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

if [[ -z "${VITE_PRIMEVUE_LICENSE_KEY:-}" ]] && [[ -n "${PRIMEVUE_LICENSE_KEY:-}" ]]; then
  VITE_PRIMEVUE_LICENSE_KEY="$PRIMEVUE_LICENSE_KEY"
  export VITE_PRIMEVUE_LICENSE_KEY
fi
if [[ -z "${VITE_PRIMEVUE_LICENSE_KEY:-}" ]]; then
  echo "Set VITE_PRIMEVUE_LICENSE_KEY before running HA integration under act." >&2
  exit 1
fi
if [[ -z "${GHCR_TOKEN:-}" ]]; then
  echo "Set GHCR_TOKEN before running HA integration under act." >&2
  exit 1
fi
if [[ -z "${GHCR_USERNAME:-}" ]]; then
  echo "Set GHCR_USERNAME to the GitHub account that owns GHCR_TOKEN." >&2
  exit 1
fi

# A stale container from a previous local run (this script's or a manual
# one) would make the "Start Home Assistant devcontainer" step's `docker
# run --name ha-integration-test` fail with a name conflict before act
# even gets to exercise anything.
docker rm -f ha-integration-test >/dev/null 2>&1 || true

EVENT_FILE="$(mktemp)"
SECRET_FILE="$(mktemp)"
chmod 600 "$SECRET_FILE"
trap 'rm -f "$EVENT_FILE" "$SECRET_FILE"' EXIT
printf '%s\n' \
  '{"ref":"refs/heads/main","repository":{"full_name":"local/ha-blink-clip-downloader","default_branch":"main"},"sender":{"login":"local"}}' \
  >"$EVENT_FILE"
printf 'PRIMEVUE_LICENSE_KEY=%s\nGITHUB_TOKEN=%s\n' \
  "$VITE_PRIMEVUE_LICENSE_KEY" "$GHCR_TOKEN" >"$SECRET_FILE"

ACT_ARGS=(
  --workflows .github/workflows/ha-integration.yaml
  --eventpath "$EVENT_FILE"
  --container-architecture linux/amd64
  --platform "ubuntu-latest=$RUNNER_IMAGE"
  --actor nektos/act
  --env ACT=true
  --env "GHCR_USERNAME=${GHCR_USERNAME}"
  --job ha-integration-test
  --rm
  --secret-file "$SECRET_FILE"
  # Required for this specific job: without --bind, act's job container
  # gets a one-time `docker cp` snapshot of the repo, not a live view of
  # it - so the "Prepare CI-only add-on copy" step's freshly-created
  # directory would exist only inside that ephemeral snapshot, invisible
  # to the sibling `docker run` (which talks to the *host's* real Docker
  # daemon) a few steps later. Docker doesn't error on a missing
  # bind-mount source, it silently creates an empty directory instead -
  # confirmed the hard way: two different host-side paths both came up
  # empty under plain `docker cp` semantics before this flag was added.
  # --bind makes the whole working directory a live two-way mount, so a
  # step-created file genuinely exists on the host by the time a later
  # step's sibling container needs it. Side effect worth knowing: with
  # --bind, anything a step writes into the checked-out tree lands in
  # your real working directory, not a throwaway copy - this workflow
  # only ever writes to paths already covered by .gitignore
  # (.ha-integration-addon-copy/, ha-integration-diagnostics/,
  # e2e/ha-integration-failure-*.png), so a local run won't dirty `git
  # status`, but keep that in mind before adding any new output path.
  --bind
)

if [[ -d "$PLAYWRIGHT_CACHE" ]]; then
  ACT_ARGS+=(--container-options "-v ${PLAYWRIGHT_CACHE}:/root/.cache/ms-playwright")
fi

echo "Running ha-integration-test under act..."
act "${ACT_ARGS[@]}"
