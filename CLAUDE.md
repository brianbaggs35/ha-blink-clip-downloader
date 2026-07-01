# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## Project overview

This is a **Home Assistant add-on repository** (the kind you add via *Settings →
Add-ons → Repositories*). It currently contains a single add-on:

- `blink_clip_downloader/` — "Blink Clip Downloader": polls the Blink API
  (via [blinkpy](https://github.com/fronzbot/blinkpy)) and downloads camera
  clips to local storage, with retention/quota management, HA notifications,
  a SQLite clip library, an embedded web UI (Video.js player), and optional
  AI-based suspicious-activity analysis of clips.

Almost all work happens inside `blink_clip_downloader/`. **Run all commands
below from that directory unless noted otherwise** (pyright is the exception —
it must run from the repo root, see below).

Target platform is Home Assistant OS (`arch: aarch64, amd64`); it may work on
other HA install types but that's not the primary support target
(see `CONTRIBUTING.md`).

## Directory layout (`blink_clip_downloader/`)

- `blink_downloader/` — the actual Python package.
  - `app.py` — `BlinkClipDownloaderApp`, the main run loop/entrypoint.
  - `config.py` — `AppConfig` dataclass; loads `/data/options.json` (HA
    add-on options) into typed config. Also defines per-camera config schema
    (`ai_camera_prompts`, `ai_camera_descriptions`, `ai_car_cameras`).
  - `downloader.py` — Blink API polling + clip download/thumbnail generation.
  - `database.py` — SQLite clip library (`ClipDatabase`).
  - `analyzer.py` — AI vision analysis. See **AI provider architecture** below.
  - `analysis_queue.py` — async queue that feeds clips to the analyzer.
  - `media_server.py` — aiohttp HTTP server: REST API + a **single embedded
    HTML/CSS/JS string** (`_HTML`) that is the whole web UI/SPA. There is no
    separate frontend build step — the UI is a Python string literal in this
    file. See **Web UI** below.
  - `event_watcher.py`, `notifier.py`, `notification_channels.py`,
    `digest.py`, `archiver.py`, `storage.py`, `library_scanner.py`,
    `tracker.py`, `manifest.py`, `blinkpy_compat.py` — supporting modules
    (event-driven fast-poll, HA/mobile/email/Discord notifications, daily
    digest, cold storage archiving, storage quota enforcement, filesystem
    library scan/reconcile, download-session tracking, clip manifest export,
    blinkpy version-compat shims).
- `tests/` — pytest test suite, one `test_<module>.py` per module above, plus
  `conftest.py` with shared fixtures (`base_config`, `sample_clip`,
  `options_file`, `tmp_download_dir`).
- `config.yaml` — HA add-on manifest (options schema, version, ports, maps).
- `build.yaml` — base images per architecture.
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
| `moondream_local` | `MoondreamLocalAnalyzer` | Local moondream package, **x86_64 only**  |
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

Per-camera `description`, `custom_prompt`, and `is_car_camera` live in a
single JSON file that the web UI's **AI tab** edits directly — it is the
single source of truth (not `config.yaml` options). The `is_car_camera`
checkbox per camera in that tab is what populates `ai_car_cameras`.

## Web UI (`media_server.py`)

The entire SPA — HTML, CSS (`<style>` block), and JS — lives in the `_HTML`
triple-quoted string near the top of `media_server.py`. There is no separate
`.css`/`.js`/template file and no bundler. When making UI changes:

- Edit the string in place; there's nothing to compile/build.
- The stylesheet has one responsive breakpoint: `@media(max-width:600px)`
  near the end of the `<style>` block. Mobile-specific fixes belong there.
- Grid layouts use `minmax(min(Npx,100%),1fr)` (not bare `minmax(Npx,1fr)`)
  so columns shrink instead of overflowing on viewports narrower than the
  minimum column width.
- `body{height:100vh;height:100dvh;...}` — the `100dvh` re-measures correctly
  when mobile browser chrome (address bar) shows/hides; `100vh` is the
  fallback for browsers without `dvh` support. Keep both declarations, in
  that order.
- Anything `position:fixed` (toasts, modals) on narrow screens needs an
  explicit width/left constraint or a `calc(100vw - ...)` cap — `right`-only
  positioning with a bare `max-width` can spill off the left edge on ~320px
  Android viewports.
- `media_server.py` is **not** excluded from the coverage requirement (see
  below) — new server-side logic in this file needs test coverage via
  `tests/test_media_server.py`, typically using the aiohttp `TestClient`
  fixtures already in that file.

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

### Before declaring any task done

Run, in this order, and fix everything before reporting completion:

1. `ruff format --check .` and `ruff check .` (from `blink_clip_downloader/`)
2. `yamllint .` (from repo root) — only relevant if YAML files changed
3. `pyright --project pyrightconfig.json` (from repo root)
4. `python -m pytest --cov=blink_downloader --cov-report=term-missing -q`
   (from `blink_clip_downloader/`)

This mirrors the `lint` and `test` jobs in `.github/workflows/ci.yaml` — if
these are clean locally, CI's lint/test jobs will pass. (CI also has `build`
and `smoke-test` jobs that build/run the actual Docker image; those aren't
practical to run per-change but are worth being aware of if a change touches
`Dockerfile`, `rootfs/run.sh`, or add-on startup behavior.)

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

- No separate frontend toolchain (no npm/webpack/etc.) — the "frontend" is
  the `_HTML` string in `media_server.py`, described above.
- `ai_car_cameras` empty = "applies to all cameras" is intentional, documented
  behavior, not a bug — see **AI provider architecture** above.
- Docstrings in this codebase are typically one-line-to-short-paragraph
  descriptions of behavior (see existing methods in `analyzer.py`,
  `config.py`) — match that style rather than terse or absent docstrings on
  public classes/methods, but don't add commentary the code already makes
  obvious.
