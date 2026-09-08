#!/usr/bin/env bash
# Orchestrates a real Home Assistant Supervisor + Core environment (started
# by the calling workflow from the official
# ghcr.io/home-assistant/devcontainer image) through Supervisor's own
# discover -> build -> install -> start flow for this repo's add-on.
#
# Each subcommand below is invoked as its own workflow step so GitHub
# Actions shows granular timing/failure per phase, and so a human can run
# any single phase locally against an already-running container while
# debugging. See .github/workflows/ha-integration.yaml for the exact
# sequence, container name, and env vars this expects.
#
# Every wait is a poll loop with a real timeout, never a fixed sleep -- see
# poll() below. On timeout, the failing check's own description is printed
# before exiting non-zero, so a failed run says what specifically didn't
# happen rather than just "step failed".
#
# Nothing here ever falls back to a manual `docker build`/`docker run` for
# the add-on itself -- every subcommand that touches the add-on goes
# through Supervisor's own `ha` CLI (a thin wrapper around the Supervisor
# REST API, see /usr/bin/ha inside the devcontainer image) or, for the one
# thing the CLI doesn't expose (see enable-ingress-panel below), a direct
# call to that same REST API. A failure in any of these is the exact class
# of bug this job exists to catch and must fail the job, not be routed
# around.
#
# One deliberate exception, scoped narrowly: cmd_prepare_addon_copy below
# disables AppArmor confinement (`apparmor: false`) for a separate copy
# of the add-on only (under github.workspace, deliberately not
# runner.temp -- see ha-integration.yaml's own comment on this step for a
# real, `act`-specific reason that distinction matters), never the
# checked-out repo's real config.yaml/
# apparmor.txt. See the "Prepare CI-only add-on copy" step in
# ha-integration.yaml for the full why -- short version, AppArmor
# confinement inside this nested Docker-in-Docker CI environment does not
# reliably reflect real Home Assistant OS host behavior, and this job's
# actual purpose (Supervisor discovery/build/install/start + real ingress)
# is a separate concern from AppArmor policy correctness.

set -uo pipefail

CONTAINER_NAME="${CONTAINER_NAME:?CONTAINER_NAME must be set}"
ADDON_SLUG="${ADDON_SLUG:?ADDON_SLUG must be set}"
HA_PORT="${HA_PORT:?HA_PORT must be set}"
ADDON_PORT="${ADDON_PORT:?ADDON_PORT must be set}"

cmd_prepare_addon_copy() {
  local src="blink_clip_downloader"
  local dest="${ADDON_COPY_DIR:?ADDON_COPY_DIR must be set}"
  # A separate copy of the add-on under $ADDON_COPY_DIR (github.workspace,
  # not runner.temp -- see the long comment on this step in
  # ha-integration.yaml for why that distinction actually matters), never
  # the checked-out repo path itself -- disabling AppArmor confinement
  # here is scoped to this ephemeral CI container only and must never
  # touch the real apparmor.txt/config.yaml a real user's install
  # actually gets.
  rm -rf "$dest"
  cp -a "$src" "$dest"
  if grep -q '^apparmor:' "$dest/config.yaml"; then
    sed -i 's/^apparmor:.*/apparmor: false/' "$dest/config.yaml"
  else
    printf '\napparmor: false\n' >>"$dest/config.yaml"
  fi
}

ha_cli() {
  docker exec "$CONTAINER_NAME" ha "$@"
}

# Supervisor-level add-on metadata (ingress_panel) isn't exposed via any
# dedicated `ha` CLI subcommand -- confirmed by reading the CLI's own
# source (github.com/home-assistant/cli/cmd), which only wraps
# addons/{slug}/{install,start,stop,info,logs,...}, not the sibling
# addons/{slug}/options endpoint's ingress_panel field. Call the Supervisor
# REST API directly instead, from inside hassio_cli specifically -- it's
# the one container Supervisor already injects a SUPERVISOR_TOKEN into,
# and "supervisor" only resolves on Supervisor's own internal Docker
# network (confirmed empirically: neither the outer devcontainer host nor
# a bare `docker exec <outer> curl` can reach it).
supervisor_api() {
  local method="$1" path="$2" body="${3:-}"
  docker exec "$CONTAINER_NAME" docker exec hassio_cli sh -c "
    curl -sf -X '$method' 'http://supervisor$path' \
      -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" \
      -H 'Content-Type: application/json' \
      ${body:+-d '$body'}
  "
}

