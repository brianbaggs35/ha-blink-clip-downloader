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
  #
  # Only git-tracked files -- mirroring exactly what a real user's git
  # checkout actually contains, and (found the hard way locally,
  # 2026-09-08) NOT a blanket `cp -a`, which also drags in local dev
  # scratch data (local-test/, .venv, node_modules, __pycache__, built
  # frontend static/ output, ...): wasteful on a disk-constrained runner,
  # irrelevant to what Supervisor's own build needs (it builds static/
  # fresh from frontend/ source via the Dockerfile regardless), and once
  # even left root-owned PostgreSQL data-directory files behind that
  # couldn't be cleaned up locally without sudo.
  rm -rf "$dest"
  mkdir -p "$dest"
  git ls-files -z -- "$src" |
    sed -z "s|^${src}/||" |
    rsync -a --files-from=- --from0 "$src/" "$dest/"
  if grep -q '^apparmor:' "$dest/config.yaml"; then
    sed -i 's/^apparmor:.*/apparmor: false/' "$dest/config.yaml"
  else
    printf '\napparmor: false\n' >>"$dest/config.yaml"
  fi

  # Supervisor cannot forward the PrimeVue BuildKit secret into its nested
  # Docker build. The integration workflow publishes a licensed image first
  # and supplies its untagged repository here; Supervisor appends the app
  # version from config.yaml when it pulls the image.
  if [[ -n "${INTEGRATION_IMAGE:-}" ]]; then
    if grep -q '^image:' "$dest/config.yaml"; then
      sed -i "s|^image:.*|image: \"${INTEGRATION_IMAGE}\"|" "$dest/config.yaml"
    else
      printf 'image: "%s"\n' "$INTEGRATION_IMAGE" >>"$dest/config.yaml"
    fi
  fi

  # ...and, for the same reason, the version Supervisor appends to it.
  #
  # The tag has to be unique per commit or two runs would overwrite each
  # other's image and each could end up testing the other's build. That
  # uniqueness used to live in the *package name* (one ghcr package per
  # commit), which meant a new package on the account's Packages page every
  # run and no way to clean them up: GitHub refuses to delete a package's
  # last tagged version, and deleting the package itself needs a PAT the
  # job does not have. Putting the uniqueness in the tag instead leaves a
  # single package whose old versions the job *can* prune.
  if [[ -n "${INTEGRATION_VERSION:-}" ]]; then
    if grep -q '^version:' "$dest/config.yaml"; then
      sed -i "s|^version:.*|version: \"${INTEGRATION_VERSION}\"|" "$dest/config.yaml"
    else
      printf 'version: "%s"\n' "$INTEGRATION_VERSION" >>"$dest/config.yaml"
    fi
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
    curl -sf --max-time 30 -X '$method' 'http://supervisor$path' \
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

