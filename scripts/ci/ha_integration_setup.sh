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
  # tar, not rsync: rsync is present on GitHub's hosted runners but not in
  # act's default runner image, and this copy failing is not survivable --
  # everything downstream discovers, builds and installs whatever landed
  # here. tar ships everywhere both run.
  git ls-files -z -- "$src" |
    tar --null --files-from=- -cf - |
    tar -xf - -C "$dest" --strip-components=1

  # This script runs without `set -e`, so a failure above would otherwise
  # sail straight past: the copy step would report success, the stub left
  # behind would be edited happily by the seds below, and the first sign of
  # trouble would be `discover` timing out three minutes later with nothing
  # to say about why. Found exactly that way -- rsync was missing under act
  # and this step still went green.
  if [[ ! -s "$dest/config.yaml" ]] || ! grep -q '^slug: blink_clip_downloader$' "$dest/config.yaml"; then
    echo "Copying the add-on to ${dest} did not produce a usable config.yaml." >&2
    echo "  Expected a full copy of ${src}/ including 'slug: blink_clip_downloader'." >&2
    echo "  Got $(wc -l <"$dest/config.yaml" 2>/dev/null || echo 0) line(s)." >&2
    echo "  Files copied: $(find "$dest" -type f 2>/dev/null | wc -l)" >&2
    return 1
  fi
  if grep -q '^apparmor:' "$dest/config.yaml"; then
    sed -i 's/^apparmor:.*/apparmor: false/' "$dest/config.yaml"
  else
    printf '\napparmor: false\n' >>"$dest/config.yaml"
  fi

  # Placeholder credentials so load_config() succeeds. Without them the
  # add-on starts in *web-only mode*: __main__.py catches the ValueError
  # from _parse_credentials and rebuilds AppConfig with empty strings,
  # discarding every configured option, and app.py takes
  # _run_config_error_mode() instead of its real startup path. That meant
  # this job only ever exercised a deliberately-crippled app -- no
  # _log_startup_config(), no downloader, no analysis queue, no poll loop.
  #
  # These do not authenticate, and are not meant to: connecting is the one
  # thing this environment genuinely cannot do. _connect_with_retry()
  # never raises and the process stays alive through a failed login (the
  # web server keeps running so ingress stays green), so everything up to
  # the Blink API itself now runs for real. cmd_assert_clean_log's
  # allowlist already expects the resulting auth failure.
  #
  # Scoped to the options: block. config.yaml repeats every key under
  # schema: at the same indentation, so an unscoped substitution rewrites
  # the *type* there too -- turning `username: "str"` into the literal
  # credential. Supervisor then rejects the add-on's config as invalid and
  # simply omits it from the store, so the only symptom is `discover`
  # timing out with nothing to say. Found exactly that way.
  sed -i '/^options:/,/^schema:/ s|^  username: .*|  username: "ci-integration@example.invalid"|' \
    "$dest/config.yaml"
  sed -i '/^options:/,/^schema:/ s|^  password: .*|  password: "ci-integration-not-a-real-password"|' \
    "$dest/config.yaml"

  # The credentials must not have leaked into the schema: types there are
  # what Supervisor validates every user's options against, and a corrupted
  # one is invisible until discovery quietly never happens.
  if ! grep -q '^  username: "str"$' "$dest/config.yaml" ||
    ! grep -q '^  password: "password"$' "$dest/config.yaml"; then
    echo "The CI credential substitution damaged config.yaml's schema block." >&2
    echo "  schema: must still declare username/password as types, not values." >&2
    grep -n '^  \(username\|password\):' "$dest/config.yaml" >&2
    return 1
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
  if [[ -z "$body" ]]; then
    docker exec "$CONTAINER_NAME" docker exec hassio_cli sh -c "
      curl -sf --max-time 30 -X '$method' 'http://supervisor$path' \
        -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" \
        -H 'Content-Type: application/json'
    "
    return
  fi
  # The body goes in on stdin and is never interpolated into the command
  # string. It used to be spliced in as -d '$body', which works for a short
  # literal like {"ingress_panel": true} but breaks the moment the JSON
  # contains an apostrophe -- and this add-on's own default options do:
  # ai_prompt ships example phrases like 'A person is walking past the car'.
  # Those quotes closed the -d '...' early and the inner shell died with
  # "sh: syntax error: unterminated quoted string", which then looked like
  # Supervisor refusing the value rather than a quoting bug here.
  printf '%s' "$body" \
    | docker exec -i "$CONTAINER_NAME" docker exec -i hassio_cli sh -c "
      curl -sf --max-time 30 -X '$method' 'http://supervisor$path' \
        -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" \
        -H 'Content-Type: application/json' \
        --data-binary @-
    "
}