# poll DESCRIPTION TIMEOUT_S INTERVAL_S COMMAND...
# Retries COMMAND (its exit code, not its output, decides success) until it
# succeeds or TIMEOUT_S elapses, sleeping INTERVAL_S between attempts.
poll() {
  local description="$1" timeout_s="$2" interval_s="$3"
  shift 3
  local waited=0
  until "$@" >/dev/null 2>&1; do
    if ((waited >= timeout_s)); then
      echo "TIMEOUT after ${timeout_s}s waiting for: $description" >&2
      return 1
    fi
    sleep "$interval_s"
    waited=$((waited + interval_s))
  done
  echo "OK (after ~${waited}s): $description"
}

cmd_wait_docker() {
  # The outer devcontainer image runs a full init system that starts its
  # own inner dockerd as one of its managed services -- it is not
  # necessarily ready the instant `docker run -d` returns. `supervisor_run`
  # has `set -e` and calls `docker system prune -f`/`docker run` as its
  # very first real actions with no readiness check of its own; if the
  # inner dockerd isn't up yet, those fail immediately and the whole
  # script dies silently (its own stdout/stderr only goes to
  # /var/log/supervisor_run.log inside the container, which nothing
  # surfaces unless something explicitly waits then checks it - see
  # cmd_diagnostics below). Confirmed as a real failure mode, not a
  # hypothetical: a run on a real GitHub-hosted runner produced zero
  # nested containers at all (not even hassio_supervisor) after the full
  # Core-readiness timeout elapsed, while the identical sequence run by
  # hand (with natural delays between commands) and under `act` both
  # worked - i.e. exactly the shape of a race condition that only shows up
  # when steps run back-to-back as fast as a real workflow runs them.
  poll "inner Docker daemon ready" 90 3 \
    docker exec "$CONTAINER_NAME" docker info
}

cmd_wait_core() {
  # Split into two phases for a much more actionable failure message than
  # one long wait for Core alone would give: first confirm supervisor_run
  # actually got far enough to launch hassio_supervisor at all (a
  # completely different failure mode from "Supervisor is up but Core is
  # still slow to boot", and one worth distinguishing loudly rather than
  # burning the full timeout below only to find out indirectly).
  poll "hassio_supervisor container exists" 60 3 \
    bash -c "docker exec '$CONTAINER_NAME' docker ps -a --format '{{.Names}}' | grep -qx hassio_supervisor"

  # hassio_observer (container port 80) is the stable Supervisor-managed
  # entry point -- Core's own 8123 redirects direct access there once it
  # detects it's running under Supervisor (confirmed empirically: the
  # redirect happens even from inside the container, so it isn't a
  # host-port-mapping artifact). Even once that redirect resolves, Core is
  # still loading default_config's many integrations for a while after the
  # container merely shows "docker ps: Up" -- polling raw TCP/HTTP-200
  # would false-positive on Core's own placeholder landing page. The
  # onboarding status endpoint requires no auth and only returns real JSON
  # once Core's HTTP API is genuinely serving requests, making it a
  # reliable universal readiness probe regardless of onboarding state.
  poll "Home Assistant Core answering via the observer (port ${HA_PORT})" 420 5 \
    curl -sf "http://127.0.0.1:${HA_PORT}/api/onboarding"
}

cmd_discover() {
  ha_cli store reload
  # #3976-class flakiness mitigation: an explicit reload plus a generous
  # poll, rather than trusting discovery happened automatically the moment
  # the add-on directory was bind-mounted.
  poll "add-on '${ADDON_SLUG}' discovered in the local store" 90 3 \
    bash -c "docker exec '$CONTAINER_NAME' ha store apps --raw-json | grep -q '\"${ADDON_SLUG}\"'"
}

cmd_install() {
  # This is what makes Supervisor build the add-on from its own Dockerfile
  # via Supervisor's own internal `docker buildx build` call (confirmed by
  # reading the Supervisor log line this actually emits: "Running command
  # ['docker', 'buildx', 'build', ...]"). No manual `docker build` fallback
  # exists anywhere in this script, on purpose.
  ha_cli store apps install "$ADDON_SLUG" --raw-json
}