cmd_serve_local_image() {
  # Supervisor installs the add-on by *pulling* the image config.yaml names,
  # through the devcontainer's own inner Docker daemon. That is the only
  # reason this job ever published anything: the image had to sit somewhere
  # that daemon could reach, and ghcr was the obvious somewhere — at the
  # cost of a real package, in a real registry, for an artifact that never
  # outlives the job.
  #
  # A registry on the loopback interface *inside* the devcontainer is
  # reachable by that same daemon and needs no credentials and no daemon
  # configuration: Docker exempts 127.0.0.1 from its HTTPS requirement, so
  # plain HTTP is accepted as-is. Nothing leaves the job.
  local tar="${1:?image tar path required}"
  local ref="${INTEGRATION_IMAGE:?INTEGRATION_IMAGE must be set}:${INTEGRATION_VERSION:?INTEGRATION_VERSION must be set}"

  # This function exists so that nothing is published, so make that a rule
  # rather than a property of today's configuration: an edit that repoints
  # INTEGRATION_IMAGE at a real registry fails here instead of quietly
  # publishing a CI-only image again, which is how the ghcr packages this
  # replaced came about in the first place.
  case "$ref" in
    127.0.0.1:* | localhost:*) ;;
    *)
      echo "refusing to push ${ref}: serve-local-image publishes only to a" \
        "loopback registry inside the devcontainer" >&2
      return 1
      ;;
  esac

  # /var/tmp, not /tmp: the devcontainer is started with `--tmpfs /tmp`, and
  # `docker cp` into a tmpfs mount does not reach what the running container
  # actually sees there. The first version of this used /tmp, the copy
  # vanished, and the only symptom was Supervisor failing to install the
  # add-on two steps later.
  local dest=/var/tmp/integration-image.tar

  # Every step checked explicitly: this script runs without `set -e`, and a
  # silent failure here surfaces much later as "App is not installed", which
  # says nothing about what actually went wrong.
  if ! docker cp "$tar" "${CONTAINER_NAME}:${dest}"; then
    echo "could not copy ${tar} into ${CONTAINER_NAME}:${dest}" >&2
    return 1
  fi
  if ! docker exec "$CONTAINER_NAME" docker load -i "$dest"; then
    echo "docker load of ${dest} failed inside ${CONTAINER_NAME}" >&2
    return 1
  fi
  docker exec "$CONTAINER_NAME" rm -f "$dest" || true

  # The load is what puts both the app image and registry:2 in there, so
  # confirm the tag arrived rather than discovering it missing at push time,
  # where the error ("tag does not exist") reads like a tagging mistake.
  if ! docker exec "$CONTAINER_NAME" docker image inspect "$ref" >/dev/null 2>&1; then
    echo "${ref} is not present after docker load — the image tar did not" \
      "contain it" >&2
    docker exec "$CONTAINER_NAME" docker images >&2 || true
    return 1
  fi

  # registry:2 arrived in the same tar as the app image, so this starts from
  # what was just loaded and never reaches Docker Hub.
  #
  # --restart=always so it survives anything Supervisor's own startup does
  # to the daemon; published on loopback only, so it is not reachable from
  # outside the container even within the job.
  if ! docker exec "$CONTAINER_NAME" docker run -d --restart=always \
    --name integration-registry -p 127.0.0.1:5000:5000 registry:2; then
    echo "could not start the loopback registry inside ${CONTAINER_NAME}" >&2
    return 1
  fi

  poll "local registry ready" 60 2 \
    docker exec "$CONTAINER_NAME" \
    curl -sf http://127.0.0.1:5000/v2/

  if ! docker exec "$CONTAINER_NAME" docker push "$ref"; then
    echo "could not push ${ref} to the loopback registry" >&2
    return 1
  fi
  echo "OK: ${ref} is served from a registry inside the devcontainer"
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
  # #3976-class flakiness mitigation. Reload the store through Supervisor's
  # store API rather than the `ha store reload` wrapper, which can wait
  # indefinitely while Supervisor is still settling and hides the useful
  # failure behind the wrapper's suppressed output. The request itself has a
  # 30-second timeout, and the reload is retried inside the bounded poll.
  discover_store_app() {
    supervisor_api POST "/store/reload" >/dev/null
    local apps
    apps="$(timeout 30s docker exec "$CONTAINER_NAME" ha store apps --raw-json)" || return 1
    grep -q "\"${ADDON_SLUG}\"" <<<"$apps"
  }

  poll "add-on '${ADDON_SLUG}' discovered in the local store" 150 3 \
    discover_store_app
}