# Same call, but yields the HTTP status instead of the body and does not
# fail the shell on a 4xx -- so a caller can tell "Supervisor rejected
# this" apart from "the request never got there", which -sf alone cannot.
supervisor_api_status() {
  local method="$1" path="$2" body="${3:-}"
  printf '%s' "$body" \
    | docker exec -i "$CONTAINER_NAME" docker exec -i hassio_cli sh -c "
      curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
        -X '$method' 'http://supervisor$path' \
        -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" \
        -H 'Content-Type: application/json' \
        --data-binary @-
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
  # update puts an existing install through, rather than the add-on only
  # ever being exercised on a first-ever start against an empty volume.
  #
  # Three separate checks hang off this one restart, and each proves a
  # different thing: assert-persisted (the /data volume came back),
  # assert-clean-log (the bundled PostgreSQL cluster was re-opened -- see
  # that function for why its marker is what establishes this), and
  # assert-log-contains (an option set through Supervisor beforehand
  # reached AppConfig on the way back up).
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

# The tracebacks a healthy run in this environment prints, dropped as
# *blocks* -- from the "Traceback" line through the specific line that ends
# each one -- so that any other traceback still shows up rather than being
# waved through by a pattern loose enough to match all of them.
#
# Only one remains expected now: the add-on cannot reach the Blink API from
# CI, and _connect_with_retry() logs that with _LOGGER.exception. The
# config-validation traceback that used to be expected here is gone --
# prepare-addon-copy now supplies placeholder credentials, so a run that
# still prints it means the add-on fell back to web-only mode and this job
# should fail rather than quietly test a crippled app.
_strip_expected_traceback() {
  awk '
    /Traceback \(most recent call last\)/ { skipping = 1; buffered = 0 }
    skipping {
      buffer = buffer $0 "\n"
      buffered = 1
      # Terminators for the one expected traceback. Anything else keeps
      # buffering until the block ends and is printed intact below.
      if ($0 ~ /(BlinkAuthenticationError|AuthenticationError|LoginError|UnauthorizedError|aiohttp\.|ClientConnectorError|ClientResponseError|TimeoutError|socket\.gaierror)/) {
        skipping = 0; buffer = ""; buffered = 0
      }
      next
    }
    buffered { printf "%s", buffer; buffer = ""; buffered = 0 }
    !skipping
    END { if (buffered) printf "%s", buffer }
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
  # /data survived that, which is the transition an upgrade puts every
  # existing install through and which nothing else in this repo's CI
  # covers.
  #
  # Scope, precisely: this reads a settings file, so what it establishes is
  # that the /data volume came back -- the same volume the bundled
  # PostgreSQL cluster lives on, but not that PostgreSQL re-opened it. That
  # half is covered by assert-clean-log's "Media server listening" marker:
  # app.py awaits ClipDatabase.init() inline *before* creating the media
  # server task, so a cluster that failed to re-attach means that line
  # never appears.
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

cmd_set_option() {
  # Changes one add-on option the way the Configuration tab does, so the
  # restart that follows rehearses the whole Supervisor->app contract:
  # Supervisor writes /data/options.json, AppConfig parses it, and the
  # running app acts on it. Nothing else in CI covers that path -- pytest
  # reads options.json straight off disk, and every other job here runs
  # the add-on on defaults only.
  #
  # Read-modify-write rather than posting the one key: POST
  # /addons/{slug}/options *replaces* the whole options dict and then
  # validates it against the add-on's schema (confirmed by reading
  # Supervisor's own source, supervisor/api/apps.py's options handler --
  # `app.options = body[ATTR_OPTIONS]` followed by `app.schema(...)`).
  # config.yaml has required options with no "?" (username, password,
  # download_path, ...), so posting a lone key would fail validation and
  # leave the add-on unconfigured.
  local key="${1:?option key required}" value="${2:?option value required}"
  local info merged
  info="$(supervisor_api GET "/addons/${ADDON_SLUG}/info")"

  # python3 rather than jq: the runner has it, and this script has never
  # needed jq for anything else.
  merged="$(printf '%s' "$info" | python3 -c '
import json, sys
key, raw = sys.argv[1], sys.argv[2]
try:
    value = json.loads(raw)          # 17 -> int, "x" stays a string below
except json.JSONDecodeError:
    value = raw
options = json.load(sys.stdin)["data"]["options"]
options[key] = value
print(json.dumps({"options": options}))
' "$key" "$value")"

  supervisor_api POST "/addons/${ADDON_SLUG}/options" "$merged" >/dev/null

  # Read back rather than trusting the POST: a silently-ignored option
  # would otherwise surface as a confusing failure in the log assertion
  # after the restart instead of here, where the cause is obvious.
  local after
  after="$(supervisor_api GET "/addons/${ADDON_SLUG}/info" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['options'].get('$key'))")"
  if [[ "$after" != "$value" ]]; then
    echo "Supervisor did not store the '$key' option." >&2
    echo "  sent: $value" >&2
    echo "  read back: $after" >&2
    return 1
  fi
  echo "OK: Supervisor stored ${key}=${value}; the next start should pick it up"
}

cmd_assert_option_rejected() {
  # config.yaml's option schema is only worth anything if Supervisor
  # actually enforces it -- that schema is what stops a user typing a
  # value the add-on then crashes on at 3am. Supervisor validates the
  # posted options against it (supervisor/api/apps.py calls
  # `app.schema(...)`), so an out-of-range value must come back as an HTTP
  # error. supervisor_api uses `curl -sf`, which exits non-zero on one.
  #
  # Nothing else checks this: pytest builds AppConfig from a dict it wrote
  # itself and never sees the schema, and a malformed or over-permissive
  # schema would ship completely silently.
  local key="${1:?option key required}" value="${2:?option value required}"
  local original
  original="$(supervisor_api GET "/addons/${ADDON_SLUG}/info" \
    | python3 -c 'import json,sys; print(json.dumps({"options": json.load(sys.stdin)["data"]["options"]}))')"

  local bad
  bad="$(printf '%s' "$original" | python3 -c '
import json, sys
key, raw = sys.argv[1], sys.argv[2]
try:
    value = json.loads(raw)
except json.JSONDecodeError:
    value = raw
body = json.load(sys.stdin)
body["options"][key] = value
print(json.dumps(body))
' "$key" "$value")"

  # Status, not exit code. `curl -sf` fails identically whether Supervisor
  # refused the value or the request never arrived, so an exit-code check
  # here would report success for a broken request -- which is exactly how
  # a quoting bug in supervisor_api once made this look like it passed.
  local status
  status="$(supervisor_api_status POST "/addons/${ADDON_SLUG}/options" "$bad")"
  case "$status" in
    400 | 422)
      echo "OK: Supervisor rejected the out-of-schema ${key}=${value} (HTTP $status)"
      ;;
    200)
      # Accepted when it should not have been -- put the good options back
      # before failing, so the rest of the job fails for its own reasons
      # rather than on a deliberately-corrupted config.
      supervisor_api POST "/addons/${ADDON_SLUG}/options" "$original" >/dev/null 2>&1 || true
      echo "Supervisor ACCEPTED ${key}=${value}, which config.yaml's schema should reject." >&2
      echo "The add-on's option schema is not being enforced." >&2
      return 1
      ;;
    *)
      echo "Could not tell whether Supervisor enforces the schema." >&2
      echo "  POST /addons/${ADDON_SLUG}/options returned HTTP '${status:-<none>}'," >&2
      echo "  which is neither a validation refusal nor an acceptance." >&2
      return 1
      ;;
  esac
}