cmd_start() {
  ha_cli apps start "$ADDON_SLUG" --raw-json
  # Supervisor's reported state and the app's actual functioning are two
  # different failure modes -- checked independently, both required.
  poll "Supervisor reports '${ADDON_SLUG}' state=started" 120 3 \
    bash -c "docker exec '$CONTAINER_NAME' ha apps info '$ADDON_SLUG' --raw-json | grep -q '\"state\": *\"started\"'"
  # Secondary readiness probe only -- proves the app process itself is
  # alive and answering, never proof that ingress (the thing this job
  # actually exists to test) works. 127.0.0.1, not localhost: curl
  # resolving localhost's IPv6 address first hits a connection reset here
  # (confirmed empirically against this exact add-on's aiohttp server),
  # which would otherwise look like "not ready yet" and just waste the
  # whole timeout for no reason.
  poll "add-on's own /health endpoint responding on its direct port (${ADDON_PORT})" 60 3 \
    curl -sf "http://127.0.0.1:${ADDON_PORT}/health"
}

cmd_enable_ingress_panel() {
  # Installing and starting an add-on does NOT put its ingress panel in the
  # HA sidebar by default -- confirmed empirically (ingress_panel reads
  # back false even once the add-on is fully started and its ingress proxy
  # route already works). Without this, there is no real sidebar link for
  # Playwright to click, and the ingress assertion downstream couldn't be
  # exercised through genuine navigation at all.
  supervisor_api POST "/addons/${ADDON_SLUG}/options" '{"ingress_panel": true}'
}

cmd_diagnostics() {
  local out_dir="${1:?output directory required}"
  mkdir -p "$out_dir"
  # The single most important file when nothing nested ever came up at all
  # (confirmed empirically to be exactly the failure this repo hit once) -
  # supervisor_run's own stdout/stderr, which is otherwise completely
  # invisible: it's launched backgrounded (`docker exec -d`) by a workflow
  # step that returns immediately, and every `ha_cli`/`supervisor_api` call
  # above silently fails with a generic "no such container" if
  # hassio_supervisor/hassio_cli never started - neither of which explains
  # *why*. Captured first and unconditionally, since every other command
  # below assumes at least the `ha` CLI's target containers exist.
  docker exec "$CONTAINER_NAME" cat /var/log/supervisor_run.log \
    >"$out_dir/supervisor_run.log" 2>&1
  # Confirms what was actually installed -- specifically whether the
  # AppArmor-disabled CI-only copy (see cmd_prepare_addon_copy) really
  # took effect, without needing to re-derive that from first principles
  # during a future debugging session.
  cp "${ADDON_COPY_DIR}/config.yaml" "$out_dir/installed_config.yaml" 2>&1 || true
  # Inner dockerd health/process list - distinguishes "dockerd itself never
  # came up" from "dockerd is fine but supervisor_run failed for some other
  # reason", without which the supervisor_run.log above could still leave
  # the root cause ambiguous.
  docker exec "$CONTAINER_NAME" docker info >"$out_dir/inner_docker_info.txt" 2>&1
  docker exec "$CONTAINER_NAME" ps aux >"$out_dir/outer_container_processes.txt" 2>&1
  docker exec "$CONTAINER_NAME" docker ps -a >"$out_dir/nested_containers.txt" 2>&1
  {
    ha_cli core logs
  } >"$out_dir/core.log" 2>&1
  {
    ha_cli supervisor logs
  } >"$out_dir/supervisor.log" 2>&1
  {
    ha_cli apps logs "$ADDON_SLUG"
  } >"$out_dir/addon.log" 2>&1
  ha_cli apps info "$ADDON_SLUG" --raw-json >"$out_dir/addon_info.json" 2>&1
  ha_cli supervisor info --raw-json >"$out_dir/supervisor_info.json" 2>&1
  ha_cli core info --raw-json >"$out_dir/core_info.json" 2>&1
  ha_cli store apps --raw-json >"$out_dir/store_apps.json" 2>&1
  docker inspect "$CONTAINER_NAME" >"$out_dir/outer_container_inspect.json" 2>&1
  echo "Diagnostics written to $out_dir"
}

case "${1:-}" in
  prepare-addon-copy) cmd_prepare_addon_copy ;;
  wait-docker) cmd_wait_docker ;;
  wait-core) cmd_wait_core ;;
  discover) cmd_discover ;;
  install) cmd_install ;;
  start) cmd_start ;;
  enable-ingress-panel) cmd_enable_ingress_panel ;;
  diagnostics)
    shift
    cmd_diagnostics "$@"
    ;;
  *)
    echo "Usage: $0 {prepare-addon-copy|wait-docker|wait-core|discover|install|start|enable-ingress-panel|diagnostics <dir>}" >&2
    exit 64
    ;;
esac
