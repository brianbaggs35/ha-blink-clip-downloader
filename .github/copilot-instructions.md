# GitHub Copilot Instructions

## Project

Blink Clip Downloader is a Home Assistant OS add-on for downloading and managing Blink camera clips. It uses `blinkpy` to communicate with the Blink service and provides its own storage/retention, PostgreSQL clip library, Vue UI, Live View, notifications, and optional AI/computer-vision features.

Primary code:

```text
blink_clip_downloader/
```

Supported architectures: **amd64 and aarch64**.

Before architectural changes, inspect existing code/tests.

## Critical Rules

* Make small, focused changes following existing architecture and patterns.
* Do not rewrite unrelated code or introduce unnecessary frameworks/abstractions.
* Do not remove, weaken, or bypass tests to make changes pass.
* Do not upgrade unrelated dependencies.
* Preserve Home Assistant compatibility and amd64/aarch64 support.
* Python 3.12 is the project baseline; do not raise the minimum version for unrelated work.
* Do not use semicolons at the end of lines.
* Add focused tests for behavioral changes.
* When uncertain, inspect the repository rather than guessing.
* Do not leave old/unused code commented out, remove it if it's not needed anymore.

## Backend

Backend: **Python + asyncio + aiohttp**.

Key modules:

* `app.py` — application lifecycle
* `config.py` — configuration
* `downloader.py` — Blink API polling/downloads/camera operations
* `database.py` — PostgreSQL database
* `analyzer.py` — AI provider abstraction
* `analysis_queue.py` — async AI queue
* `vision.py` — optional computer vision
* `media_server.py` — HTTP API/frontend serving
* `live_view.py` — Live View
* `storage.py` / `archiver.py` — storage/archive management

Avoid blocking operations in async code. Failures from external services should not unnecessarily take down unrelated functionality.

## Database

The application uses **PostgreSQL 17**. **Do not replace it with SQLite.**

Existing installations must be supported. Changing `CREATE TABLE IF NOT EXISTS` does not modify an existing table.

Schema changes require an appropriate `_MIGRATIONS` entry, normally using:

```sql
ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...
```

Keep SQL parameterized and ensure migrations work for both fresh and existing databases.

## Home Assistant / Storage

The add-on relies on Home Assistant Supervisor and persistent paths:

```text
/data
/share
```

Home Assistant provides `/data/options.json`.

Do not assume ordinary Docker execution matches the production Home Assistant environment. Do not hard-code developer-machine paths.

The production image uses a Debian/Trixie-based Home Assistant base image. Do not casually change the base image or dependency installation strategy.

## Computer Vision

Enhanced computer vision is optional and must not break core functionality when unavailable.

It includes object detection/tracking, depth estimation, contact segmentation, OpenCV preprocessing, and local face recognition.

Do not make optional CV dependencies mandatory for unrelated features. Be especially careful with PyTorch/torchvision compatibility and aarch64 support.

## AI

`analyzer.py` defines `BaseAnalyzer`; `create_analyzer()` selects providers.

Current providers:

* Ollama Local
* Ollama Cloud
* Moondream Cloud
* Moondream Local
* Anthropic
* OpenAI

Follow the existing analyzer/factory architecture and isolate provider-specific behavior.

## Camera Configuration

Per-camera settings are stored in:

```text
/data/camera_configs.json
```

Important fields include `description`, `custom_prompt`, `is_car_camera`, and `car_zone`.

Camera configuration saves use **full-array replacement**. When changing one setting, preserve fields owned by other UI sections so one save cannot overwrite another section's settings.

Vehicle settings are separate:

```text
/data/vehicle_settings.json
```

## Live View

`LiveViewManager` uses the `blinkpy` live-view session, a local raw TCP relay, and ffmpeg to produce HLS through the existing HTTP server.

**Live View is not RTSP.**

Only **one Live View session** may be active. Starting another camera's session stops the current session.

Preserve existing timeout, cancellation, cleanup, and crash-handling behavior. Do not replace it with another HTTP server.

## Security Feed

