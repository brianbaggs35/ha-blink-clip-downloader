# Changelog

## 2.6.2

### Bug fixes

- **Fixed blinkpy 0.25.5 authentication issues** — Pinned blinkpy to `>=0.23.0,<0.25.0` as v0.25.5 introduced a regression where `start()` fails silently without raising exceptions. Enhanced error detection to catch incomplete auth state and retry properly.
- **Improved 2FA flow after successful start()** — Changed 2FA handler to call `start()` again after code submission instead of just `refresh()`, ensuring full initialization of blink object.
- **Better auth error messages** — Added logging to distinguish between login failures and silent initialization failures to aid in debugging credential issues.

## 2.6.1

### Improvements

- **Updated Docker build workflow** — Migrated from deprecated `home-assistant/builder` action to modern `docker/build-push-action` with proper multi-arch manifest support.
- **Fixed YAML lint issues** — Ensured all workflow files comply with yamllint standards.

## 2.6.0

### Bug fixes

- **Fixed 2FA authentication flow** — Added `refresh()` call after 2FA submission to ensure the blink object is fully initialized with all sync modules and networks.
- **Fixed `'NoneType' object has no attribute 'base_url'` error** — Improved URL resolution with safer null checks to handle edge cases where the blink object might not be fully initialized.
- **Fixed incomplete authentication detection** — Added validation after Blink auth completes to detect incomplete initialization and force fresh login if needed.
- **Fixed cached auth handling** — Clears stale cached credentials when authentication fails or is incomplete, forcing a fresh login on retry.

## 2.5.9

### Bug fixes

- Fixed some more bugs.

## 2.5.6

### Bug fixes

- Fixed automatic library refresh while browsing clips so the page will no longer jump back to the top unexpectedly.

## 2.5.5

### New features

- **Sync Module USB local-storage download** (`download_local_storage: false`)
  — When a USB drive is plugged into a Blink Sync Module the module records
  clips to it as well as to the cloud.  Enabling this option instructs the
  add-on to also fetch those locally-stored clips each poll cycle.

  **How it works:**
  1. After every normal cloud download the add-on iterates `blink.sync` to
     find any Sync Module that reports `local_storage = True`.
  2. For each such module it calls `sync.update_local_storage_manifest()` to
     fetch the current USB clip list via the Blink cloud API (no direct LAN
     access is required).
  3. New clips (not already in the tracker) are downloaded using blinkpy's
     `LocalStorageMediaItem.prepare_download()` + `download_video()`.
  4. Downloaded clips are saved to the same `download_path` directory tree as
     cloud clips (respecting `organize_by_camera` / `organize_by_date`) and
     indexed in the SQLite clip library with `source = "local_storage"`.
  5. Clips skipped by `is_over_quota()` halt the batch for that poll cycle to
     prevent storage overruns.

  The clip IDs are prefixed with `local_` to avoid collisions with cloud
  clip IDs.  The feature is opt-in and disabled by default.

### Improvements

- **`web.FileResponse`-based video streaming** — The previous manual
  `aiofiles`-based chunked stream loop has been replaced with aiohttp's
  built-in `web.FileResponse`.  On Linux (including Raspberry Pi OS) aiohttp
  delegates the transfer to the kernel's `sendfile(2)` system call, which
  copies bytes directly from the filesystem page cache to the socket buffer
  without passing through the Python interpreter.  The result is:
  - **Lower CPU usage** — the Python GIL is not held during byte transfer;
    the asyncio event loop stays responsive for other requests.
  - **Automatic Range support** — `206 Partial Content` byte-range requests
    (used by the browser for seeking) are handled natively by aiohttp/the
    kernel, with no Python loop involved.
  - **Smoother seek performance on Raspberry Pi 5** — the Pi's kernel page
    cache warms quickly via `sendfile`, and seeks that land on cached pages
    resolve with essentially zero latency.
  - Removed the `aiofiles` and `re` dependencies from `media_server.py`.

- **Video.js `enableSmoothSeeking: true`** — When the user clicks or scrubs
  in the seek bar Video.js now plays forward at 2× speed to reach the target
  position rather than issuing an immediate jump.  The result is a fluid
  "glide" instead of a hard cut, eliminating the black-frame stall that
  previously appeared during seeks on the Pi 5.