cmd_assert_capabilities() {
  # config.yaml's declarations are promises Supervisor has to honour, and
  # every one of them is invisible to every other job here: a dropped
  # `ingress: true` or `homeassistant_api: true` still builds, still
  # starts, and still passes a bare `docker run` smoke test -- it only
  # fails once a real Supervisor is the one reading the manifest.
  #
  # Checked against Supervisor's own view of the installed add-on rather
  # than against config.yaml, so this compares what Supervisor actually
  # granted with what the add-on needs at runtime.
  local info
  info="$(supervisor_api GET "/addons/${ADDON_SLUG}/info")"
  local problems
  problems="$(printf '%s' "$info" | python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]
problems = []
# ingress: the sidebar panel and every URL the web UI is served on.
if not data.get("ingress"):
    problems.append("ingress is not enabled")
# homeassistant_api: the Automations tab notification round trip.
if not data.get("homeassistant_api"):
    problems.append("homeassistant_api was not granted")
# A started add-on Supervisor considers broken still answers its port.
state = data.get("state")
if state != "started":
    problems.append("state is %r, not started" % (state,))
boot = data.get("boot")
if boot not in ("auto", "manual"):
    problems.append("unexpected boot mode %r" % (boot,))
for line in problems:
    print(line)
')"
  if [[ -n "$problems" ]]; then
    echo "Supervisor's view of the add-on does not match what it needs:" >&2
    printf '  - %s\n' "$problems" >&2
    return 1
  fi
  echo "OK: Supervisor granted ingress + homeassistant_api and reports the add-on started"
}