cmd_install() {
  # The CI-only config copy points at the licensed image published by the
  # workflow. Supervisor pulls that image here instead of attempting a local
  # Dockerfile build that cannot receive the PrimeVue BuildKit secret.
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

cmd_restart() {
  # The closest thing this job can do to rehearsing an upgrade without
  # installing two images: stopping and starting recreates the add-on's
  # container while /data survives, which is exactly the transition an
  # update puts an existing install through. What it proves is the part
  # that actually breaks people -- that the bundled PostgreSQL cluster
  # written under /data by the previous run is re-attached and re-read by a
  # fresh container, rather than the add-on only ever working on the
  # first-ever start against an empty volume.
  ha_cli apps stop "$ADDON_SLUG" --raw-json
  poll "Supervisor reports '${ADDON_SLUG}' stopped" 120 3 \
    bash -c "docker exec '$CONTAINER_NAME' ha apps info '$ADDON_SLUG' --raw-json | grep -q '\"state\": *\"stopped\"'"
  cmd_start
}

# Error lines that are *expected* on a healthy run in this environment. The
# add-on is installed with default options, so it has no Blink username: it
# reports that and starts in web-only mode, exactly as designed (see
# __main__.main()'s deliberate refusal to sys.exit). Everything here was
# taken from a real run's log, not guessed.
#
# Deliberately a short, specific list: the whole point of this check is to
# notice a traceback or an error nobody has seen before, and a permissive
# allowlist would hide precisely that.
_EXPECTED_LOG_NOISE='Configuration error . starting in web-only mode|Running in web-only mode|username is required and cannot be empty|Blink authentication failed|Invalid credentials|Could not connect to Blink|two_fa|2FA|AI analysis (is )?not configured|No AI provider|ollama'

# The one traceback a healthy run in this environment prints: config
# validation rejecting the absent username. Dropped as a *block* — from the
# "Traceback" line through the ValueError that ends it — so that any other
# traceback still shows up, rather than being waved through by a pattern
# loose enough to match all of them.
_strip_expected_traceback() {
  awk '
    /Traceback \(most recent call last\)/ { skipping = 1 }
    skipping && /ValueError: username is required and cannot be empty/ {
      skipping = 0
      next
    }
    !skipping
  '
}

cmd_assert_clean_log() {
  # Supervisor keeps the add-on's stdout, which is where every unhandled
  # exception in the poll loop, the media server, or any background task
  # ends up. A traceback there does not stop the container, so without this
  # the job passes with the add-on quietly broken behind a UI that still
  # renders its empty states.
  local log
  log="$(ha_cli apps logs "$ADDON_SLUG" 2>&1 || true)"

  # Positive check first: a log that is "clean" because the app never got
  # far enough to say anything would otherwise pass silently.
  local marker='Media server listening on port'
  if ! printf '%s\n' "$log" | grep -qF "$marker"; then
    echo "The add-on's log never reports the media server starting." >&2
    echo "Last 40 lines:" >&2
    printf '%s\n' "$log" | tail -40 >&2
    return 1
  fi

  local suspicious
  suspicious="$(printf '%s\n' "$log" \
    | _strip_expected_traceback \
    | grep -E 'Traceback \(most recent call last\)|CRITICAL|ERROR' \
    | grep -Ev "$_EXPECTED_LOG_NOISE" || true)"
  if [[ -n "$suspicious" ]]; then
    echo "Unexpected error output in the add-on's log:" >&2
    printf '%s\n' "$suspicious" >&2
    return 1
  fi
  echo "OK: add-on started cleanly, with no unexpected errors or tracebacks"
}

cmd_assert_persisted() {
  # The other half of e2e/ha_integration_smoke.mjs's PERSISTENCE_MARKER:
  # that marker was written through the real ingress UI *before*
  # `restart` recreated the add-on's container. Reading it back now proves
  # /data -- the bundled PostgreSQL cluster included -- genuinely survived
  # that, which is the transition an upgrade puts every existing install
  # through and which nothing else in this repo's CI covers.
  #
  # Read over the add-on's own direct port rather than through ingress:
  # this runs after the browser is gone, and the direct port is already
  # proven reachable by cmd_start's readiness probe.
  local expected="${1:?expected marker value required}"
  local body
  body="$(curl -sf --max-time 30 \
    "http://127.0.0.1:${ADDON_PORT}/api/storage/gdrive/settings" || true)"
  if [[ "$body" != *"$expected"* ]]; then
    echo "Settings written before the restart did not survive it." >&2
    echo "  expected to find: $expected" >&2
    echo "  got: ${body:-<no response>}" >&2
    return 1
  fi
  echo "OK: settings written before the restart survived it (/data persisted)"
}

cmd_assert_version() {
  # Supervisor pulls the image named in config.yaml by that file's own
  # version. If the tag it resolved were stale, every check in this job
  # would still pass while testing the wrong build entirely.
  local expected="${1:?expected version required}"
  local reported
  reported="$(ha_cli apps info "$ADDON_SLUG" --raw-json 2>/dev/null \
    | tr ',' '\n' | grep -m1 '"version"' | cut -d'"' -f4 || true)"
  if [[ "$reported" != "$expected" ]]; then
    echo "Supervisor is running version '${reported:-<unknown>}', expected '${expected}'" >&2
    return 1
  fi
  echo "OK: Supervisor is running the expected version ($expected)"
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
  serve-local-image)
    shift
    cmd_serve_local_image "$@"
    ;;
  wait-core) cmd_wait_core ;;
  discover) cmd_discover ;;
  install) cmd_install ;;
  start) cmd_start ;;
  restart) cmd_restart ;;
  assert-clean-log) cmd_assert_clean_log ;;
  assert-persisted)
    shift
    cmd_assert_persisted "$@"
    ;;
  assert-version)
    shift
    cmd_assert_version "$@"
    ;;
  enable-ingress-panel) cmd_enable_ingress_panel ;;
  diagnostics)
    shift
    cmd_diagnostics "$@"
    ;;
  *)
    echo "Usage: $0 {prepare-addon-copy|wait-docker|serve-local-image <tar>|wait-core|discover|install|start|restart|assert-clean-log|assert-persisted <value>|assert-version <version>|enable-ingress-panel|diagnostics <dir>}" >&2
    exit 64
    ;;
esac