### Tests

- `test_download_local_storage_no_blink_returns_empty` — returns `[]` when
  `_blink` is `None`.
- `test_download_local_storage_skips_no_usb` — skips sync modules whose
  `local_storage` attribute is falsy.
- `test_download_local_storage_handles_manifest_error` — logs a warning and
  continues when `update_local_storage_manifest()` raises.
- `test_download_local_storage_skips_already_tracked` — does not re-download
  clips that are already in the tracker.
- `test_download_local_storage_downloads_new_clip` — happy-path: clip is
  downloaded, written to disk, added to tracker, and added to the DB.
- `test_download_local_storage_download_failure_skipped` — when
  `download_video()` returns `False` the clip is excluded from results.
- `test_poll_cycle_calls_local_storage_when_enabled` — `_poll_cycle` calls
  `download_local_storage_clips()` when the option is enabled.
- `test_poll_cycle_skips_local_storage_when_disabled` — `_poll_cycle` does
  not call `download_local_storage_clips()` when the option is disabled.
- `test_poll_cycle_local_storage_clips_trigger_notification` — local-storage
  clips returned alongside cloud clips produce a combined HA notification.
- `test_download_local_storage_defaults_to_false` — `AppConfig` defaults to
  `download_local_storage = False`.
- `test_download_local_storage_can_be_enabled` — `_parse_config` honours
  `download_local_storage: True`.

Total: **285 tests** (up from 274 in v2.5.4); coverage 91 %.

## 2.5.4

### Bug fixes

- **Fixed Storage section showing nothing in the sidebar and Status page** —
  `_handle_stats` was reading `request.app.get("disk_stats")` which queries
  aiohttp's internal Application data store — a completely separate dict that
  is never populated with disk information.  The actual disk data lives in
  `self.extra_status` (the `MediaServer` instance dict set by `app.py`).

  **Fixes applied:**
  1. **`media_server.py` `_handle_stats`** — changed the lookup from
     `request.app.get("disk_stats")` → `self.extra_status.get("disk")`.
  2. **`app.py` connection point** — `extra_status` now includes
     `"disk": self._storage.disk_stats()` immediately after a successful Blink
     login, so storage is visible even before the first clip download.
  3. **`app.py` `_on_clips_downloaded`** — refreshes `extra_status["disk"]`
     after every successful download batch.
  4. **`app.py` `_poll_cycle`** — refreshes `extra_status["disk"]` at the end
     of every poll cycle (even when no new clips were downloaded) so the web UI
     always reflects current disk usage.

- **Fixed 2FA Verify button re-enabling after successful submission** — The
  `finally` block in `submitTwoFA()` was unconditionally re-enabling the button
  and resetting its label to "Verify", making it trivial to accidentally submit
  the same code twice (which Blink rejects).

  The `finally` block has been removed.  The button is now re-enabled (with
  "Verify" text) **only on error** (HTTP failure or network exception).  On
  success it stays disabled and shows "✓ Submitted", relying on the 3-second
  `checkAuthStatus` poll to automatically close the overlay once the add-on
  confirms sign-in.

### Improvements

- **Smoother video playback** — Three complementary changes reduce the
  micro-stalls that caused choppy playback:

  1. **Larger I/O chunks** — `_handle_stream` now reads 256 KiB at a time
     (up from 64 KiB) for both full-file and range-request streaming.  Fewer
     round-trips between the server and browser means the browser can fill its
     decode buffer faster.

  2. **`Cache-Control: public, max-age=3600`** — both streaming paths now send
     this header so the browser can cache video segments locally.  Seeking to an
     already-watched position no longer triggers a new server request.

  3. **Video.js `preload: 'auto'` and native HTML5 video** — The embedded
     player now pre-loads video content immediately instead of waiting until the
     user clicks play (`preload: 'metadata'` → `preload: 'auto'`).  The VHS
     override (`overrideNative`) has been disabled for plain MP4 progressive
     downloads so the browser's own highly-optimised video decoder handles
     buffering directly without going through the VHS (HLS) layer.

### Tests

- `test_stats_returns_disk_from_extra_status` — asserts that `/api/stats`
  returns the `disk` object when `extra_status["disk"]` is set.
- `test_stats_no_disk_when_extra_status_empty` — asserts the `disk` key is
  absent when `extra_status` is empty (server just started).
