# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## Project overview

This is a **Home Assistant add-on repository** (the kind you add via *Settings →
Add-ons → Repositories*). It currently contains a single add-on:

- `blink_clip_downloader/` — "Blink Clip Downloader": polls the Blink API
  (via [blinkpy](https://github.com/fronzbot/blinkpy)) and downloads camera
  clips to local storage, with retention/quota management, HA notifications,
  a bundled-PostgreSQL clip library, a Vue 3 + PrimeVue web UI (Video.js
  player) including a one-camera-at-a-time Live View tab (blinkpy's
  live-view session, bridged through ffmpeg into HLS — see `live_view.py`),
  and optional AI-based suspicious-activity analysis of clips, including
  local-only face recognition that can auto-clear a clip's suspicious flag
  for approved household members.

Almost all work happens inside `blink_clip_downloader/`. **Run all commands
below from that directory unless noted otherwise** (pyright is the exception —
it must run from the repo root, see below).

Target platform is Home Assistant OS (`arch: aarch64, amd64`); it may work on
other HA install types but that's not the primary support target
(see `CONTRIBUTING.md`). The add-on's base image is Debian (`*-base-debian:trixie`,
glibc) as of 4.1.0, not Alpine — PyTorch (a dependency of the optional
computer-vision pipeline, see below) has no wheels for musl/Alpine on any
architecture.

## Directory layout (`blink_clip_downloader/`)

- `blink_downloader/` — the actual Python package.
  - `app.py` — `BlinkClipDownloaderApp`, the main run loop/entrypoint.
  - `config.py` — `AppConfig` dataclass; loads `/data/options.json` (HA
    add-on options) into typed config. Also defines per-camera config schema
    (`ai_camera_prompts`, `ai_camera_descriptions`, `ai_car_cameras`).
  - `downloader.py` — Blink API polling + clip download/thumbnail generation.
  - `database/` — clip library (`ClipDatabase`) against a **PostgreSQL 17
    server bundled and supervised inside this same container** (not SQLite —
    that was replaced in 5.0.0; see the Dockerfile and
    `rootfs/etc/services.d/postgresql`). New columns on an existing table
    need an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `schema.py`'s
    `_MIGRATIONS`, not just adding the column to `_SCHEMA`'s `CREATE TABLE
    IF NOT EXISTS` — that statement is a no-op for a table that already
    exists, so upgrading installs would never get the new column otherwise.
    A package since 6.0.6 (it was one 3,545-line module): `ClipDatabase` is
    still **one class with an identical public API**, assembled in
    `__init__.py` from one mixin per job — `clips.py` (the library and its
    archive lifecycle), `analysis.py` (verdicts and token spend),
    `detections.py` (boxes, security events, vehicle signatures),
    `faces.py` (enrollments and the bypass audit trail), `learning.py`
    (feedback tuning, activity baselines, scene drift), `queues.py` (the
    analysis and Drive upload queues), `cameras.py` (battery history and
    carrying a rename across every camera-keyed table), `legacy.py` (the
    pre-5.0.0 SQLite import) and `core.py` (pool lifecycle), over
    `schema.py` (the DDL) and `sql.py` (pure text/row helpers).
    Mixins rather than sub-objects (`db.clips.get(...)`) so that no caller
    or test had to change. Two rules worth knowing: a method that needs a
    *different* mixin's method goes on `ClipDatabase` itself (only
    `save_analysis` does, writing a verdict plus its detections and events
    together), which is what keeps each mixin independently type-checkable;
    and anything added to `cameras.py`'s rename path must cover **every**
    table with a `camera` column — there are 13, and the count is worth
    re-deriving from `schema.py` rather than trusting, since a missed one
    is this repo's most-repeated bug.
  - `analyzer/` — AI vision analysis, a package since 6.0.6 (it was a
    single 5,200-line module). `base.py` holds `BaseAnalyzer` —
    everything that is the same whichever provider is configured: frame
    extraction/down-selection, prompt assembly, the vision and security
    layers, prompt-cache/token accounting, verdict parsing, the risk
    override and the face bypass. `ollama_provider.py`,
    `moondream_provider.py`, `anthropic_provider.py` and
    `openai_provider.py` hold only what differs about talking to one API;
    `factory.py` holds `create_analyzer()`, the only module that knows all
    six at once. Every provider module carries the `_provider` suffix for
    a concrete reason: **pyright resolves a bare `import <x>` to a
    same-named file in the importing file's own directory** when the real
    package isn't installed, so `moondream.py` doing `import moondream`
    resolved to itself and failed CI — where the optional GPU-only
    `moondream` package is absent — while passing locally, where it is
    installed. `tests/test_module_names.py` guards the whole package
    against that collision now. `__init__.py` is
    a facade: every name the old module exposed is re-exported, so
    `from .analyzer import ...` is unchanged for callers — reach into a
    submodule only for an internal the facade deliberately doesn't
    export. See **AI provider architecture** below.
  - `model_catalog.py` — per-provider model reference data: which ids can
    see images, which accept a structured-output schema, and per-token
    pricing, plus the small pure functions that read it. Split out of
    `analyzer` because it changes on the providers' schedule, not this
    add-on's; it imports nothing from `analyzer`.
  - `prompt_segments.py` — the individual blocks a clip-analysis prompt is
    assembled from (time of day, anomaly alert, scene baseline, motion
    trajectory, recent corrections, zone motion, vision hints, output
    rules). Each renders one block from plain arguments and returns `""`
    when it has nothing to say, which is how `_build_prompt` decides
    whether to include it. Same reasoning as `model_catalog.py`: this is
    text reworded on its own schedule, easier to review against the whole
    set of segments than buried in the request/response plumbing.
    `_camera_context_segment` and `_car_protection_segment` deliberately
    stay on `BaseAnalyzer` — both read configured analyzer state, and the
    latter is part of the safety-critical protected-vehicle path.
  - `frame_motion.py` — pure frame arithmetic: grayscale thumbnails,
    inter-frame diff magnitudes/centroids, the scene-baseline thumbnail,
    the motion-trajectory phrase, and the share of a clip's motion falling
    inside a car zone. Same reasoning as `model_catalog.py` — it is image
    math that happens to be *used* during analysis, not part of deciding
    what a clip means, and it imports nothing from `analyzer`. Uses
    `security/geometry.py`'s `point_in_polygon` rather than keeping the
    second, identical copy the analyzer used to carry.
  - `ffmpeg_output.py` — reading what ffmpeg wrote: splitting its
    concatenated-JPEG stdout into frames, and condensing its stderr into
    one loggable line. A leaf module (stdlib only) because both
    `analyzer/` and `media_server/` shell out to ffmpeg and need it,
    and `media_server/` importing `analyzer` for two small pure
    functions would drag `aiohttp` and the whole `security` package in
    behind them. Both modules used to carry their own identical copy for
    exactly that reason; same fix as `frame_motion.py` taking
    `point_in_polygon` from `security/geometry.py`.
  - `moondream_finetune.py` — `MoondreamFineTuneManager`, an async wrapper
    over Moondream Cloud's fine-tuning REST API. **Not an analyzer** — no
    clip, prompt or verdict is involved; it backs the AI tab's Fine-Tuning
    panel and every method swallows transport failures rather than raising,
    since nothing in clip analysis depends on it.
  - `analysis_queue.py` — async queue that feeds clips to the analyzer.
  - `face_enrollment.py` — the Biometrics tab's enrollment logic, numpy
    only: `FaceCandidateStore` (faces a scan found, held server-side by
    opaque id until enrolled or expired — the browser never sees an
    embedding), near-duplicate suppression, grouping candidates by person,
    and `review_enrollments` (flags a photo unlike the person's others, or
    one that also matches someone else). Its thresholds were measured on
    real faces shrunk to camera size; the comments say what they are.
    `media_server/faces.py` is the route side; `vision/faces.py`'s
    `FaceEmbedder.detect()` finds the faces.
  - `security/` — the structured security layer (`events.py`, `tracks.py`,
    `geometry.py`, `zones`/`assets.py`, `vehicles.py`, `detector.py`,
    `sounds.py`, `scoring.py`, `evidence.py`, `narrative.py`,
    `pipeline.py`). Turns
    `vision/`'s per-frame boxes into typed `ObjectTrack`s, deterministic
    `SecurityEvent`s, a 0-100 risk score and an evidence-quality score, and
    renders them as prompt text the AI provider verifies rather than
    re-derives.
    `sounds.py` is the one non-geometric source: it turns the optional
    audio stage's `(label, confidence)` pairs into the three
    heard-not-seen events (`AUDIO_EVENTS`), and the rules around it are
    load-bearing rather than incidental. A heard event is **not damped by
    evidence quality and not offset by the known-person discount** —
    both of those measure the *picture*, and a sound carries from places
    the camera cannot see, so damping by them would mute audio exactly
    when it is the only witness (a dark clip, or nothing in frame). Only
    sounds with a near-zero routine-household rate raise an event at all;
    speech, footsteps, a dog, a car, a door and a power tool stay hints,
    because an event that fires daily is noise wearing a security label.
    That same test is why `GLASS_BREAK_HEARD`/`GUNSHOT_HEARD` are in
    `BYPASS_BLOCKING_EVENTS` and `ALARM_HEARD` is not (a passing
    ambulance matches it). Correspondingly, `_assess_security` runs when
    there are tracks **or** audio — requiring tracks meant breaking glass
    with nobody in frame produced no event at all, which is most of the
    cases audio exists for. **Imports nothing from `vision/` and no heavy optional
    dependency** — every CV stage's output is reduced to plain numbers
    before it arrives, so the whole layer loads and is tested with no
    torch/opencv installed. `vehicles.py` is the one to read first: it
    decides *which* car in frame is the protected one (zone occupancy +
    a learned per-camera parking position + a learned colour fingerprint),
    and is allowed to answer "none of them", which is what stops a
    neighbour's car being treated as yours.
    Four rules around the protected car that are easy to break, each pinned
    by a scenario in `tests/test_security_scenarios.py`:
    - **Whether a contact was confirmed is recorded, not re-derived.** The
      detector writes `evidence["confirmed"]` on contact events (and carries
      it onto `retreat_after_contact`) when it grades the contact, *before*
      an uncertain car identification scales every event's confidence by
      0.6. `scoring.py`'s unconfirmed-contact ceiling (a bare 2D overlap may
      not be the reason a clip reaches 75) reads that flag; reading
      `confidence < 0.6` instead held confirmed touches on a no-zone
      single-car camera at 74.
    - **Depth, segmentation and pose examine one subject per clip**
      (`vision/pipeline.py`'s `_select_pair`): nearest by the in-front ground
      gap, and a person within `near_feet` of the car ahead of any animal —
      only a person's contact can reach suspicious/critical.
    - **The far-side rule** (depth "similar" plus an overlapping outline
      counts as at the car) applies only to feet at or above the car's
      ground line. Depth calls most passers-by in front of a car "similar",
      so letting it overrule a visible gap forced alerts on them.
    - **A possible impact's speed-up is in the subject's own heights per
      second** (`max_speed_increase_at`, bar 1.2 = a jog), and only at the
      car. Frame widths made walking away from a parked car an impact on
      any close camera — which also withholds the face bypass.
  - `vision/` — optional, off-by-default computer-vision enhancement
    pipeline, one module per stage since 6.0.6 (it was one 2,490-line
    module, already written as "Stage 1..6 + Orchestrator" banners, which
    is exactly where it was cut): `enhance.py` (OpenCV frame
    preprocessing), `detection.py` (YOLO detection + ByteTrack tracking),
    `depth.py`, `contact.py` (SAM2 segmentation), `pose.py` and `faces.py`
    (local-only face recognition), sequenced by `pipeline.py`.
    Layered on top of `analyzer/base.py`'s prompt pipeline via
    `BaseAnalyzer.attach_vision_pipeline()` — each stage produces a hint
    string appended to the same prompt, never replacing the configured AI
    provider's judgment. Every stage lazily imports its own heavy
    dependency (torch/ultralytics/opencv/transformers/facenet-pytorch) and
    reports itself unavailable rather than raising if missing — none of
    them are required for the add-on's core features to work.
    Two support modules, and the rule that goes with them: `runtime.py`
    holds what is genuinely **process-wide** (the single native-import
    lock, the single CV concurrency semaphore, the torch/CPU availability
    checks, the YOLO weights cache dir) and `imaging.py` the pure image
    arithmetic several stages share. Stages reach both **through the
    module** (`runtime.torch_cpu_compatible()`, not a `from .runtime
    import`) — a name import would copy the reference into six stage
    modules, which both hides that there is only one of each and forces a
    test simulating "no torch" to pick a different patch target per stage.
    `__init__.py` re-exports the public surface; a test wanting an internal
    imports it from the stage that owns it.
  - `media_server/` — aiohttp HTTP server: REST API + serves the built Vue
    app as static files (`support.py`'s `_STATIC_DIR`, `app_shell.py`'s
    `_handle_index`). See **Web UI** below — the frontend itself lives in
    `frontend/`, a sibling of `blink_downloader/`.
    A package since 6.0.6 (it was one 3,707-line module, 139 methods on one
    class). `MediaServer` is still **one class with an identical public
    API**, composed in `__init__.py` from **one mixin per tab of the web
    UI** — so the module to open is the one named after the tab you are
    changing: `app_shell` (the SPA, `/health`, Blink auth), `library`,
    `status`, `liveview`, `security_feed`, `ai`, `usage`,
    `camera_configs`, `vehicles`, `security_events`, `sync_module`,
    `feedback`, `faces`, `finetune`, `storage`, `automations` — over
    `support.py` (middleware, CSP, JSON parsing, paging, shared error
    strings) and `core.py` (`_MediaServerBase`, which declares the
    dependencies and runtime state every mixin reads, so each one
    type-checks alone).
    Things worth knowing before editing it:
    - **Each mixin registers its own routes** via `_register_<area>_routes`,
      called by `_build_app`. Adding an endpoint is one file, not a handler
      here and a route line far away. `tests/test_media_server_routes.py`
      fails if a registrar is never called, if a handler has no route, or
      if one route shadows another — the first of those is a mistake that
      otherwise just makes a whole tab 404 with nothing pointing at why.
    - **Route order is not load-bearing**: aiohttp indexes plain paths ahead
      of `{placeholder}` ones, so `/api/ai/feedback/stats` wins over
      `/api/ai/feedback/{clip_id}` regardless of registration order. That is
      asserted rather than assumed.
    - **A method needing another mixin's method** is expressed as
      inheritance, not a comment: `VehicleRoutesMixin` extends
      `CameraConfigsRoutesMixin` (a car zone *is* a camera-config field) and
      `LibraryRoutesMixin` extends `StorageRoutesMixin` (deleting a clip has
      to delete its Drive copy). A subclass must be listed **before** its
      base in `MediaServer`'s bases or C3 linearization fails. `rename_camera`
      spans three areas, so it sits on `MediaServer` itself.
    - **`_STATIC_DIR` is anchored on the parent package**
      (`Path(__file__).resolve().parent.parent`), not on `support.py` — a
      plain `.parent` points one directory too deep now and 500s every page.
    - **A test patching a module-level name must target the route module
      that resolves it**, e.g. `media_server.ai._is_moondream_installed`,
      not the package facade. Rebinding a name on `__init__.py` does not
      change what an already-imported route module looks up.
  - `live_view.py` — `LiveViewManager`, backing the Live View tab. Bridges
    blinkpy's live-view session (a proprietary binary protocol relayed onto
    a local raw-TCP socket — not RTSP) through an ffmpeg subprocess into a
    short rolling HLS playlist, served through `media_server/`'s existing
    routes/port rather than a new one (HA ingress only proxies HTTP/
    WebSocket, so the raw TCP socket itself is unreachable from a browser
    regardless). Exactly one session is active at a time; starting a
    different camera stops whichever was active first. A background sweep
    loop enforces an idle timeout and a hard cap per session, on top of the
    frontend stopping its session on unmount (navigating away) — see the
    module's own docstring and `tests/test_live_view.py` for the session
    lifecycle/crash-handling details.
  - Security Feed tab (grid of near-live camera snapshot tiles) has no
    dedicated module — it's a few methods on `BlinkDownloader`
    (`get_camera_snapshot`, reading blinkpy's `camera.image_from_cache`/
    `camera.thumbnail`, never `camera.snap_picture()` — see that method's
    docstring for why) plus routes directly on `media_server/`
    (`/api/security-feed/*`), independent of `LiveViewManager`. Settings
    (`cameras`/`columns`/`refresh_seconds`) persist to
    `/data/security_feed_settings.json`, same convention as
    `vehicle_settings.json`.
  - `ha_entities.py` — the extra Home Assistant entities the add-on
    publishes beyond `sensor.blink_downloader_status`: the two storage
    percentage sensors (`sensor.blink_local_storage`,
    `sensor.blink_cloud_storage`) and the `blink_clip_analyzed` /
    `blink_camera_battery_low` events. **Those names are a user-facing
    contract** — the Automations tab's builders generate YAML against them
    and users' own automations reference them, so renaming one is a
    breaking change. The cloud sensor is deliberately named for the role,
    not for Google Drive (OneDrive is planned); which backend is in use is
    the `provider` attribute. Pure payload builders plus a thin publisher
    class, so the number-shaping is testable without a notifier; `app.py`
    calls it once per poll cycle and once at startup, and hands its two
    event methods to `analysis_queue.py`/`battery_monitor.py` as callbacks
    rather than letting either import a notifier.
  - `ha_config.py` — writes automations/scripts/scenes into Home Assistant
    for the Automations tab's "Create in Home Assistant" buttons, through
    Core's own config API (`POST /core/api/config/<domain>/config/<id>`).
    Works because Supervisor validates the add-on's token and then
    re-issues the call to Core *as the Supervisor user*, which Core creates
    in the admin group — Core's config views are `@require_admin`, and the
    add-on's own token is never forwarded. Core reloads the domain itself
    via its `post_write_hook`, so this deliberately does not call reload.
    Only those three kinds have a REST config API at all: blueprints,
    helpers and Lovelace are WebSocket-only and `configuration.yaml` blocks
    have nothing, which is why those recipes stay copy-only.
    `normalize_config()` carries the per-kind unwrapping (a script's YAML
    nests its body under its own id; a scene's is a one-item list whose
    `id` Core injects itself). It reports back the **name** Core lists the
    object under, never an entity id: only a script's entity id is its
    object id — Core derives an automation's and a scene's from the
    `alias`/`name`, and several recipe aliases embed the threshold the
    user chose, so the entity id is not predictable from the recipe at
    all (see `created_name`).
  - `event_watcher.py`, `notifier.py`, `notification_channels.py`,
    `digest.py`, `battery_monitor.py`, `archiver.py`, `storage.py`,
    `library_scanner.py`, `tracker.py`, `manifest.py` — supporting modules
    (event-driven fast-poll, HA/mobile/email/Discord notifications, daily
    digest, per-camera battery tracking/alerts, cold storage archiving,
    storage quota enforcement, filesystem library scan/reconcile,
    download-session tracking, clip manifest export).
- `frontend/` — the Vue 3 + PrimeVue + Pinia web UI, a separate npm project
  (its own `package.json`/`node_modules`/toolchain). See **Web UI** below,
  including `frontend/e2e/`'s Playwright interaction tests.
- `tests/` — pytest test suite, one `test_<module>.py` per module above, plus
  `conftest.py` with shared fixtures (`base_config`, `sample_clip`,
  `options_file`, `tmp_download_dir`).
- `scripts/standalone_server.py` — boots a real `MediaServer` against a
  seeded throwaway Postgres database with no Home Assistant, blinkpy, or
  Docker involved; the backend `frontend/e2e/`'s Playwright tests run
  against (see **Web UI** below). Not shipped in the Docker image.
- `config.yaml` — HA add-on manifest (options schema, version, ports, maps).
- `pyproject.toml` — package metadata, dependencies, pytest/coverage/pyright
  config.
- `CHANGELOG.md` / `DOCS.md` — user-facing changelog and add-on docs.
- `blink_downloader.egg-info/` — **generated** by `pip install -e`; do not
  hand-edit, just re-run the install after changing dependencies or version
  (see below).

Repo root also has `pyrightconfig.json` and `pytest.ini` — these exist so
tools can be invoked from the root the same way CI does.

## AI provider architecture

`analyzer/base.py` defines `BaseAnalyzer` (ABC); six concrete analyzers
live one provider-family per module, selected via `analyzer/factory.py`'s
`create_analyzer()` keyed on `ai_provider`:

| `ai_provider`     | Class                    | Module                   | Notes                                   |
|-------------------|--------------------------|--------------------------|------------------------------------------|
| `ollama`          | `ClipAnalyzer`           | `ollama_provider.py`     | Local/LAN Ollama server                   |
| `ollama_cloud`    | `OllamaCloudAnalyzer`    | `ollama_provider.py`     | Hosted Ollama Cloud API (subclasses `ClipAnalyzer` — same wire format) |
| `moondream_cloud` | `MoondreamCloudAnalyzer` | `moondream_provider.py`  | Moondream Cloud API, no model selection   |
| `moondream_local` | `MoondreamLocalAnalyzer` | `moondream_provider.py`  | Local moondream package, requires an **NVIDIA/Apple Silicon GPU** (any arch since the 4.1.0 Debian base image switch) |
| `anthropic`       | `AnthropicAnalyzer`      | `anthropic_provider.py`  | Claude vision models                      |
| `openai`          | `OpenAIAnalyzer`         | `openai_provider.py`     | GPT vision models                         |

`MoondreamFineTuneManager` is a separate helper class (not an analyzer) that
wraps the Moondream Cloud fine-tuning API — it lives in
`moondream_finetune.py`, not the `analyzer` package. The model capability/pricing
tables live in `model_catalog.py` for the same reason: both are edited for
reasons that have nothing to do with how a clip is analyzed.

`BaseAnalyzer._build_prompt(camera, ...)` assembles the analysis prompt per
clip. Key detail: **`ai_car_cameras` empty means "applies to all cameras"**
(documented at `config.py` next to the field) — this is a deliberate default,
not a bug, but it means an unconfigured camera silently inherits car-distance
language unless `_build_prompt` explicitly excludes it. When touching the
prompt-building logic, keep the OUTPUT RULES example phrase and any
scenery/location language scoped to what a given camera can actually see —
don't let language meant for one camera (e.g. "the driveway") leak into a
prompt for a camera that can't see that thing.

## Camera config (`camera_configs.json`)

Per-camera `description`, `custom_prompt`, `is_car_camera`, and `car_zone`
live in a single JSON file (`/data/camera_configs.json`) that the web UI
edits directly via `GET/PUT /api/ai/camera-configs` — it is the single
source of truth (not `config.yaml` options). This is a **full-array
replace** on every `PUT`, not a merge: `description`/`custom_prompt` are
edited from the AI tab's Camera Configurations section, while
`is_car_camera`/`car_zone` are edited from the **Vehicles** tab — each of
those two Vue components must round-trip the fields it doesn't own
unchanged, or saving from one would silently clobber edits made from the
other. The `is_car_camera` checkbox is what populates `ai_car_cameras`.

The one car-protection setting that isn't per-camera —
`ai_car_description` — is similarly overridable from the web UI via a
sibling file, `/data/vehicle_settings.json`
(`GET/PUT /api/vehicle/settings`), falling back to the `config.yaml` option
only until that file is first written.

## Web UI (`frontend/`)

The web UI is a **Vue 3 + PrimeVue 5 + Pinia** single-page app, a separate
npm project at `frontend/` (its own `package.json`, `node_modules`,
`vite.config.ts`, `eslint.config.js`). It is **not** an embedded string in
`media_server/` — an earlier version of this add-on worked that way, but
that was fully replaced; the ~2,900-line dead `_HTML` remnant of it was
removed in 5.0.0.

- **Build & serving**: `npm run build` (Vite) writes straight into
  `blink_downloader/static/` — the Dockerfile's `frontend-builder` stage
  runs this before the image is packaged, and `media_server/app_shell.py`'s
  `_handle_index`/`_STATIC_DIR` serve that output. Running the Python test
  suite alone (no `npm run build` first) means `static/` won't exist;
  `_handle_index` reports that clearly (500 + "run `npm run build`") rather
  than serving nothing. Video.js is bundled into the Vue build's own JS
  (`components/library/ClipModal.vue`), not loaded from a CDN.
- **Page-per-tab convention**: each nav tab is a `<TabName>Page.vue` in its
  own `components/<area>/` directory (e.g. `components/vehicles/VehiclesPage.vue`,
  `components/biometrics/BiometricsPage.vue`), composed of the page itself
  plus one or more self-contained "section" components that each own their
  own `load()`/`save()` — see `components/ai/AiPage.vue` for the reference
  pattern (a page component + several independent `*Card.vue`/`*Section.vue`
  children). `ModelsPage.vue` is reference-only — it fetches nothing and is
  simpler, but isn't the pattern to copy for a data-driven tab.
- **Automations tab** (`components/automations/`): five builders inside one
  PrimeVue `Tabs` (`lazy`, so only the open panel exists — a Playwright
  selector never collides with a hidden panel, and a form's edits are lost
  on tab switch by design). The catalogues live in `recipes/` as data:
  `automations.ts`/`scripts.ts` are lists of `Recipe`s (fields + a pure
  `build(values)` returning YAML), `dashboard.ts`/`blueprints.ts` are the
  Dashboards and Blueprints content, `shared.ts` holds the fragments more
  than one recipe emits, and `types.ts` the field model and YAML helpers.
  Adding an automation is a few lines in a catalogue, not another
  hand-written snippet in a template. Everything the generated YAML
  references must be something the add-on really publishes — see
  `ha_entities.py` above. `App.vue`'s `?kiosk=1&tab=<id>` mode exists for
  the Dashboards builder's iframe-card route.
- **Nav wiring**: adding/removing a tab touches four places —
  `components/layout/AppSidebar.vue`'s `TabName` type + `TABS` array (in
  that file's plain `<script>` block, not its `<script setup>` one: `TABS`
  and the `TAB_NAMES` derived from it are runtime *exports*, which
  `<script setup>` cannot do, and `App.vue`'s kiosk mode validates
  `?tab=` against them),
  `App.vue`'s imports + `<div id="page-X">` blocks,
  `components/icons/paths.ts`'s `ICONS` map (add a `tab-X` entry; icons are
  plain path/rect/circle data, not separate `.vue` files — see `AppIcon.vue`),
  and an `#page-X { overflow-y: auto; }` override in `assets/styles/base.css`
  (grouped with `#page-vehicles`/`#page-biometrics`/`#page-storage`/
  `#page-liveview`) unless the page's content is certain to always fit
  within the viewport — `.page` defaults to `overflow: hidden` (the
  fixed-height sidebar/content shell), so a page that doesn't opt in just
  clips its content with no scrollbar. The Storage tab shipped without this
  once; it only surfaced via live browser testing under a real Home
  Assistant OS install, not any automated test. Current nav order: Library,
  Live View, Security Feed, Automations, Sync Module, Status, AI, AI Usage,
  Models, Security Events, Vehicles, Biometrics, Storage. Note the two
  similarly-named tabs are unrelated: **Security Feed** is the grid of
  near-live camera snapshots; **Security Events** is the structured
  security-event timeline (`components/security/`, backed by
  `blink_downloader/security/`). The latter's tab *id* is still `security`
  (`data-tab="security"`, `#page-security`, `components/security/`) —
  6.0.1 renamed only the label and the page heading, deliberately leaving
  the id alone so no selector, route or persisted state had to migrate.
- **API client**: every backend call goes through `api/<area>.ts` modules
  built on `api/client.ts`'s `apiGet`/`apiPost`/`apiPut`/`apiPatch`/`apiDelete`
  helpers (thin `fetch` wrappers, ingress-path-aware via `env.ts`). Add new
  endpoint bindings there, typed against `api/types.ts`, rather than calling
  `fetch` directly from a component.
- **Pinia stores** (`stores/`): only used for state genuinely shared across
  tabs/components (theme, auth polling, toast queue, confirm-dialog,
  library camera selection, cross-tab refresh signal). Page-owned data
  (fetched lists, form state) stays in local component `ref()`s — don't
  reach for a store just because a page fetches something.
- **PrimeVue usage**: most existing settings-style sections predate a
  PrimeVue-forward convention and use hand-rolled `<div style="...">` markup
  with utility CSS classes (`.btn`, `.card`, `.status-card`, see
  `assets/styles/base.css`) — `Button`/`Tag`/`Dialog` are the exceptions.
  Newer additions (Vehicles, Biometrics, the Automations notification-test
  panel) deliberately lean on real PrimeVue components (`Card`, `Message`,
  `ToggleSwitch`, `FileUpload`, `Select`, `Textarea`, `InputText`) — prefer
  that vocabulary for new UI rather than adding more hand-rolled markup.
  `main.ts` configures the shared theme (`@primeuix/themes`, dark mode via
  `.dark` class on `<body>`).
- **Testing**: Vitest + Vue Test Utils, `*.spec.ts` beside each source file.
  `test-setup.ts` polyfills jsdom gaps PrimeVue components hit
  (`matchMedia`, `ResizeObserver`) — add to it rather than working around
  the crash per-test. Mounting anything using a PrimeVue form component
  (`Select`, `Textarea`, `ToggleSwitch`, `FileUpload`, ...) receives the
  shared licensed PrimeVue plugin from `src/test-setup.ts`; do not install
  `PrimeVue` again in individual specs or Vue warns that the plugin was
  already applied. For a component's own `v-model`/`defineModel`, wire a real
  two-way test harness (pass
  `'onUpdate:x': (v) => wrapper.setProps({ x: v })`) rather than a no-op
  handler — the no-op silently breaks any test that expects selections to
  accumulate across multiple interactions. `PointerEvent`/`MouseEvent`
  position properties (`clientX`/`clientY`, not `offsetX`/`offsetY`) can't be
  set through Vue Test Utils' `trigger(type, options)` for events that
  inherit them from a different prototype (a VTU/jsdom quirk) — dispatch a
  real `new PointerEvent(...)` directly on the element instead when a test
  needs a specific pointer position (see `VehicleZonePicker.spec.ts`).
  A PrimeVue component that owns its own id prop (`InputNumber`, `Select`,
  `MultiSelect`, `ToggleSwitch`) takes `input-id`, while `InputText` and
  `InputMask` are plain inputs that take `id` — mixing the two leaves the
  `<label for>` pointing at nothing, which no test that clicks the element
  directly can see (`RecipeFieldInput.spec.ts` asserts the association
  itself for exactly that reason). `MultiSelect`'s id lands on its hidden
  input, so a Playwright test clicks the widget (`.p-multiselect`) instead.
  The generated-YAML specs parse their output with `js-yaml` (a
  devDependency, test-only) rather than string-matching it — string
  assertions cannot tell valid YAML from a plausible-looking indentation
  bug.
  Coverage threshold is 80% (`vitest.config.ts`), mirroring the Python side's
  `fail_under = 80` — actual coverage on this codebase runs ~98-99%.
- **E2E testing** (`frontend/e2e/`, Playwright's own test runner —
  `@playwright/test`, not Vitest): real interaction tests against a real
  backend — `scripts/standalone_server.py` boots an actual `MediaServer`
  against a throwaway Postgres database seeded with fake clips (12
  "distribution" clips across 3 cameras/3 sources for filter/count
  assertions, 2 dedicated "Test Scratch" clips for star/tag mutation tests,
  and 3 pre-archived clips on real camera names for the Storage tab — see
  the script's `_ARCHIVE_CLIPS` for why reusing real cameras there was
  deliberate — see the script for the exact layout and why mutating tests
  use their own clips) and a real `ClipAnalyzer` pointed at an unreachable port
  (fails fast, `ai_online: false`) so the AI/AI Usage tabs — both gated
  server-side on `analyzer is not None` — have something real to render
  instead of "AI Analysis Not Configured". This is deliberately distinct
  from the root `e2e/` directory, which smoke-tests that the packaged
  Docker container boots at all with no real data (every tab lands on its
  empty state there) — this suite instead proves specific web UI workflows
  actually work end to end against a real frontend+backend+DB round trip,
  the class of bug neither Vitest's mocked `fetch` nor pytest's
  browser-less tests can catch (it has already caught one for real: several
  settings-save endpoints used to return `{"saved": true}` even when the
  file write silently failed — fixed in 5.3.3; see `_redirect_data_files`
  in the script for why `/data` needs redirecting here in the first
  place). Covers Library
  (filter/star/tag/modal, including the clip modal's theater mode/autoplay/
  loop/prev-next-nav/download-link/AI-panel-analyze-now), Vehicles, Status,
  AI (including Test Analysis) + AI Usage, Automations, Sync Module,
  Models, Security Feed, Security (timeline/filters/evidence/open-clip,
  against directly-seeded `security_events` rows — producing them for real
  would need a running YOLO), and Storage (Archived Clips list/expand/
  camera-filter/delete — all DB-backed; Google Drive only as far as its
  disconnected/not-configured state, since exercising a real connection
  needs actual OAuth credentials)
  — also Live View and Biometrics, each via its own unlock trick (below);
  neither is a mock in the sense of faking application logic, just a fake
  data source feeding the real code paths. Security Feed is
  unlocked the same cheap way the AI tab is (a real dependency pointed at
  something that fails fast, not a mock): `list_camera_names`/
  `get_camera_snapshot` are narrow callables MediaServer takes regardless of
  blinkpy, so the script wires in a fake camera list plus a real (tiny,
  Pillow-generated) JPEG per camera — one camera deliberately returns no
  snapshot, exercising the "No snapshot available yet" placeholder path
  too. Live View similarly wires in a real `LiveViewManager` with a fake
  `get_camera` whose `init_livestream()` always raises — this exercises
  the camera picker and a genuine (not mocked) `LiveViewError` surfaced as
  a toast through `live_view.py`'s real code path, but actually starting a
  session needs a real Blink stream feeding a real ffmpeg process, out of
  reach here — `live-view.spec.ts` covers the rest (an active session,
  switching cameras mid-session, the server ending a session with an
  error) via `page.route()` mocking the `/api/liveview/*` responses
  instead, the same "mock the API layer, not the application" approach
  `mocked-integrations.spec.ts` uses for Google Drive's connected state.
  Biometrics is unlocked via `_force_face_recognition_available()`
  patching `media_server.faces.is_face_recognition_available` (the route
  module that resolves the name, not the package facade) to always return
  `True` (real dependency: `facenet_pytorch`, part of the optional CV-
  pipeline extra, genuinely absent in this lightweight test environment),
  plus `_E2EFaceEmbedder` standing in for the models themselves: it finds
  the same three deterministic faces in any readable image (two people and
  one too blurred to offer), so the whole enroll flow — scanning a real
  ffmpeg-extracted clip, collapsing duplicate shots, grouping, enrolling
  from server-held candidates, serving the stored thumbnail, recognizing
  the enrolled face on the next scan — runs through the real server code.
  Its 4-d embeddings share a space with the seeded enrollments, chosen so
  nothing matches by accident and one of Riley's photos is flagged by the
  photo review. The Vehicles tab's zone-drawing
  canvas depends on its background `<img>` firing a real `load` event,
  which needs a real thumbnail file on disk — solved by giving `Test
  Scratch` a real, `ffmpeg`-generated video (the same fixture Biometrics'
  enrollment test needs), not a placeholder file. `vitest.config.ts` excludes `e2e/**` from Vitest's own test
  discovery — the two runners don't overlap. Requires a reachable Postgres
  and `npm run build` having already produced `blink_downloader/static/`
  (see `playwright.config.ts`'s `webServer`, which starts/stops the
  standalone server for you around the whole run — `workers: 1`, since
  every test shares that one backend/database for the whole run, so a
  mutating test must use its own dedicated data rather than touch anything
  a different spec file's assertions depend on, regardless of run order).
  Shared overlay components (`HelpOverlay`/`PromptOverlay`/`TwoFAOverlay`/
  `ConfirmDialog`) reuse the same `.modal-bg`/`.modal-title`/`.modal-close`
  class names as `ClipModal` — scope locators to the specific open modal
  (`.modal-bg.open`) rather than querying those classes unscoped, or
  Playwright's strict mode throws on the multiple matches.
- **E2E coverage** — `npm run test:e2e:coverage` (separate from the fast,
  default `npm run test:e2e`, same relationship as Vitest's `test`/
  `test:coverage`) builds with `VITE_COVERAGE=true`, which switches on
  `vite.config.ts`'s `vite-plugin-istanbul` plugin to instrument `src/**`
  before running the suite — a plain `npm run build` (what the Dockerfile
  actually runs) never sets that env var and ships uninstrumented, verified
  by grepping the built bundle for `__coverage__` and finding none. Each
  test's `window.__coverage__` is collected by a shared Playwright fixture
  (`e2e/coverage-fixtures.ts`, which every spec imports `test`/`expect`
  from instead of `@playwright/test` directly) into `.nyc_output/`, then
  merged and rendered into `coverage-e2e/` (html/lcov/cobertura, same
  reporters as Vitest's own coverage) by `e2e/generate-coverage-report.mjs`
  — **not** `nyc report` itself, which silently dropped all but a handful
  of the real source files when pointed at these externally-collected
  files (confirmed by merging the identical files by hand with
  `istanbul-lib-coverage` and getting the correct, full set) — the script
  calls that same library, plus `istanbul-lib-report`/`istanbul-reports`,
  directly instead. `vite.config.ts`'s `sourcemap` is `'hidden'` only inside
  the `COVERAGE` build (istanbul needs sourcemaps to map instrumented output
  back to source; `'hidden'` generates them without adding a
  `sourceMappingURL` comment to the served JS, which as a side effect also
  quiets `vite-plugin-istanbul`'s own console notice about auto-enabling
  them) — the plain/shipped build stays `false`. CI uploads this report to
  Codecov under its own `e2e` flag (`fail_ci_if_error: false` — never blocks
  CI), but `codecov.yml`'s `project.default` status check is explicitly
  scoped to `flags: [backend, frontend]`, not all flags — istanbul (e2e) and
  v8 (Vitest) count "total statements" differently for the same
  `frontend/src/` files, so leaving `default` unscoped would let the e2e
  flag move the combined backend+frontend score for reasons unrelated to an
  actual regression in either. The `e2e` flag instead gets its own
  `informational: true` status check — visible on the PR, never gates it.
  **A navigation at the end of a test throws that test's coverage away.**
  The fixture reads `window.__coverage__` once, after the test body and its
  hooks have run, and a `page.goto()`/`page.reload()` wipes the counters the
  page had accumulated. A trailing navigation in an `afterEach` — cleaning
  up by driving the UI back to a known state — therefore discards
  *everything* that test exercised, while the test still passes, so the
  symptom is a spec whose target file's coverage does not move at all. Clean
  up over HTTP with `page.request` instead (it needs no page, and is
  faster), or leave state alone when the test never wrote any. The same
  applies to a mid-test reload: only what happens after the last navigation
  is counted, which is a fair price for proving persistence but should be a
  deliberate choice. Two specs were written this way in 6.0.6 and
  contributed zero branches until it was noticed.
  No enforced coverage threshold either — this is a visibility tool for
  "what does this suite actually exercise", not a merge gate.
- Responsive/mobile conventions carried over from the pre-Vue UI still
  apply: grid layouts use `minmax(min(Npx,100%),1fr)` (not bare
  `minmax(Npx,1fr)`) so columns shrink instead of overflowing narrow
  viewports, and anything `position:fixed` (toasts, modals) needs an
  explicit width/left constraint or `calc(100vw - ...)` cap.
- `media_server/` (the Python side of the API) is **not** excluded from
  the Python coverage requirement — new server-side logic needs coverage
  via `tests/test_media_server.py`, typically using the aiohttp `TestClient`
  fixtures already in that file.

### Frontend commands (run from `frontend/`)

```bash
npm run dev            # local dev server
npm run build           # production build -> ../blink_downloader/static
npm run lint            # eslint .
npm run format:check    # prettier --check .
npm run type-check      # vue-tsc -b
npm test                 # vitest run
npm run test:coverage   # vitest run --coverage
npm run test:e2e        # playwright test (needs Postgres; does NOT build - run `npm run build` first)
npm run test:e2e:coverage # same, instrumented -> coverage-e2e/ (slower: full rebuild + per-test collection)
```

CI folds the first six into the existing Python `lint`/`test` jobs
(`.github/workflows/ci.yaml`), gated to run once (the `python-version ==
"3.13"` matrix leg) rather than as a separate job. `test:e2e:coverage`
runs in its own `frontend-e2e` job instead (own Postgres service, not
matrixed).

## Face-recognition suspicious-flag bypass (safety-critical — read before touching)

An approved, recognized household member can auto-clear a clip's
suspicious flag (`analyzer/base.py`'s `BaseAnalyzer._face_bypass_applies` /
`_personalize_summary`, wired into `_analyze_clip_locked` right after
`parse_response()`). This is deliberately **all-or-nothing per clip**: it
requires at least one approved match **and zero** unrecognized or
recognized-but-not-approved faces anywhere in the clip's sampled frames
(`vision/faces.py`'s `FaceRecognizer.recognize()` → `FaceRecognitionResult`, via
`_face_match_is_unambiguous`). A single stranger standing next to an
approved family member must still get flagged — **do not loosen this
condition** without equally strong justification; a false bypass here is a
missed genuine intrusion, not a cosmetic bug. `tests/test_analyzer.py`'s
adversarial "stays suspicious when a stranger is also present" tests exist
specifically to catch a regression here.

Since 6.0.7 the identity condition also counts people, not just faces:
`_unaccounted_people` withholds the bypass when object detection
confidently saw more people together in **one frame** than there are
approved names recognized — a stranger with their back to the camera has
no face to be "unrecognized", so faces alone called that clip "only Brian
here". `_people_in_one_frame` is tuned against the opposite mistake, a lone
resident miscounted as two, which costs them a bypass they should have
had: it counts only boxes at 0.5+ confidence, and not one mostly inside a
more confident person box (the same body boxed twice — the commonest
miscount), and takes the per-frame peak rather than track ids (tracking on
frames seconds apart splits one person into several). Measured with the
real detector on 100 real one-person scenes: counting every kept box
miscounted 10 by day and 7 at night; the tuned rule 2 by day (one a man on
a bus advert) and 0 at night, while still catching the second person in
66/50 of 100 real two-person scenes. Re-measure before changing either
constant. It sits inside `_face_match_is_unambiguous`, so the Library badge
and the known-person risk discount follow it too, and
`_personalization_names` applies the same count (the unseen face may be
whoever the summary describes). With detection off it counts 0 and the old
faces-only rule stands.

Since 6.0.0 there is a **second** condition: an event in
`security.BYPASS_BLOCKING_EVENTS` withholds the bypass even on a clean
identity match — a recognized person denting the car is still a dented car.
That set is deliberately a single entry (`IMPACT_CANDIDATE`) and **must not
be widened casually**: `CONTACT_CANDIDATE` in particular is what a resident
opening their own car door produces several times a day, so adding it would
make routine household activity permanently suspicious — exactly the
false-positive problem the bypass exists to solve. See the set's own comment
for why each near-miss was excluded.

Separately, `ai_risk_alert_threshold` can flag a clip the model called
unremarkable. It is checked *before* the bypass so the two can never
contradict each other (a clip is never reported as both bypassed and
force-flagged), and it is strictly one-directional: it can raise a verdict,
never lower one.

A recognized person's **name never appears in any prompt sent to any AI
provider**, local or cloud (`vision/faces.py`'s `_build_recognition_hint` is
strictly name-free — only a count/fact). The name is only ever used
afterward, entirely locally, to personalize the human-facing summary text
(`_personalize_summary`) — this is what the Biometrics tab's privacy
banner promises, and the promise and the implementation must stay in sync.
`face_enrollments.approved` (per-enrollment, defaults `TRUE`) gates whether
a match counts toward the bypass at all; enrolling ≠ approving forever.
The Biometrics tab stores a small face crop per enrollment (`thumbnail`) —
also local-only, and the privacy banner and DOCS.md say so.

A frame face detection **could not examine** (models unavailable, an
undecodable frame, a model error) counts as `unrecognized_present` in
`FaceRecognizer.recognize` — it might have held anyone, so it can never help
vouch that everyone in a clip is known. `FaceEmbedder.detect()`/`embed()`
return `None` for that case, never `[]`, precisely so callers can tell it
apart from "no face here"; keep that distinction.

Enrollment and recognition must work at the **same frame width**
(`ai_face_recognition_resolution` → `FACE_RESOLUTION_WIDTHS`, 640px by
default = `ffmpeg_output.ANALYSIS_FRAME_WIDTH`): a face enrolled at one size
matches poorly at another (measured: 2 of 30 recognized when the old picker
enrolled at 480px against 640px recognition, 20 of 30 at matching widths).
The clip scan reads the configured width from `MediaServer`, and each
enrollment records the width it came from (`frame_width`) so the tab can
flag photos a resolution change has left behind.

Personalization and bypass-eligibility are **deliberately different
widths**, both computed in `_analyze_clip_locked`: `_face_bypass_applies`
(approved-only, all-or-nothing) still gates the safety-critical suspicious-
flag clear, unchanged. `_personalization_names` is wider — approved *and*
recognized-but-not-approved names both count, as long as no genuinely
unrecognized face also appears — because rewriting "A person" to someone's
actual name is a cosmetic text rewrite, not a safety decision, so a
household member whose per-enrollment "Approved for bypass" is off (e.g. a
nanny) still gets named in their own routine clips. Do not collapse these
two into one condition again — that's the bug this distinction fixes.

## Development workflow

All commands assume you're in `blink_clip_downloader/` unless stated
otherwise. Install once per environment:

```bash
pip install -e ".[test]"
```

### Tests

```bash
python -m pytest -q                                              # quick run
python -m pytest --cov=blink_downloader --cov-report=term-missing # with coverage (what CI runs)
```

- `asyncio_mode = auto` (set in `pyproject.toml`/`pytest.ini`) — async test
  functions don't need `@pytest.mark.asyncio`.
- Coverage `fail_under = 80` (`pyproject.toml`), enforced in CI. As of v3.0.4
  the whole package is included (no per-file omits beyond `tests/*` and
  `__main__.py`) — don't add new omits to dodge coverage on a file, add tests
  instead.

### Lint / format

```bash
ruff format --check .
ruff check .
```

CI installs `ruff==0.16.6` (`.github/workflows/ci.yaml`, pinned since 5.2.0 —
it used to install unpinned and would periodically drift from whatever
version was installed locally, causing CI-only lint failures on rules like
import sorting that changed behavior between ruff releases with no config
change on our side). Match that version locally
(`pip install "ruff==0.16.6"`) rather than whatever `pip install ruff` pulls
latest — if you do need to bump it (e.g. to pick up a new rule on purpose),
bump both this pin and the CI workflow's in the same change, not just one.

### Type checking

Pyright config (`pyrightconfig.json`) lives at the **repo root**, not inside
`blink_clip_downloader/`, so it must be invoked from there:

```bash
cd /home/brian/ha-blink-clip-downloader
pyright --project pyrightconfig.json
```

### YAML lint

Also run from the repo root (uses the root `.yamllint`, line-length max 120):

```bash
cd /home/brian/ha-blink-clip-downloader
yamllint .
```

### Security scan (bandit)

Config lives in `[tool.bandit]` in `blink_clip_downloader/pyproject.toml`
(tests excluded; B101/B608 skipped with rationale documented there). Run from
the repo root, mirroring CI:

```bash
cd /home/brian/ha-blink-clip-downloader
bandit -c blink_clip_downloader/pyproject.toml -r blink_clip_downloader
```

Needs `pip install "bandit[toml]"`. For a genuinely-false-positive finding,
prefer a per-line `# nosec B<id>` with a brief comment; a config-level skip
is only for rules that can't be suppressed per-line (B608 on multiline SQL
f-strings) or are wrong for this project wholesale (B101 asserts).

### Before declaring any task done

Run, in this order, and fix everything before reporting completion:

1. `ruff format --check .` and `ruff check .` (from `blink_clip_downloader/`)
2. `yamllint .` (from repo root) — only relevant if YAML files changed
3. `pyright --project pyrightconfig.json` (from repo root)
4. `bandit -c blink_clip_downloader/pyproject.toml -r blink_clip_downloader`
   (from repo root)
5. `python -m pytest --cov=blink_downloader --cov-report=term-missing -q`
   (from `blink_clip_downloader/`)
6. `npm run lint` and `npm run format:check` (from `frontend/`) — ESLint and
   Prettier
7. `npm run type-check` (from `frontend/`)
8. `npm run test:coverage` (from `frontend/`) — Vitest; `npm test` is only a
   substitute for a quicker pass without coverage, not a replacement for this
   step

Always run all eight, not just the ones for whichever side you touched —
cheap enough to run every time, and it catches cross-side breakage (e.g. a
backend API shape change breaking a frontend type) that a "only run if X
changed" rule would miss.

This mirrors the `lint` and `test` jobs in `.github/workflows/ci.yaml` — if
these are clean locally, CI's lint/test jobs will pass. (CI also has `build`
and `smoke-test` jobs that build/run the actual Docker image — including the
frontend build — and a Playwright e2e smoke check in `e2e/`; those aren't
practical to run per-change but are worth being aware of if a change touches
`Dockerfile`, `rootfs/run.sh`, `frontend/vite.config.ts`, or add-on startup
behavior. The `build` job also Trivy-scans the built image on both arches,
gated to HIGH/CRITICAL vulnerabilities **with a fix available**. The
Dockerfile's OS-package layer (`apt-get update && apt-get upgrade -y &&
...`) re-runs against current Debian security patches at most once per
UTC day via its `APT_CACHE_BUST` build-arg (fed by a small step in both
`ci.yaml` and `build.yaml`) — without it, CI's Buildx cache (`type=gha`,
scoped only by architecture, shared across every branch/PR/release) would
otherwise reuse that layer's first-ever-build package versions forever,
since the RUN instruction's own text never changes. So a genuine Trivy
failure on an already-fixed CVE should now resolve itself on the next
build after a UTC day boundary with no action needed; only add a
`.trivyignore` entry as a last resort (no fix available yet upstream, or
a finding that's a genuine false positive) with a dated justification
comment.
CI also runs a `sonarqube` job (`SonarSource/sonarqube-scan-action`,
config at the repo root's `sonar-project.properties`) against SonarCloud —
informational only, doesn't gate merges, and isn't practical to run
locally since it needs a `SONAR_TOKEN` and network access to sonarcloud.io;
skipped automatically for PRs from forks, which don't have repo secrets.

CI's `frontend-e2e` job runs `npm run test:e2e:coverage` (Playwright, see
**Web UI**'s E2E testing/E2E coverage bullets) — unlike `build`/
`smoke-test`, this one *is* practical to run locally (no Docker: just a
reachable Postgres, the same prerequisite `pytest` already needs) — worth
doing whenever a change touches Library, Vehicles, Status, AI, AI Usage,
Automations, Sync Module, or Models (their page components, their API
routes, or `media_server/`'s handlers for them), since that's this
suite's coverage so far. Plain `npm run test:e2e` (no coverage
instrumentation, and it reuses an existing `npm run build` instead of
always rebuilding) is faster for a quick local check; CI always runs the
coverage variant.

A separate workflow, `.github/workflows/ha-integration.yaml`, installs the
add-on into a *real* Home Assistant Supervisor + Core (the official
`ghcr.io/home-assistant/devcontainer` image, not just a bare `docker run`
of the built image) and drives the real ingress-proxied UI with
Playwright — the only CI coverage of the actual app↔Home Assistant
integration boundary (Supervisor discovery/build/install/start, the
ingress panel, real ingress routing, a real round trip through Home
Assistant's own API via the Automations tab's "Send test HA notification"
button, and Supervisor-captured logs rendering on HA's own Settings > Apps
> Log page — see `e2e/ha_integration_smoke.mjs`'s `checkHaNotification`/
`checkAddonSupervisorTabs`), which `build`/`smoke-test` above don't touch
at all. Three checks are worth knowing about specifically, because each
guards something no other suite can see:
`checkMediaThroughIngress` (in the post-seed pass) asserts a **206 with a
correct `Content-Range`** for a `Range` request — Video.js needs that to
seek, and ingress silently dropping it would break scrubbing for everyone
while the clip still appeared to play; it needs the real files
`ha_integration_setup.sh seed-media` generates with the image's own ffmpeg,
since the seeded rows alone leave every media endpoint at 404.
`checkAppHeadersThroughIngress` asserts the CSP survives the proxy with
`worker-src 'self' blob:` intact — its absence once broke Live View with a
black frame and no error outside the console — and that `__HAROOT__` was
substituted. And `starAClipThroughIngress` + `assert-clip-starred` are one
pair: the first stars a clip in the real UI, the second reads that row back
*after* the container is recreated, which is the only assertion anywhere
that a change **a user made** survives an update (the other two durability
checks re-read a settings file and rows the job inserted itself).
Deliberately separate from `ci.yaml` and
informational-only (not required/blocking), since it depends on
Supervisor/devcontainer infrastructure this repo doesn't control — see
that workflow file's own header comment for the specific known upstream
risks it's built to tolerate. AppArmor confinement itself is deliberately
**not** exercised here — the job installs a separate copy of the add-on
(under `github.workspace`, deliberately **not** `runner.temp`) with
`apparmor: false` forced into that copy's `config.yaml`
(`prepare-addon-copy`), never the real repo files. This was a deliberate
retreat, not a shortcut taken casually: a real, ecosystem-standard fix
(`network unix stream,` etc., matching Supervisor's own reference profile)
for a genuine confinement failure (s6-ipcserver-socketbinder denied)
didn't resolve it on a real run, pointing at this nested Docker-in-Docker
CI environment not faithfully reproducing real HAOS AppArmor enforcement
rather than an actual bug in the add-on's profile — see the workflow
file's header comment and `blink_clip_downloader/apparmor.txt`'s git
history around 2026-09-08 before attempting this again; don't re-edit that
file for a CI-only problem without a real HAOS host (or at least a kernel
with `CONFIG_SECURITY_APPARMOR` actually enabled — this dev sandbox's
doesn't, confirmed via `/sys/module/apparmor/parameters/enabled`) to
validate against, and prefer extending the CI-only bypass over touching
the production profile again on circumstantial evidence alone.
`scripts/ci/ha_integration_setup.sh`'s subcommands (`prepare-addon-copy`/
`wait-docker`/`serve-local-image`/`wait-core`/`discover`/`install`/`start`/
`restart`/`assert-clean-log`/`assert-persisted`/`assert-version`/
`enable-ingress-panel`/`diagnostics`) can each be run independently
against an already-running container for local debugging, and
`scripts/run-act-ha-integration.sh` runs the whole workflow locally via
`act`, mirroring `scripts/run-act.sh`'s conventions for `ci.yaml` but with
one addition specific to this job: `--bind`, required because
`prepare-addon-copy` creates a new directory mid-job that a later sibling
`docker run` needs to see on the real host — `act`'s default checkout
(a one-time `docker cp` snapshot into its own job container) doesn't
give it that visibility, `--bind` (a live two-way mount of the working
directory) does. See the "Prepare CI-only add-on copy" step's own comment
in `ha-integration.yaml` for the full mechanism if this ever needs
revisiting — confirmed the hard way over several act runs each leaving a
phantom empty host directory behind, not guessed. Slow
(~15–20 min, dominated by Supervisor building the add-on's own image) and
not something to run per-change. If this workflow ever needs modifying,
re-derive nothing by guesswork — bring the environment up by hand first
(`docker run` the devcontainer image, `bash devcontainer_bootstrap`,
background `supervisor_run`, then poke at it with `ha --raw-json`/curl/a
throwaway Playwright script) and confirm each command actually works
before touching the workflow YAML; several of its details (Supervisor's
real entry point is container port 80, not Core's own 8123; a freshly
installed add-on's ingress panel is not automatically added to the HA
sidebar; the sidebar panel link needs an unscoped `page.getByText()`
click, not a forced click on the `<a>`) came from exactly that kind of
hands-on discovery and aren't documented anywhere upstream.

## Versioning

The add-on version appears in **seven places** (the last of which is several
npm files) that must all be updated together for any user-facing change
(bug fix, feature, dependency bump):

1. `blink_clip_downloader/config.yaml` — `version: "vX.Y.Z"`
2. `blink_clip_downloader/pyproject.toml` — `version = "X.Y.Z"`
3. `blink_clip_downloader/blink_downloader/__init__.py` — `__version__`
4. `blink_clip_downloader/blink_downloader.egg-info/PKG-INFO` — regenerate by
   re-running `pip install -e ".[test]"` after bumping the other files, don't
   hand-edit
5. `blink_clip_downloader/CHANGELOG.md` — add a new `## X.Y.Z` section
   describing the change (follow the existing "Bug fixes" / "Dependencies" /
   feature-heading style)
6. `blink_clip_downloader/frontend/src/components/layout/AppSidebar.vue` —
   the About dialog's `header="About Blink Clips X.Y.Z"`. **Hardcoded**, not
   read from any of the above, and it is the version a user actually sees in
   the UI — 6.0.1 found it still advertising 6.0.0. `e2e/app-sidebar.spec.ts`
   asserts the dialog by that exact name, so it has to move with it.
7. `blink_clip_downloader/frontend/package.json` and the repo root's
   `e2e/package.json` — `"version"`, plus the self-version each one's
   `package-lock.json` carries twice (its own lines 3 and 9, never a
   dependency's). Kept in step with the rest purely so they never
   disagree; nothing reads any of them at runtime. The root `e2e` one
   only joined the list in 6.0.5, which is why older tags still show it
   at `1.0.0`.

Missing any of these breaks Docker image tagging or version sync between the
add-on manifest and the Python package.

**Exception:** changes that only touch CI/workflow files or pinned action
versions (`.github/workflows/*.yaml`) do not need a version bump or
CHANGELOG entry — nothing user-facing changed.

### Release ordering (since `config.yaml` gained an `image:` key)

`config.yaml` names a published image (`image:
"ghcr.io/brianbaggs35/blink_clip_downloader"`), so **Supervisor pulls
`<image>:<version>` rather than building the add-on locally** — there is no
build fallback if that tag is missing. `build.yaml` publishes it on
`release: published`, and takes ~15-30 minutes for both architectures.

So the version in `config.yaml` on the branch users' add-on store reads
must never be newer than the image tag that actually exists on ghcr: a user
who refreshes the repository in that window gets a pull failure with no way
forward. Publish the GitHub release (and let `build.yaml` finish) before
the bumped `config.yaml` reaches the branch the add-on repository points
at. Confirm with:

```bash
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:brianbaggs35/blink_clip_downloader:pull" | jq -r .token)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  https://ghcr.io/v2/brianbaggs35/blink_clip_downloader/manifests/vX.Y.Z
```

200 means the tag is there (and it should be an OCI index covering
linux/amd64 *and* linux/arm64 — `arch:` promises both, and `image:` carries
no `{arch}` placeholder, so one multi-arch manifest is what makes both
work).

## Conventions worth knowing

- Two toolchains, not one: Python (`blink_clip_downloader/`, ruff/pyright/
  pytest) and the Vue frontend (`frontend/`, its own eslint/prettier/vue-tsc/
  vitest) — see **Web UI** above. Neither ships the other's tooling into the
  final Docker image (Node/`node_modules` never land in it; only the built
  static output does).
- `ai_car_cameras` empty = "applies to all cameras" is intentional, documented
  behavior, not a bug — see **AI provider architecture** above.
- Two coordinate spaces exist and must not be mixed: **normalized** (0-1, how
  user-drawn zones and learned vehicle signatures are stored, so they survive a
  resolution change) and **pixel** (what the detector returns).
  `security/geometry.py`'s `Zone` owns the conversion; every `box_*` helper is
  scale-independent but requires both arguments in the *same* space.
- `detected_objects` rows are **per box per sampled frame**, so counting
  them is never the answer to "how many were there" — one parked car across
  twelve frames is twelve rows. `get_detected_objects_summary`'s `count` is
  distinct `track_id`s (the same identity `security/tracks.py` groups its
  `ObjectTrack`s by, so the chips and the security events can never
  disagree about how many people were in a clip), floored by the per-frame
  peak so rows stored with no `track_id` still count. `detections` keeps
  the raw box total.
- Distance has two meanings in this codebase and they answer different
  questions: `box_gap` is outline-to-outline (negative when boxes overlap) and
  is what contact rules need; `ground_gap` is feet-to-ground-line with vertical
  separation weighted for perspective, and is what proximity/approach rules
  need. Using the former for proximity is what made a pedestrian walking in
  front of a parked car read as "inches from the vehicle".
- Docstrings in this codebase are typically one-line-to-short-paragraph
  descriptions of behavior (see existing methods in `analyzer/base.py`,
  `config.py`) — match that style rather than terse or absent docstrings on
  public classes/methods, but don't add commentary the code already makes
  obvious.