# The add-on's own container, as Supervisor named it. Derived rather than
# hardcoded, and checked, so a Supervisor naming change fails here with a
# clear message instead of somewhere downstream.
addon_container() {
  # Both prefixes are tried because Supervisor renamed them: the container
  # for this add-on is app_<slug> on the version this job pins, not the
  # addon_<slug> every piece of documentation still says. Same rename as
  # `ha addons` -> `ha apps` and "Add-ons" -> "Apps" in the UI. Checked
  # rather than assumed so a rename back, or forward, fails here with the
  # actual container list instead of somewhere downstream.
  local name
  for name in "app_${ADDON_SLUG}" "addon_${ADDON_SLUG}"; do
    if docker exec "$CONTAINER_NAME" docker inspect "$name" >/dev/null 2>&1; then
      printf '%s' "$name"
      return 0
    fi
  done
  echo "No add-on container found inside the devcontainer." >&2
  echo "  Tried: app_${ADDON_SLUG}, addon_${ADDON_SLUG}" >&2
  echo "  Containers present:" >&2
  docker exec "$CONTAINER_NAME" docker ps --format '    {{.Names}}' >&2
  return 1
}

# Pipes SQL into the add-on's bundled PostgreSQL. On stdin, never spliced
# into the command string -- the same mistake that broke supervisor_api,
# and SQL carries far more quoting than a JSON body does.
addon_psql() {
  # </dev/null on the lookup: this function's stdin is the caller's SQL
  # heredoc, and a docker command in a command substitution must not be
  # able to consume any of it before psql runs.
  local container
  container="$(addon_container </dev/null)" || return 1
  docker exec -i "$CONTAINER_NAME" docker exec -i "$container" \
    su -s /bin/bash postgres -c \
    "/usr/lib/postgresql/17/bin/psql -v ON_ERROR_STOP=1 -q -d blink_clips $*"
}