- `test_stream_full_has_cache_control` — full-file stream response includes a
  `Cache-Control` header.
- `test_stream_range_has_cache_control` — partial-content (206) response also
  includes `Cache-Control`.
- `test_poll_cycle_updates_disk_stats_in_extra_status` — `_poll_cycle` always
  refreshes `extra_status["disk"]`, even when no new clips were downloaded.
- `test_on_clips_downloaded_updates_disk_stats_in_extra_status` — download
  callback also updates `extra_status["disk"]`.

## 2.5.3

### Bug fixes

- **Fixed `int() argument must be … not 'NoneType'` — clips failing to
  download** — The Blink API returns `null` (→ Python `None`) for `duration`,
  `network_id`, and sometimes `source` on live-view clips and certain camera
  types.  `dict.get(key, default)` only uses its default when the key is
  **absent** — when the key is present with a `None` value the default is
  ignored and `None` is returned.  Calling `int(None)` then raises `TypeError`.

  **Root cause locations and fixes:**

  1. **`database.py` `add_clip`** — Changed `int(clip.get("duration", 0))`,
     `int(clip.get("network_id", 0))`, and `int(clip.get("size_bytes", 0))`
     to use `or 0` (`int(clip.get("duration") or 0)`, etc.) so a present-but-
     null value is coerced to `0` before the `int()` call.  Same treatment for
     the `str()` fields.

  2. **`downloader.py` `_download_clip` result dict** — The `result` dict
     passed to `add_clip` now normalises nullable fields at the source:
     `"duration": int(clip.get("duration") or 0)`,
     `"network_id": int(clip.get("network_id") or 0)`,
     `"source": str(clip.get("source") or "")`.
     This also protects the HA event payload and webhook call which consume
     the same dict.

  3. **`downloader.py` stale warning** — Updated the `"Clip has no address"`
     warning (leftover from before the `address`→`media` fix in 2.5.2) to
     `"Clip has no media URL"`.

- **Regression tests added**:
  - `test_add_clip_with_null_fields` in `test_database.py` — inserts a clip
    where all nullable integer fields are explicitly `None`; asserts they land
    as `0` in the database.
  - `test_download_clip_null_api_fields` in `test_downloader.py` — exercises
    `_download_clip` end-to-end with a clip whose `duration`, `network_id`, and
    `source` are all `None`; asserts the returned result dict has safe defaults.

## 2.5.2

### Bug fixes

