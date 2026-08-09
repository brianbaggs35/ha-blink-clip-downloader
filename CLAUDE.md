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
  - `database.py` — clip library (`ClipDatabase`) against a **PostgreSQL 17
    server bundled and supervised inside this same container** (not SQLite —
    that was replaced in 5.0.0; see the Dockerfile and
    `rootfs/etc/services.d/postgresql`). New columns on an existing table
    need an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `_MIGRATIONS`, not
    just adding the column to `_SCHEMA`'s `CREATE TABLE IF NOT EXISTS` —
    that statement is a no-op for a table that already exists, so upgrading
    installs would never get the new column otherwise.
  - `analyzer.py` — AI vision analysis. See **AI provider architecture** below.
  - `analysis_queue.py` — async queue that feeds clips to the analyzer.
  - `vision.py` — optional, off-by-default computer-vision enhancement
    pipeline (object detection/tracking, depth estimation, contact
    segmentation, OpenCV frame preprocessing, local-only face recognition).
    Layered on top of `analyzer.py`'s prompt pipeline via
    `BaseAnalyzer.attach_vision_pipeline()` — each stage produces a hint
    string appended to the same prompt, never replacing the configured AI
    provider's judgment. Every stage lazily imports its own heavy
    dependency (torch/ultralytics/opencv/transformers/facenet-pytorch) and
    reports itself unavailable rather than raising if missing — none of
    them are required for the add-on's core features to work.
  - `media_server.py` — aiohttp HTTP server: REST API + serves the built Vue
    app as static files (`_STATIC_DIR`/`_handle_index`). See **Web UI** below
    — the frontend itself lives in `frontend/`, a sibling of `blink_downloader/`.
  - `live_view.py` — `LiveViewManager`, backing the Live View tab. Bridges
    blinkpy's live-view session (a proprietary binary protocol relayed onto
    a local raw-TCP socket — not RTSP) through an ffmpeg subprocess into a
    short rolling HLS playlist, served through `media_server.py`'s existing
    routes/port rather than a new one (HA ingress only proxies HTTP/
    WebSocket, so the raw TCP socket itself is unreachable from a browser
    regardless). Exactly one session is active at a time; starting a
    different camera stops whichever was active first. A background sweep
    loop enforces an idle timeout and a hard cap per session, on top of the
    frontend stopping its session on unmount (navigating away) — see the
    module's own docstring and `tests/test_live_view.py` for the session
    lifecycle/crash-handling details.
  - `event_watcher.py`, `notifier.py`, `notification_channels.py`,
    `digest.py`, `archiver.py`, `storage.py`, `library_scanner.py`,
    `tracker.py`, `manifest.py` — supporting modules (event-driven
    fast-poll, HA/mobile/email/Discord notifications, daily digest, cold
    storage archiving, storage quota enforcement, filesystem library
    scan/reconcile, download-session tracking, clip manifest export).
- `frontend/` — the Vue 3 + PrimeVue + Pinia web UI, a separate npm project
  (its own `package.json`/`node_modules`/toolchain). See **Web UI** below.
- `tests/` — pytest test suite, one `test_<module>.py` per module above, plus
  `conftest.py` with shared fixtures (`base_config`, `sample_clip`,
  `options_file`, `tmp_download_dir`).
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

`analyzer.py` defines `BaseAnalyzer` (ABC) and six concrete analyzers,
selected via the `create_analyzer()` factory keyed on `ai_provider`:

| `ai_provider`     | Class                    | Notes                                   |
|-------------------|--------------------------|------------------------------------------|
| `ollama`          | `ClipAnalyzer`           | Local/LAN Ollama server                   |
| `ollama_cloud`    | `OllamaCloudAnalyzer`    | Hosted Ollama Cloud API                   |
| `moondream_cloud` | `MoondreamCloudAnalyzer` | Moondream Cloud API, no model selection   |
| `moondream_local` | `MoondreamLocalAnalyzer` | Local moondream package, requires an **NVIDIA/Apple Silicon GPU** (any arch since the 4.1.0 Debian base image switch) |
| `anthropic`       | `AnthropicAnalyzer`      | Claude vision models                      |
| `openai`          | `OpenAIAnalyzer`         | GPT vision models                         |

`MoondreamFineTuneManager` is a separate helper class (not an analyzer) that
wraps the Moondream Cloud fine-tuning API.

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

The web UI is a **Vue 3 + PrimeVue 4 + Pinia** single-page app, a separate
npm project at `frontend/` (its own `package.json`, `node_modules`,
`vite.config.ts`, `eslint.config.js`). It is **not** an embedded string in
`media_server.py` — an earlier version of this add-on worked that way, but
that was fully replaced; the ~2,900-line dead `_HTML` remnant of it was
removed in 5.0.0.