cmd_seed_data() {
  # Everything this job asserts through ingress had, until now, been an
  # *empty state*: no Blink account means no clips, so most tabs were only
  # ever verified in the one condition where they render almost nothing.
  #
  # Rows go straight into the add-on's own PostgreSQL rather than through
  # any API: nothing in the app creates a clip, they only ever arrive from
  # Blink. frontend/e2e/ seeds security_events the same way and for the
  # same reason. The difference here is that this is the *real* add-on
  # container, under a real Supervisor, behind real ingress -- the one
  # place the whole stack is assembled the way a user actually runs it.
  #
  # Every id is prefixed ci-seed- so its origin is obvious in a screenshot
  # or a failure dump. Deliberately shaped to unlock as many surfaces as
  # possible at once:
  #   - four cameras, three sources, tags, starred    -> Library's filters
  #   - two archived clips with an archive_path       -> Storage tab
  #   - analysis rows on two models, with token counts-> AI + AI Usage
  #   - security_events across three severities       -> Security Events
  #   - detected_objects on one clip                  -> modal's chips
  #   - battery_history per camera                    -> Status tab
  #   - a learned vehicle signature                   -> Vehicles tab
  #
  # All timestamps are recent on purpose. retention_days is set to 17 later
  # in this job, and a clip older than that would be a deletion candidate
  # whose file does not exist on disk.
  local now h6 d1 d2
  now="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
  h6="$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%S+00:00)"
  d1="$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%S+00:00)"
  d2="$(date -u -d '2 days ago' +%Y-%m-%dT%H:%M:%S+00:00)"

  addon_psql <<SQL
INSERT INTO clips
  (id, camera, file_path, timestamp, size_bytes, duration, source,
   starred, tags, downloaded_at, archived, archive_path, gdrive_backed_up)
VALUES
  ('ci-seed-1', 'Front Door', '/share/blink-clips/ci-seed-1.mp4',
   '${now}', 1048576, 12, 'motion', TRUE,  '["person"]', '${now}', FALSE, '', FALSE),
  ('ci-seed-2', 'Driveway',   '/share/blink-clips/ci-seed-2.mp4',
   '${h6}',  2097152, 30, 'motion', FALSE, '[]',         '${h6}',  FALSE, '', FALSE),
  ('ci-seed-3', 'Backyard',   '/share/blink-clips/ci-seed-3.mp4',
   '${h6}',   524288,  6, 'manual', FALSE, '["animal"]', '${h6}',  FALSE, '', FALSE),
  ('ci-seed-4', 'Side Gate',  '/share/blink-clips/ci-seed-4.mp4',
   '${d1}',  3145728, 45, 'live',   TRUE,  '[]',         '${d1}',  FALSE, '', FALSE),
  ('ci-seed-5', 'Front Door', '/share/blink-clips/ci-seed-5.mp4',
   '${d1}',   786432, 18, 'motion', FALSE, '["person","delivery"]', '${d1}', FALSE, '', FALSE),
  ('ci-seed-6', 'Driveway',   '/share/blink-clips/ci-seed-6.mp4',
   '${d2}',  1572864, 22, 'motion', FALSE, '[]',         '${d2}',  FALSE, '', TRUE),
  ('ci-seed-archived-1', 'Front Door', '/share/blink-clips/ci-seed-archived-1.mp4',
   '${d2}',  4194304, 25, 'motion', FALSE, '[]', '${d2}', TRUE,
   '/share/blink-clips/archive/ci-seed-archive.zip', FALSE),
  ('ci-seed-archived-2', 'Driveway', '/share/blink-clips/ci-seed-archived-2.mp4',
   '${d2}',  2621440, 15, 'motion', FALSE, '[]', '${d2}', TRUE,
   '/share/blink-clips/archive/ci-seed-archive.zip', FALSE)
ON CONFLICT (id) DO NOTHING;

-- Two models so AI Usage's per-model breakdown has more than one row to
-- price and group, which is where its aggregation SQL actually does work.
INSERT INTO analysis_results
  (clip_id, camera, model, is_suspicious, confidence, summary, analyzed_at,
   tokens_prompt, tokens_completion, risk_score, severity, event_type)
VALUES
  ('ci-seed-1', 'Front Door', 'ci-seed-model-a', TRUE, 0.92,
   'A person is standing at the front door.', '${now}', 1200, 80, 72.0,
   'suspicious', 'subject_present'),
  ('ci-seed-2', 'Driveway', 'ci-seed-model-a', FALSE, 0.11,
   'The driveway is empty and nothing is moving.', '${h6}', 900, 60, 4.0,
   'routine', ''),
  ('ci-seed-4', 'Side Gate', 'ci-seed-model-b', TRUE, 0.78,
   'A person is lingering by the side gate.', '${d1}', 1500, 120, 61.0,
   'suspicious', 'loitering'),
  ('ci-seed-5', 'Front Door', 'ci-seed-model-b', FALSE, 0.22,
   'A delivery was left by the door.', '${d1}', 1100, 70, 12.0,
   'routine', 'object_added')
ON CONFLICT DO NOTHING;

-- The Security Events tab is entirely empty without these: producing them
-- for real needs a running YOLO, which this container has no GPU for.
INSERT INTO security_events
  (clip_id, camera, event_type, severity, confidence, risk_score,
   evidence_quality, detail, subject_label, track_id, asset_name,
   asset_type, start_offset, end_offset, evidence, created_at)
VALUES
  ('ci-seed-1', 'Front Door', 'subject_present', 'suspicious', 0.91, 72.0, 0.80,
   'A person was present for most of the clip.', 'person', 1, '', '',
   0.0, 11.0, '{}', '${now}'),
  ('ci-seed-4', 'Side Gate', 'loitering', 'critical', 0.84, 88.0, 0.72,
   'A person remained near the gate for 40 seconds.', 'person', 2, '', '',
   2.0, 42.0, '{}', '${d1}'),
  ('ci-seed-5', 'Front Door', 'object_added', 'noteworthy', 0.66, 20.0, 0.55,
   'A parcel appeared near the door and stayed.', 'package', 3, '', '',
   5.0, 17.0, '{}', '${d1}')
ON CONFLICT DO NOTHING;

-- Per box per sampled frame, so one subject across several frames is
-- several rows -- which is exactly what the modal's chip summary has to
-- collapse by track_id.
INSERT INTO detected_objects
  (clip_id, label, confidence, box_x1, box_y1, box_x2, box_y2, track_id,
   frame_index, offset_seconds, frame_width, frame_height)
VALUES
  ('ci-seed-1', 'person', 0.94, 120, 80, 260, 400, 1, 0, 0.0, 640, 480),
  ('ci-seed-1', 'person', 0.92, 140, 82, 280, 402, 1, 1, 2.0, 640, 480),
  ('ci-seed-1', 'person', 0.90, 160, 84, 300, 404, 1, 2, 4.0, 640, 480),
  ('ci-seed-1', 'car',    0.88, 400, 240, 620, 420, 2, 0, 0.0, 640, 480)
ON CONFLICT DO NOTHING;

INSERT INTO battery_history
  (camera, battery_state, battery_level, battery_voltage, recorded_at)
VALUES
  ('Front Door', 'ok',   82, 1680, '${now}'),
  ('Driveway',   'ok',   64, 1610, '${now}'),
  ('Backyard',   'low',  18, 1450, '${now}'),
  ('Side Gate',  'ok',   91, 1705, '${now}')
ON CONFLICT DO NOTHING;

INSERT INTO camera_vehicle_signatures
  (camera, box, histogram, sample_count, updated_at)
VALUES
  ('Driveway', '[0.30,0.45,0.72,0.88]', '[0.1,0.2,0.3,0.4]', 12, '${now}')
ON CONFLICT (camera) DO NOTHING;
SQL

  local insert_status=$?

  # Read back rather than announce success. This script runs without
  # `set -e`, so a failed lookup or a psql that never received the SQL
  # would otherwise print OK and hand a completely empty database to the
  # assertions four steps later -- which is exactly what happened when the
  # container was still being looked up under its old addon_ prefix.
  local counts
  counts="$(printf '%s\n' \
    "SELECT (SELECT COUNT(*) FROM clips WHERE id LIKE 'ci-seed-%')
         || '/' || (SELECT COUNT(*) FROM analysis_results WHERE clip_id LIKE 'ci-seed-%')
         || '/' || (SELECT COUNT(*) FROM security_events WHERE clip_id LIKE 'ci-seed-%')
         || '/' || (SELECT COUNT(*) FROM detected_objects WHERE clip_id LIKE 'ci-seed-%')
         || '/' || (SELECT COUNT(*) FROM battery_history);" \
    | addon_psql -t 2>/dev/null | tr -d '[:space:]')"

  if [[ "$insert_status" -ne 0 || "$counts" != "8/4/3/4/4" ]]; then
    echo "Seeding did not put the expected rows in the add-on's database." >&2
    echo "  expected clips/analysis/events/detections/battery = 8/4/3/4/4" >&2
    echo "  got: ${counts:-<no response>} (psql exit ${insert_status})" >&2
    return 1
  fi
  echo "OK: seeded 8 clips, 4 analyses, 3 security events, 4 detections, 4 battery rows"
}