- **Fixed "Clip has no address, skipping" — no clips downloading** — The Blink
  API returns the video URL in a field named `"media"`, not `"address"`.  Our
  `_download_clip` was calling `clip.get("address", "")` which always returned
  an empty string, causing every clip to be skipped with the "has no address"
  warning.

  **Changes in `downloader.py`:**
  - `clip.get("address", "")` → `clip.get("media", "")` (the correct Blink API
    field name, as used in blinkpy's own `_parse_downloaded_items`).
  - Added a `deleted` filter in `_apply_filters`: clips where
    `clip.get("deleted", False)` is truthy are now silently skipped before the
    download stage, matching blinkpy's own behaviour.

  **Test fixture updated** (`conftest.py`): `sample_clip` now uses `"media"`
  instead of `"address"` to reflect the real API response shape.

### Improvements

- **Web UI now follows the HA theme** — The Blink Clips panel defaults to a
  **light theme** that matches Home Assistant's default UI.  The theme
  automatically switches to dark when the operating system or browser prefers
  dark mode (`prefers-color-scheme: dark`) — the same signal HA uses for its
  own default theme.

  A **☀ / 🌙 toggle button** in the top-right of the nav bar lets users
  override the automatic choice.  The preference is stored in `localStorage`
  and persists across page loads.

  All previously hardcoded dark-mode colours (`#0d2818`, `#1a3055`, `#a9d1f7`,
  etc.) are now CSS custom properties (`--badge-ok-bg`, `--tag-bg`,
  `--code-color`, etc.) so both themes render correctly.

## 2.5.1

### Bug fixes

- **Fixed `AttributeError: 'dict' object has no attribute 'status'` — clips not
  downloading** — Our `_fetch_clip_list` was treating the return value of
  `blinkpy.api.request_videos()` as an aiohttp response object (checking
  `.status` and calling `.json()`).  In blinkpy ≥ 0.22 the library returns
  the **parsed JSON dict directly** (via `auth.query → validate_response` with
  `json_resp=True`).  Non-200 responses raise exceptions rather than returning
  an error response object.

  **Changes in `downloader.py`:**
  - Removed `response.status` and `await response.json()` calls from
    `_fetch_clip_list`.
  - Now treats the `request_videos()` return value as a dict and reads
    `data.get("media") or []` directly.
  - Wrapped the `request_videos()` call in a `try/except` so any blinkpy
    exception (`UnauthorizedError`, `BlinkBadResponse`, etc.) is caught,
    logged, and results in an empty list rather than an unhandled crash.
  - Added a `isinstance(data, dict)` guard for unexpected return types.

  **Tests updated** to mock `request_videos` returning dicts (not mock
  response objects), and the error-path test now uses `side_effect=Exception`
  to reflect how blinkpy actually signals failures.

## 2.5.0

### Bug fixes

- **Fixed `s6-svscan: fatal: another instance of s6-svscan is already running`
  — definitive fix** — Root cause was the AppArmor profile missing the `k`
  (file-lock) permission flag on `/run/`.  Without `k`, AppArmor silently
  blocks `fcntl(F_SETLK)` calls.  s6-svscan uses `fcntl`-based locking to
  acquire an exclusive lock on `/run/service/.s6-svscan/lock` at startup;
  when that call is blocked it reports "another instance already running"
  even when no competing process exists.  This was the true root cause in
  every prior version — the service-structure and ENTRYPOINT changes in
  2.2.x–2.4.0 were all red herrings.

  **Changes modelled on the official `home-assistant/apps-example` repo:**

  1. **`apparmor.txt` completely rewritten** — replaced the hand-rolled
     per-path rules with the canonical HA add-on AppArmor pattern:
     - `file,` (blanket file access — supersedes all individual `r`/`w`/`x`
       rules and implicitly includes the `k` lock flag)
     - `/run/{,**} rwk,` — explicit `rwk` so `fcntl(F_SETLK)` is permitted
       on every path under `/run/`, which is exactly what s6-svscan needs.

  2. **Switched from `s6-rc.d/` to `services.d/`** — the official example
     uses the S6-overlay v3 *legacy services* path
     (`/etc/services.d/<name>/run` + `finish`), not `s6-rc.d/`.  Removed
     `rootfs/etc/s6-overlay/s6-rc.d/` entirely; created
     `rootfs/etc/services.d/blink-downloader/run` and `finish`.

  3. **Service script shebangs corrected** — `run` uses
     `#!/usr/bin/with-contenv bashio`; `finish` uses `#!/usr/bin/env bashio`,
     matching the official example exactly.

  4. **`Dockerfile` chmod targets updated** — now points at
     `/etc/services.d/blink-downloader/{run,finish}`.

## 2.4.0

### Bug fixes

- **Fixed `s6-svscan: fatal: another instance of s6-svscan is already running`
  — for real this time** — The root cause was identified with the help of
  concrete diagnostic guidance: the previous fix (`ENTRYPOINT ["/run.sh"]`
  with `exec /init "$@"` inside `run.sh`) was itself **the bug**.

  The HA base image's `ENTRYPOINT ["/init"]` already starts s6-overlay
  exactly once.  Our wrapper called `/init` a second time (even via `exec`),
  which triggered a second s6-svscan — producing the "another instance"
  crash.  The correct pattern for HA OS add-ons is to **never override
  ENTRYPOINT and never call `/init` or `s6-svscan` from any script**.

  **Changes:**
  - Removed `ENTRYPOINT ["/run.sh"]` from `Dockerfile` — the base image's
    own `ENTRYPOINT ["/init"]` is used unchanged.
  - `rootfs/run.sh` no longer calls `/init`; it is a reference-only file
    and is not invoked by any startup mechanism.
  - The `rootfs/etc/s6-overlay/s6-rc.d/blink-downloader/run` script
    (the actual s6 service entry point) remains `exec python3 -m blink_downloader`
    with no s6 or init commands.

- **Fixed maintainer name not appearing in HA add-on repository** —
  `repository.yaml` at the repo root was still using the placeholder
  `Your Name <your@email.com>`.  Updated to `Brian Baggs <brianbaggs@hotmail.com>`.
  Also updated the placeholder GitHub URL in `config.yaml`, `Dockerfile`, and
  `repository.yaml` from `yourusername` to `brianbaggs`.

### Internal

- `rootfs/run.sh` updated with a clear comment explaining that the HA base
  image manages s6-overlay startup and that no script should call `/init`.

## 2.2.0

### Bug fixes

- **Fixed `s6-svscan: fatal: another instance of s6-svscan is already running`**
  — The `s6-rc.d/user/contents.d` bundle registration approach used in v2.1.x
  conflicts with the supervision tree the HA base image already owns.  Switched
  to the **`/etc/services.d/`** legacy service format, which S6-overlay v3
  supports via its backward-compatibility layer and does not touch the user
  bundle or start a second svscan.  Also added a `finish` script to the service
  directory that prevents rapid crash-restart loops (10 s back-off on unexpected
  exits).

- **Fixed "App not running — Start?" ingress loop** — The Python process was
  calling `sys.exit(1)` on configuration errors and returning immediately on
  Blink authentication failures.  Both killed the aiohttp web server, leaving
  port 8099 silent and causing HA ingress to report the add-on as not running.
  Three code-path changes fix this:

  1. **`__main__.py`** — removed `sys.exit(1)`.  On any `load_config()` error
     the process now creates a minimal `AppConfig` with `startup_error` set and
     continues into the normal app lifecycle.

  2. **`app.py` — startup-error mode** — when `startup_error` is set the web
     server starts as normal, the auth state is set to `"error"` (visible on
     the Status tab), and the process sleeps in a loop until SIGTERM rather than
     exiting.  HA ingress sees port 8099 up and the sidebar panel loads.

  3. **`app.py` — `_connect_with_retry()`** — replaces the bare `try/except`
     that returned on auth failure.  On `TwoFARequired` or any other Blink
     exception the add-on sends the HA notification, logs the error, waits
     `_reconnect_interval` seconds (default 60), and retries indefinitely.
     The process never exits between retries; the web server stays up the whole
     time.  SIGTERM is responded to promptly because the wait loop checks
     `_running` every second.

### Improvements

- Added `startup_error: str = ""` field to `AppConfig`; set by `__main__` when
  `load_config()` raises, consumed by `app.run()` to enter web-only mode.
- `_reconnect_interval` and `_startup_poll_interval` instance attributes on
  `BlinkClipDownloaderApp` (default 60 s and 1 s respectively) can be overridden
  in tests to keep the suite fast without patching `asyncio.sleep`.
- `services.d` `finish` script: logs exit code and adds a 10 s sleep before S6
  restarts the service on unexpected crashes.

## 2.1.2

### Bug fixes

- **Fixed `/init: exec: line 45: s6-overlay-suexec: Permission Denied`** —
  Root cause identified through tarball inspection of the HA base image:

  **Why this error occurs:**  `s6-overlay-suexec` is a *setuid-root* ELF
  binary whose real path inside the container is
  `/package/admin/s6-overlay-helpers-<version>/command/s6-overlay-suexec`.
  It is exposed via a two-hop symlink chain:
  `/command/s6-overlay-suexec` →
  `/package/admin/s6-overlay-helpers/command/s6-overlay-suexec` →
  real binary.  AppArmor resolves symlinks to their real path when checking
  `execve` permissions — so the `/command/** mrix,` rule that was already
  in the profile covered only the *symlink name*, not the *binary the kernel
  actually loads*.  The `Permission Denied` was AppArmor denying exec on
  `/package/admin/s6-overlay-helpers-*/command/s6-overlay-suexec`.

  **Fixes applied to `apparmor.txt`:**

  1. Added `/package/** mrix,` — covers all S6-overlay real binary paths
     (`s6-overlay-helpers`, `s6`, `s6-rc`, `s6-linux-init`, `execline`,
     `s6-portable-utils`, etc.) regardless of version number.

  2. Added Linux capabilities block:
     `capability setuid, setgid, chown, dac_override, fowner,
     net_bind_service` — `s6-overlay-suexec` is setuid root and calls
     `setresuid()`/`setresgid()` to switch UIDs; without `capability setuid`
     and `capability setgid` the kernel refuses the UID-switch even after
     the `execve` succeeds.

  3. Expanded runtime path coverage: `/run/service/** rwix,`,
     `/etc/services.d/**`, `/etc/cont-init.d/**`, `/etc/cont-finish.d/**`,
     `/etc/fix-attrs.d/**` — all paths the S6 startup sequence reads and
     executes from.

  4. Added broad `/bin/** mrix,`, `/usr/bin/** mrix,`, `/sbin/** mrix,`,
     `/lib/** mr,`, `/usr/lib/** mr,` — S6 supervision scripts invoke many
     Alpine utilities; without these the shell inside S6 service scripts
     could not execute basic commands.

  5. Removed over-specific `deny /etc/** w,` / `deny /bin/** wl,` /
     `deny /sbin/** wl,` deny rules that conflicted with the new broad
     execute rules.  The profile relies on AppArmor's default-deny posture
     for anything not explicitly allowed; the only retained `deny` is
     `deny /root/** rw,` to protect the root home directory.

## 2.1.1

### Bug fixes

- **Fixed `/bin/sh: can't open /init: Permission denied` — root cause found and
  eliminated** — Thorough research against HA OS 17.3 / Supervisor 2026.x revealed
  three compounding issues that together produced the error:

  1. **AppArmor profile was blocking S6's own init binary.**  HA Supervisor 2026.x
     enforces AppArmor more strictly than earlier versions.  The profile was missing
     explicit allow rules for `/init` (the S6-overlay ELF binary), the `/command/`
     directory (where S6v3 stores `with-contenv` and all supervision binaries),
     `/bin/sh`, `/bin/bash`, and the full S6 runtime state paths
     (`/run/s6/**`, `/run/s6-rc*/**`, `/run/service/**`,
     `/run/container_environment/**`).  Without `/init mrix,`, the kernel denied
     `open()` on the init binary and the error surfaced exactly as seen.
     All missing rules have been added to `apparmor.txt`.

  2. **Wrong `with-contenv` shebang path.**  The service `run` script used
     `#!/usr/bin/with-contenv bashio`.  The canonical, forward-compatible path in
     S6-overlay v3 HA base images is `#!/command/with-contenv bashio` — this is
     what every official HA add-on uses and what the base image's own AppArmor
     baseline expects.  Updated in both
     `rootfs/etc/s6-overlay/s6-rc.d/blink-downloader/run` and `rootfs/run.sh`.

  3. **Missing `dependencies.d/base` declaration.**  S6-overlay v3 requires each
     longrun service to contain an empty file at
     `s6-rc.d/<service>/dependencies.d/base` to declare that it must not start
     until the base bundle has fully initialised.  Without it the service could
     be launched before the container environment (including `SUPERVISOR_TOKEN`)
     was ready.  The file has been added.

- **Git execute bit set on service `run` script** — `git update-index
  --chmod=+x` is now applied to
  `rootfs/etc/s6-overlay/s6-rc.d/blink-downloader/run` and `rootfs/run.sh`
  so the execute permission is embedded in the repository (`100755`) and
  survives a clean clone without relying solely on the Dockerfile `chmod`.

## 2.1.0

### Bug fixes

- **Fixed `/bin/sh: can't open /init: Permission denied` crash on HA OS** —
  Switched from the S6-overlay v3 `CMD`-based one-shot mechanism to a proper
  S6v3 **longrun service definition** at
  `/etc/s6-overlay/s6-rc.d/blink-downloader/`.  The old `CMD ["/run.sh"]`
  approach triggered an S6v3 internal shutdown path that calls `/init` via
  `/bin/sh`; `/init` is mode 711 in the base image so the shell read fails.
  Using a named longrun service bypasses that shutdown path entirely.
- **AppArmor updated** — added `/etc/s6-overlay/**` read and
  `/run/s6*/**` read-write rules so the S6v3 runtime state directories are
  accessible within the add-on's AppArmor sandbox.
- **Fixed `webhook_url` schema** — changed from `"url?"` to `"str?"` so
  leaving the field blank no longer causes `expected a URL` validation errors
  when saving add-on configuration.
- **Fixed base image tag** — corrected `build.yaml` and `Dockerfile` to use
  the full arch-prefixed Alpine tag
  (`ghcr.io/home-assistant/{arch}-base-python:3.12-alpine3.20`), resolving
  the `image not found` build error.
- **Fixed UTC date mismatch in stats** — `get_stats()` and `get_camera_stats()`
  now use `datetime.now(timezone.utc).date()` instead of `date.today()`,
  preventing incorrect "today" counts in US timezones after 5 pm UTC−5/−8.

### Improvements

- **Web 2FA UI** — a sanitised 6-digit input overlay appears automatically in
  the Blink Clips web panel whenever Blink requires a verification code; no
  more manual `/data/two_fa_code.txt` file editing.
- **HA Blink integration coexistence** — the add-on and the built-in HA Blink
  integration can run side-by-side without conflict.  They use independent API
  sessions and separate credential storage (`/data/auth_credentials.json` vs.
  HA's own storage); the add-on does not touch any Blink-integration entities.

## 2.0.0

### New features

- **Web library UI** — built-in Video.js media server (port 8099 / HA ingress sidebar panel)
  with clip grid, thumbnails, search, camera/date/source/tag filters, sort, starred filter,
  and a camera sidebar.
- **SQLite clip library** — all downloaded clips are indexed in `/data/clip_library.db`,
  enabling fast filtering, tag management, and starred clips.
- **Video.js player** — in-browser streaming with seek, fullscreen, PiP, loop,
  theater mode, autoplay-next, and configurable playback rates.
- **Bulk ZIP export** — select multiple clips and download them as a single ZIP archive.
- **Tag support** — add/remove freeform tags per clip; filter the library by tag.
- **Keyboard shortcuts** — Space/F/M/L/Esc/↑↓/←→ with a `?` help overlay.
- **Browser notifications** — opt-in desktop notifications when new clips arrive.
- **Activity heatmap** — 7-day clip count chart on the Status tab.
- **Event-driven instant download** — subscribe to HA `state_changed` events and
  trigger a fast-poll immediately when a Blink motion sensor fires.
- **Fast-poll mode** — configurable burst polling after motion events
  (`fast_poll_interval`, `fast_poll_duration`, `post_motion_delay`).
- **Daily digest** — scheduled HA notification summarising downloads and storage.
- **ZIP archiving** — compress clips older than a threshold into monthly ZIPs.
- **Minimum clip duration filter** — skip clips shorter than N seconds.
- **HA ingress panel** — automatic sidebar entry "Blink Clips" via `ingress: true`;
  all web UI API calls use the `X-Ingress-Path` prefix so ingress and direct access
  both work correctly.
- **Retry delay** — configurable `retry_delay` (base seconds, multiplied per attempt).

### Improvements

- `config.yaml`: added ingress, panel icon/title, `retry_delay` option, corrected
  `max_storage_gb` type to `float`, removed placeholder `image` field.
- `Dockerfile`: added `io.hass.*` OCI labels and `BUILD_ARCH` / `BUILD_VERSION` ARGs
  for multi-arch HA OS builds.
- `apparmor.txt`: added `/tmp/`, `/run/s6-linux-init-container-results/exitcode`,
  and `site-packages` paths required for Python and S6-overlay operation.
- `translations/en.yaml`: added `retry_delay` translation.
- Test coverage raised to 88 % across 245 tests; event_watcher coverage 98 %.

## 1.0.0 — Initial release

- Continuous polling for new Blink camera clips
- Organise by camera name and date
- Configurable filename format with `{camera}`, `{timestamp}`, `{date}`, `{time}`, `{id}` tokens
- Storage quota management and auto-retention policy
- Camera whitelist filtering
- Motion-only clip filter
- Time-window filter (e.g. nighttime only)
- Download JPEG thumbnails alongside clips (optional)
- Configurable concurrent downloads with retry/back-off
- Home Assistant persistent notifications
- HA custom event `blink_clip_downloaded` per clip
- Virtual sensor `sensor.blink_downloader_status`
- Webhook URL support
- Newline-delimited JSON clip manifest at `/data/clip_manifest.json`
- Statistics snapshot at `/data/stats.json`
- File-based 2FA code entry via `/data/two_fa_code.txt`
- Manual trigger via `/data/trigger_download`
- Cached auth tokens for restart-free operation
- Graceful shutdown on SIGTERM / SIGINT