- **Build & serving**: `npm run build` (Vite) writes straight into
  `blink_downloader/static/` — the Dockerfile's `frontend-builder` stage
  runs this before the image is packaged, and `media_server.py`'s
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
  children). Static/reference-only tabs (`ModelsPage.vue`,
  `AutomationsPage.vue`'s doc content) don't fetch anything and are simpler,
  but aren't the pattern to copy for a data-driven tab.
- **Nav wiring**: adding/removing a tab touches four places —
  `components/layout/AppSidebar.vue`'s `TabName` type + `TABS` array,
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
  Live View, Automations, Status, AI, AI Usage, Models, Vehicles,
  Biometrics, Storage.
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
  (`Select`, `Textarea`, `ToggleSwitch`, `FileUpload`, ...) needs
  `global: { plugins: [PrimeVue] }` (`import PrimeVue from 'primevue/config'`)
  or it throws on a missing `$primevue` injection. For a component's own
  `v-model`/`defineModel`, wire a real two-way test harness (pass
  `'onUpdate:x': (v) => wrapper.setProps({ x: v })`) rather than a no-op
  handler — the no-op silently breaks any test that expects selections to
  accumulate across multiple interactions. `PointerEvent`/`MouseEvent`
  position properties (`clientX`/`clientY`, not `offsetX`/`offsetY`) can't be
  set through Vue Test Utils' `trigger(type, options)` for events that
  inherit them from a different prototype (a VTU/jsdom quirk) — dispatch a
  real `new PointerEvent(...)` directly on the element instead when a test
  needs a specific pointer position (see `VehicleZonePicker.spec.ts`).
  Coverage threshold is 80% (`vitest.config.ts`), mirroring the Python side's
  `fail_under = 80` — actual coverage on this codebase runs ~98-99%.
- Responsive/mobile conventions carried over from the pre-Vue UI still
  apply: grid layouts use `minmax(min(Npx,100%),1fr)` (not bare
  `minmax(Npx,1fr)`) so columns shrink instead of overflowing narrow
  viewports, and anything `position:fixed` (toasts, modals) needs an
  explicit width/left constraint or `calc(100vw - ...)` cap.
- `media_server.py` (the Python side of the API) is **not** excluded from
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
```

CI folds these into the existing Python `lint`/`test` jobs
(`.github/workflows/ci.yaml`), gated to run once (the `python-version ==
"3.12"` matrix leg) rather than as a separate job.

## Face-recognition suspicious-flag bypass (safety-critical — read before touching)

An approved, recognized household member can auto-clear a clip's
suspicious flag (`analyzer.py`'s `BaseAnalyzer._face_bypass_applies` /
`_personalize_summary`, wired into `_analyze_clip_locked` right after
`parse_response()`). This is deliberately **all-or-nothing per clip**: it
requires at least one approved match **and zero** unrecognized or
recognized-but-not-approved faces anywhere in the clip's sampled frames
(`vision.py`'s `FaceRecognizer.recognize()` → `FaceRecognitionResult`). A
single stranger standing next to an approved family member must still get
flagged — **do not loosen this condition** without equally strong
justification; a false bypass here is a missed genuine intrusion, not a
cosmetic bug. `tests/test_analyzer.py`'s adversarial "stays suspicious when
a stranger is also present" tests exist specifically to catch a regression
here.

A recognized person's **name never appears in any prompt sent to any AI
provider**, local or cloud (`vision.py`'s `_build_recognition_hint` is
strictly name-free — only a count/fact). The name is only ever used
afterward, entirely locally, to personalize the human-facing summary text
(`_personalize_summary`) — this is what the Biometrics tab's privacy
banner promises, and the promise and the implementation must stay in sync.
`face_enrollments.approved` (per-enrollment, defaults `TRUE`) gates whether
a match counts toward the bypass at all; enrolling ≠ approving forever.

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

CI installs `ruff==0.16.2` (`.github/workflows/ci.yaml`, pinned since 5.2.0 —
it used to install unpinned and would periodically drift from whatever
version was installed locally, causing CI-only lint failures on rules like
import sorting that changed behavior between ruff releases with no config
change on our side). Match that version locally
(`pip install "ruff==0.16.2"`) rather than whatever `pip install ruff` pulls
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
gated to HIGH/CRITICAL vulnerabilities **with a fix available** — if it
fails, a rebuild usually picks up the fixed Debian package; only add a
`.trivyignore` entry as a last resort with a dated justification comment.)

## Versioning

The add-on version appears in **five places** that must all be updated
together for any user-facing change (bug fix, feature, dependency bump):

1. `blink_clip_downloader/config.yaml` — `version: "vX.Y.Z"`
2. `blink_clip_downloader/pyproject.toml` — `version = "X.Y.Z"`
3. `blink_clip_downloader/blink_downloader/__init__.py` — `__version__`
4. `blink_clip_downloader/blink_downloader.egg-info/PKG-INFO` — regenerate by
   re-running `pip install -e ".[test]"` after bumping the other files, don't
   hand-edit
5. `blink_clip_downloader/CHANGELOG.md` — add a new `## X.Y.Z` section
   describing the change (follow the existing "Bug fixes" / "Dependencies" /
   feature-heading style)

Missing any of these breaks Docker image tagging or version sync between the
add-on manifest and the Python package.

**Exception:** changes that only touch CI/workflow files or pinned action
versions (`.github/workflows/*.yaml`) do not need a version bump or
CHANGELOG entry — nothing user-facing changed.

## Conventions worth knowing

- Two toolchains, not one: Python (`blink_clip_downloader/`, ruff/pyright/
  pytest) and the Vue frontend (`frontend/`, its own eslint/prettier/vue-tsc/
  vitest) — see **Web UI** above. Neither ships the other's tooling into the
  final Docker image (Node/`node_modules` never land in it; only the built
  static output does).
- `ai_car_cameras` empty = "applies to all cameras" is intentional, documented
  behavior, not a bug — see **AI provider architecture** above.
- Docstrings in this codebase are typically one-line-to-short-paragraph
  descriptions of behavior (see existing methods in `analyzer.py`,
  `config.py`) — match that style rather than terse or absent docstrings on
  public classes/methods, but don't add commentary the code already makes
  obvious.