cmd_assert_seed_survived() {
  # The real database-durability check, and only possible because
  # seed-data puts actual rows in PostgreSQL. assert-persisted re-reads a
  # JSON settings file, which proves the /data volume came back; this
  # reads rows back out of the bundled PostgreSQL cluster *through the
  # app*, proving the cluster re-attached that volume and the data in it
  # is still queryable. A cluster silently re-initialized from scratch
  # comes up perfectly healthy on an empty database and would pass every
  # other check in this job.
  #
  # Over the add-on's own port rather than ingress: this runs after the
  # browser is gone, exactly as assert-persisted does.
  local body
  body="$(curl -sf --max-time 30 \
    "http://127.0.0.1:${ADDON_PORT}/api/clips?limit=50" || true)"
  if [[ "$body" != *"ci-seed-1"* ]]; then
    echo "The clips seeded before the restart are gone." >&2
    echo "  The PostgreSQL cluster under /data was not carried across the" >&2
    echo "  container being recreated - an update would wipe the library." >&2
    echo "  got: ${body:0:200}" >&2
    return 1
  fi
  echo "OK: clips seeded before the restart are still queryable after it"
}

cmd_assert_log_contains() {
  # Proves the add-on actually *consumed* what Supervisor stored. app.py
  # logs its resolved configuration at startup, so a value appearing there
  # after a restart is end-to-end evidence that options.json reached
  # AppConfig -- not merely that Supervisor persisted it.
  local pattern="${1:?grep pattern required}" description="${2:-$1}"
  local log
  log="$(ha_cli apps logs "$ADDON_SLUG" 2>&1 || true)"
  if ! printf '%s\n' "$log" | grep -qE "$pattern"; then
    echo "The add-on's log never showed: $description" >&2
    echo "Last 40 lines:" >&2
    printf '%s\n' "$log" | tail -40 >&2
    return 1
  fi
  echo "OK: add-on log shows $description"
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
  set-option)
    shift
    cmd_set_option "$@"
    ;;
  assert-option-rejected)
    shift
    cmd_assert_option_rejected "$@"
    ;;
  assert-capabilities) cmd_assert_capabilities ;;
  seed-data) cmd_seed_data ;;
  assert-seed-survived) cmd_assert_seed_survived ;;
  assert-log-contains)
    shift
    cmd_assert_log_contains "$@"
    ;;
  diagnostics)
    shift
    cmd_diagnostics "$@"
    ;;
  *)
    echo "Usage: $0 {prepare-addon-copy|wait-docker|serve-local-image <tar>|wait-core|discover|install|start|restart|assert-clean-log|assert-persisted <value>|assert-version <version>|enable-ingress-panel|set-option <key> <value>|assert-option-rejected <key> <value>|assert-capabilities|seed-data|assert-seed-survived|assert-log-contains <pattern> [description]|diagnostics <dir>}" >&2
    exit 64
    ;;
esac