Security Feed is separate from Live View.

It uses cached camera images/thumbnails and deliberately does not call `snap_picture()` on every refresh.

Do not couple Security Feed to `LiveViewManager`.

# Frontend

Frontend stack:

* Vue 3
* TypeScript
* Vite
* PrimeVue 4
* Pinia
* Video.js

**Do not use React or introduce React patterns.**

Source:

```text
blink_clip_downloader/frontend/
```

Production Vite output:

```text
blink_clip_downloader/blink_downloader/static/
```

The backend serves the built assets.

Video.js is bundled. Do not load it from a CDN; prefer project dependencies for consistent versions, offline availability, and security.

## Frontend API

Use the existing API layer:

```text
frontend/src/api/client.ts
```

with:

```text
apiGet
apiPost
apiPut
apiPatch
apiDelete
```

Put area-specific API code in the existing API modules.

Do not call `fetch()` directly from Vue components unless necessary. The API layer handles Home Assistant ingress paths.

## Frontend State / UI

Use Pinia only for genuinely shared state. Keep page-specific data and form state local unless it must be shared.

Follow existing Vue component/page organization.

For new UI:

* Prefer existing PrimeVue 4 components and project styling.
* Reuse existing components/utilities/patterns.
* Do not introduce another UI framework.
* Keep layouts responsive and avoid fixed-width overflow.
* Put new code in the appropriate existing directories.

## Testing

Backend: **pytest**

Frontend unit tests: **Vitest + Vue Test Utils**

Frontend E2E: **Playwright**

Root `e2e/` is separate from `frontend/e2e/` and smoke-tests the packaged container.

Coverage minimum: **80%**.

Add/update regression tests for behavioral bug fixes where practical. Never lower coverage or remove tests to make CI pass.

Follow existing PrimeVue test setup and mounting patterns.

## Local CI with act

Use:

```text
scripts/run-act.sh
```

for locally compatible GitHub Actions jobs.

The script limits local CI to **Python 3.12 and AMD64**, since native GitHub ARM runners are unavailable locally, and excludes integrations requiring GitHub services.

It selects a free host port, rewrites the temporary PostgreSQL service mapping, passes matching DSNs to test steps, and cleans up the temporary workflow. ONLY use act if I ask for it!!!

Examples:

```bash
scripts/run-act.sh
scripts/run-act.sh lint test
ACT_CONCURRENT_JOBS=2 scripts/run-act.sh lint test frontend-e2e
```

Keep the committed GitHub PostgreSQL mapping at `localhost:5432`. Local `act` port selection belongs in `scripts/run-act.sh`, not the committed workflow.
When the host has `$HOME/.cache/ms-playwright`, the script mounts that cache
into the Act runner so Playwright browsers are reused instead of downloaded.

Frontend E2E uses a shared backend/database and one worker. Mutating tests must isolate their data and must not depend on execution order.

## Docker / Architectures

Docker must support:

```text
amd64
aarch64
```

The optional CV stack includes heavyweight/native dependencies such as PyTorch, torchvision, Ultralytics, OpenCV, Transformers, and facenet-pytorch.

Do not casually change their versions, installation order, or dependency flags. Check architecture compatibility before introducing native/binary dependencies.

## Code Quality

Existing tooling:

* Ruff
* Pyright
* pytest
* Vitest
* Playwright
* ESLint
* Prettier
* Bandit
* yamllint
* Home Assistant add-on linting

Use existing project configuration and scripts rather than inventing alternative tooling.

## Completion

Before considering a change complete:

1. Inspect affected code and existing tests.
2. Make the smallest appropriate change.
3. Add/update tests for behavioral changes.
4. Run relevant checks.
5. Check for regressions and architecture compatibility.
6. Verify Home Assistant compatibility where applicable.

For broad changes, run applicable checks covering:

**Pyright, Ruff, pytest, Vitest, Playwright, ESLint, Prettier, Bandit, yamllint, and Home Assistant add-on linting.**

When behavior is unclear, inspect the implementation and tests before making assumptions.
