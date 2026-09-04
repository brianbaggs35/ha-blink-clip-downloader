# Blink Clip Downloader — Documentation

## Overview

This add-on continuously polls the Blink API for new camera clips and saves them to
your local storage (under `/share/blink-clips` by default). It includes a built-in
web library UI, PostgreSQL clip database, event-driven instant download, daily digest
notifications, ZIP archiving, full Home Assistant integration, and an AI video
analysis engine that automatically flags suspicious activity.

---

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Click **⋮** (top-right) → **Repositories** → add
   `https://github.com/brianbaggs35/ha-blink-clip-downloader`
3. Refresh the page; find **Blink Clip Downloader** and click **Install**.
4. Open the **Configuration** tab, fill in your Blink credentials, and save.
5. Click **Start**.

### Web UI Access

After starting, the clip library is accessible two ways:

- **HA Sidebar** — a **Blink Clips** panel appears automatically (powered by HA
  ingress; no extra port or auth needed).
- **Direct URL** — `http://<ha-ip>:8099` (requires the `8099/tcp` port mapping to
  be forwarded).

---

## Disk Space

This add-on can use noticeably more disk space than a typical add-on, mostly driven
by what you enable. Actual measurements from a real running install:

| What | Size | Notes |
|---|---|---|
| Docker image | ~4.2 GB | Bundles PyTorch, OpenCV, and the rest of the optional computer-vision stack (see below) whether or not you ever turn it on — the image size itself doesn't shrink if you leave those features off, only their runtime CPU/RAM/model-download cost does. |
| PostgreSQL database (`/data/postgresql/`) | ~50 MB baseline, grows slowly | Mostly fixed overhead (system catalogs, indexes) at small clip counts, not a strict per-clip cost — expect gradual growth as your clip/analysis history accumulates, not a runaway one. |
| Downloaded clips (`/share/blink-clips/`) | Depends entirely on your cameras and settings | Individual clip sizes vary by resolution and motion duration — a real account's recent clips ranged roughly 0.2–14 MB each, a few MB on average. Total storage is governed by `retention_days` and `max_storage_gb` (see **Retention & Quota** below); this is the number that grows unbounded if you don't set a quota. |
| Computer-vision models (`/data/model_cache/`), only if `ai_enhanced_detection_enabled` is on | 100 MB–800 MB+ combined | Downloaded once, on first use, and cached — see **Computer-Vision Enhancement Pipeline** below for which model sizes correspond to which detection-model choice. |
| Moondream local model, only if `ai_provider`/`ai_escalation_provider` is `moondream_local` | ~430 MB | Downloaded automatically on first health check. |

**Practical takeaway:** the Docker image itself is the one cost every install pays
regardless of configuration (~4.2 GB) — budget for that plus whatever clip storage
your `max_storage_gb` quota allows, and add the CV model sizes above only if you
turn on those specific features. A boot medium recommendation from Home Assistant's
own installation docs applies here as much as anywhere: pair a Raspberry Pi with a
USB SSD or NVMe HAT rather than a microSD card, both for the extra room this add-on
needs and because microSD cards are not recommended for production Home Assistant
installs generally (wear-induced failures under sustained write load, which this
add-on's continuous polling and clip downloads generate a fair amount of).

---

## Uninstallation

1. Go to **Settings → Add-ons → Blink Clip Downloader → Uninstall**.
2. The supervisor removes the add-on container and its `/data/` directory
   (auth tokens, database, tracker, manifest) automatically.
3. Downloaded clips in `/share/blink-clips/` are intentionally **not** deleted —
   your recordings are kept. Remove them manually if no longer needed.

> **Note:** Uninstalling the add-on has no effect on Home Assistant itself.
> The `sensor.blink_downloader_status` entity becomes unavailable after the add-on
> stops and disappears from the entity registry once HA next restarts.

---

## Using alongside the HA Blink Integration

This add-on is **fully compatible** with the built-in Home Assistant Blink integration
(`Settings → Devices & Services → Blink`). You can and should run both at the same time:

| | HA Blink Integration | This Add-on |
|---|---|---|
| **Purpose** | Live camera view, motion sensors, arm/disarm | Clip archiving, library & playback |
| **Auth storage** | HA's own credential storage | `/data/auth_credentials.json` |
| **API session** | Independent | Independent |
| **HA entities** | `binary_sensor.blink_*`, `camera.blink_*` | `sensor.blink_downloader_status` |

Each authenticates with Blink separately and holds its own session token.
They do not share state and cannot interfere with each other.

> **Tip:** If you want automations that react to motion *and* archive clips, the
> recommended setup is: let the HA Blink integration own `binary_sensor.blink_*`
> entities for motion triggers, and enable `watch_ha_events` in this add-on so that
> every time a motion sensor fires, the add-on immediately polls Blink for the new
> clip — combining real-time alerts from the integration with permanent local storage
> from the add-on.

> **API rate limits:** Both systems make independent API calls to Blink.  With default
> settings (`poll_interval: 300`) the combined traffic is well within Blink's rate
> limits, but if you drop `poll_interval` below 60 seconds you may occasionally see
> transient authentication errors on one or both.

---

## Changing Sync Modules or Adding Cameras

The add-on discovers the Blink account's sync modules and cameras from the Blink
cloud API rather than storing a fixed hardware list. This supports replacing a
sync module, including moving to a **Sync Module XR+**, and adding **Blink Outdoor
4** cameras alongside existing cameras. Outdoor 4 devices are handled through
blinkpy's normal camera API; no model-specific option is required.

After the change:

1. Finish onboarding the XR+ and Outdoor 4 cameras in the Blink app first.
2. Leave `camera_filter` empty (the default) if newly added cameras should be
   downloaded automatically. If a whitelist is configured, add each new camera's
   exact Blink name to it.
3. The add-on refreshes the account topology periodically while it is running.
   A restart is not required, although restarting after hardware changes is
   reasonable if you want the new camera list immediately.

An offline camera or sync module remains represented using blinkpy's reported
offline state. It is not removed just because its batteries are dead or its
power/network connection is unavailable. Removal is only applied after a
successful available-topology response no longer lists the device.

Existing clips, tracker entries, camera configurations, and the library database
are keyed by clip/camera names and are not deleted when a sync module is replaced.
If the new cameras use new names, they initially use the normal defaults in the
web UI; configure their per-camera AI or vehicle settings after they appear.

If `download_local_storage` is enabled, the add-on enumerates every currently
available sync module on each local-storage pass. A replaced module's old USB
manifest is not reused, and the new module's manifest is discovered after the
topology refresh.

---

## Two-Factor Authentication (2FA)

If your Blink account has 2FA enabled, an **input overlay** appears automatically
in the **Blink Clips** web panel the first time the add-on needs a verification code.
Enter the 6-digit code from your authenticator app or SMS directly in the browser —
no SSH or file editing required.

The overlay dismisses automatically once the code is accepted and the library loads.

After a successful login, auth tokens are cached in `/data/auth_credentials.json`
and reused on subsequent starts. You will only be prompted for 2FA again if the
refresh token expires (typically after 30+ days with the add-on stopped).

> **Legacy fallback:** You can still write the code to `/data/two_fa_code.txt` via
> SSH if the web UI is unavailable:
> ```bash
> echo "123456" > /data/two_fa_code.txt
> ```

---

## Configuration Options

### Credentials

| Option | Default | Description |
|--------|---------|-------------|
| `username` | _(required)_ | Blink account email |
| `password` | _(required)_ | Blink account password |

### Storage

| Option | Default | Description |
|--------|---------|-------------|
| `download_path` | `/share/blink-clips` | Absolute path for saved clips (must be under `/share/`) |
| `organize_by_camera` | `true` | Create a sub-folder per camera name |
| `organize_by_date` | `true` | Create a sub-folder per recording date (`YYYY-MM-DD`) |
| `filename_format` | `{camera}_{timestamp}` | Clip filename template (see tokens below) |

#### Filename format tokens

| Token | Example | Meaning |
|-------|---------|---------|
| `{camera}` | `Front_Door` | Camera name (special chars replaced with `_`) |
| `{timestamp}` | `20240615_083000` | `YYYYMMDD_HHMMSS` in UTC |
| `{date}` | `2024-06-15` | Date part only |
| `{time}` | `083000` | Time part only |
| `{id}` | `99001` | Blink clip ID |

### Polling

| Option | Default | Description |
|--------|---------|-------------|
| `poll_interval` | `300` | Seconds between regular polls (30–3600) |
| `max_clips_per_poll` | `50` | Maximum clips downloaded in one cycle |

On a first-ever poll (fresh install, or reconnecting the Blink account after
`/data` was wiped — e.g. an uninstall/reinstall), the initial download window
looks back **6 hours**, not further — a normal restart or upgrade always
resumes from wherever the last real poll left off instead. If AI analysis is
enabled, only the **5 most recent** clips from any single download batch are
queued for automatic analysis, regardless of how many clips that batch
contains; the rest still download and appear in the library normally, just
without auto-analysis, so a fresh reconnect can't silently burn through a
large batch of API tokens re-analyzing a backlog. Any clip is always
analyzable on demand via **Analyze Now** (or **Bulk select → 🔬 Analyze
selected** for several at once) regardless of this cap.

### Retention & Quota

| Option | Default | Description |
|--------|---------|-------------|
| `retention_days` | `30` | Auto-delete clips older than N days (0 = keep forever) |
| `max_storage_gb` | `10.0` | Stop downloading when the folder exceeds N GB (0 = unlimited) |

### Filtering

| Option | Default | Description |
|--------|---------|-------------|
| `camera_filter` | `[]` | Only download from these cameras (empty = all) |
| `motion_only` | `false` | Skip clips not triggered by a PIR motion sensor |
| `time_window_start` | `""` | `HH:MM` — only download clips recorded at or after this time |
| `time_window_end` | `""` | `HH:MM` — only download clips recorded at or before this time |
| `min_clip_duration` | `0` | Skip clips shorter than N seconds (0 = keep all) |

### Download Options

| Option | Default | Description |
|--------|---------|-------------|
| `download_thumbnails` | `false` | Save a JPEG thumbnail (first frame, via ffmpeg) alongside each clip. Enabling this also gradually backfills thumbnails for clips downloaded earlier and for clips re-imported after an uninstall/reinstall (a few per poll cycle until the library is fully covered). |
| `concurrent_downloads` | `3` | Parallel downloads (1–10) |
| `retry_attempts` | `3` | Retries per failed download |
| `retry_delay` | `5.0` | Base seconds between retries (multiplied by attempt number) |

### HA Notifications

| Option | Default | Description |
|--------|---------|-------------|
| `notify_ha` | `false` | Send a persistent HA notification when new clips arrive (also covers the daily digest and system events — 2FA/auth/storage). Off by default: a continuously-syncing account can download clips every poll cycle, and a notification per download quickly becomes noise. See `notify_ha_suspicious` below for an alert scoped to suspicious clips only. |
| `ha_notification_title` | `"Blink Clip Downloaded"` | Title for HA notifications |

### Extra Features

| Option | Default | Description |
|--------|---------|-------------|
| `webhook_url` | `""` | POST clip metadata to this URL after each download |
| `create_clip_manifest` | `true` | Append metadata to `/data/clip_manifest.json` |

### Clip Library Database

| Option | Default | Description |
|--------|---------|-------------|
| `enable_library_db` | `true` | Store clip metadata in a PostgreSQL database. Required for the web UI and AI analysis. |

### Web Library UI

| Option | Default | Description |
|--------|---------|-------------|
| `enable_media_server` | `true` | Start the built-in web UI |
| `media_server_port` | `8099` | TCP port for the web UI (also the ingress port) |

### Event-Driven Instant Download

| Option | Default | Description |
|--------|---------|-------------|
| `watch_ha_events` | `true` | Subscribe to HA `state_changed` events for instant download |
| `fast_poll_duration` | `120` | Seconds to stay in fast-poll mode after a motion event |
| `fast_poll_interval` | `15` | Poll interval (seconds) while in fast-poll mode |
| `post_motion_delay` | `30` | Seconds to wait after motion clears before polling (5–300) |
| `event_cameras` | `[]` | Only fast-poll for motion from these cameras (empty = all) |

> **Tip:** Blink typically takes 15–60 seconds to encode and upload a clip after
> motion ends. The default `post_motion_delay` of 30 s is a good starting point;
> increase it if clips are missing from the first fast poll.

### Daily Digest

| Option | Default | Description |
|--------|---------|-------------|
| `digest_enabled` | `true` | Send a daily HA notification with a download summary |
| `digest_time` | `"08:00"` | Local time to send the digest (24-hour, e.g. `"08:00"`) |

### ZIP Archiving

| Option | Default | Description |
|--------|---------|-------------|
| `archive_enabled` | `false` | Compress old clips into monthly ZIP files |
| `archive_after_days` | `21` | Clips older than N days are archived (1–365) |

**`archive_after_days` must be lower than `retention_days`** (see
[Retention & Quota](#retention--quota) above, default `30`). Retention
deletes a clip's file — and its database row — outright once it crosses
`retention_days`, with no regard for whether it's been archived yet; if
`archive_after_days` isn't comfortably below that, retention deletes most
clips before archiving ever gets a chance to preserve them, and little to
nothing reaches your ZIP archives (or Google Drive, under the
`archived_only` backup policy) no matter how long the add-on runs. A
startup log warning calls this out if your configuration has it backwards.

Eligibility is per clip, not per month: a clip is swept in the moment it
turns `archive_after_days` old, checked once per poll cycle. A practical
effect of this is that a given month's archive can appear to fill in
gradually over the following weeks, rather than all at once — the 1st of
the month crosses the age threshold roughly a month before the 28th–31st
does, so a mid-month check of that month's ZIP will often show it still
short a few of the month's later days. This is expected behavior for
age-based retention, the same way it would work anywhere else clips age out
on a rolling basis — it is not a sign anything is stuck. If you want
everything currently eligible archived immediately rather than waiting for
it to trickle in over the rest of the poll cycle's schedule, use **Run
Archiving Now** on the Storage tab's Archived Clips section, which sweeps
the full current backlog on demand.

### Google Drive Backup

Everything about connecting an account, choosing a backup folder, and picking a
backup policy is done from the **Storage** tab in the web UI, not here — see
[Storage Tab — Google Drive Backup](#storage-tab--google-drive-backup) below
for setup steps. These two options are the only Google Drive knobs that live
in the add-on's own configuration, matching how `ai_batch_size`/
`ai_check_interval` work for the AI analysis queue:

| Option | Default | Description |
|--------|---------|-------------|
| `gdrive_batch_size` | `5` | Clips uploaded to Drive per check cycle (1–50) |
| `gdrive_check_interval` | `300` | Seconds between upload-queue checks (30–3600) |

### Sync Module Local Storage

When a USB drive is plugged into a Blink Sync Module, the module records clips to it
in addition to the cloud. Enabling this option instructs the add-on to also fetch those
locally-stored clips each poll cycle.

| Option | Default | Description |
|--------|---------|-------------|
| `download_local_storage` | `false` | Download clips stored on the USB drive attached to the Blink Sync Module |

**How it works:** After every normal cloud download the add-on checks each Sync Module
for a connected USB drive, fetches the clip list via the Blink cloud API (no direct LAN
access is required), and downloads any new clips into the same folder structure as cloud
clips. Local-storage clips are indexed in the database with `source = "local_storage"`.

### Logging

| Option | Default | Description |
|--------|---------|-------------|
| `log_level` | `info` | `debug`, `info`, `warning`, or `error` |

---

## AI Video Analysis

The add-on can automatically analyse every downloaded clip for suspicious activity and
send alerts via the HA mobile app, email, or Discord. AI analysis is **disabled by
default**; set `ai_analysis_enabled: true` to turn it on.

### Enabling AI Analysis

| Option | Default | Description |
|--------|---------|-------------|
| `ai_analysis_enabled` | `false` | Enable AI clip analysis |
| `ai_provider` | `"ollama"` | Which AI backend to use (see providers below) |
| `ai_batch_size` | `10` | Maximum clips to analyse per batch run |
| `ai_check_interval` | `60` | Seconds between analysis batch runs |
| `ai_schedule_start` | `""` | Only run analysis after this time (`HH:MM`; empty = always) |
| `ai_schedule_end` | `""` | Only run analysis before this time (`HH:MM`; empty = always) |

### AI Providers

Six providers are supported. Set `ai_provider` to one of the values below:

#### Ollama (Local/LAN) — `ollama`

Sends frames to an Ollama server running a vision model on your local network.
Free and fully private — no data leaves your home.

| Option | Default | Description |
|--------|---------|-------------|
| `ollama_url` | `""` | URL of your Ollama server, e.g. `http://192.168.1.10:11434` |
| `ollama_model` | `""` | Vision model to use (e.g. `llava:7b`, `llama3.2-vision:11b`). Use **Fetch Models** in the web UI to browse available models. |

Only vision-capable models are shown in the model picker; text-only models are filtered out.

#### Ollama Cloud — `ollama_cloud`

Uses the [Ollama Cloud API](https://api.ollama.com) instead of a local server. Identical
API surface to local Ollama, so model selection and token counting work the same way.

| Option | Default | Description |
|--------|---------|-------------|
| `ollama_url` | `""` | Leave empty to use the default Ollama Cloud endpoint |
| `ollama_model` | `""` | Model name (same as local Ollama) |
| `ollama_cloud_api_key` | `""` | API key from api.ollama.com |

#### Moondream Cloud — `moondream_cloud`

Sends frames to the [Moondream Cloud API](https://docs.moondream.ai/api). Billed per
request; reasoning mode is enabled by default for better spatial analysis.

| Option | Default | Description |
|--------|---------|-------------|
| `moondream_api_key` | `""` | API key from moondream.ai |

#### Moondream Local — `moondream_local`

Runs Moondream directly on the host device via the **Install** button in the AI tab.

> **Hardware note:** as of the `moondream` package's 1.x line, on-device inference
> requires an NVIDIA CUDA or Apple Silicon GPU — pure-CPU inference is no longer
> offered by the package. Most Home Assistant OS hosts do not have a GPU passed
> through to the add-on container, so this provider will report itself as
> unavailable on them, on **both** amd64 and aarch64; use `moondream_cloud` or
> `ollama` instead. (Before 4.1.0 this provider was amd64-only — the add-on's
> Alpine base image had no wheels for moondream's dependencies on aarch64; the
> 4.1.0 switch to a Debian base image removed that constraint.)

#### Anthropic (Claude) — `anthropic`

Uses the Anthropic Claude API for high-quality vision analysis. Billed per token.

| Option | Default | Description |
|--------|---------|-------------|
| `anthropic_api_key` | `""` | API key from [console.anthropic.com](https://console.anthropic.com) |
| `anthropic_model` | `"claude-haiku-4-5"` | Claude model to use. Use **Fetch Models** in the web UI to browse available models. |

Claude models receive a dedicated system prompt that separates role/format instructions
from user content, improving JSON compliance. `claude-haiku-4-5` is the default as
the most cost-effective option.

#### OpenAI (GPT) — `openai`

Uses the OpenAI API for vision analysis. Billed per token. Any OpenAI vision model is
supported (GPT-4o, GPT-4.1, GPT-4-Turbo, GPT-5, and variants).

| Option | Default | Description |
|--------|---------|-------------|
| `openai_api_key` | `""` | API key from [platform.openai.com](https://platform.openai.com) |
| `openai_model` | `"gpt-4o-mini"` | GPT model to use. Use **Fetch Models** in the web UI to browse available models, newest first. |

GPT models use `response_format: json_object` to guarantee valid JSON output, and
`"high"` image detail for better scene analysis. `gpt-4o-mini` is selected by default
as the most cost-effective option. The o1/o3/o4-mini reasoning models and the entire
GPT-5 family use `max_completion_tokens` instead of the legacy `max_tokens` parameter;
this is handled automatically based on the model name.

#### Two-tier escalation (any provider)

Most motion clips are not suspicious, so paying for a strong model on every clip is
wasted spend or wasted time. When `ai_escalation_enabled` is on, every clip is first
analyzed with the normal `ai_provider`/model (tier 1). Only clips tier 1 flags as
suspicious are re-analyzed by `ai_escalation_provider` (tier 2) for a closer second
opinion, and the tier-2 verdict — not tier 1's — is what gets recorded and alerted on.

| Option | Default | Description |
|--------|---------|-------------|
| `ai_escalation_enabled` | `false` | Master on/off switch for two-tier escalation. When off, `ai_escalation_provider`/`ai_escalation_model` below are ignored entirely and analysis runs on tier 1 alone. |
| `ai_escalation_provider` | `"ollama"` | Tier-2 provider, only used when `ai_escalation_enabled` is on: `"ollama"`, `"ollama_cloud"`, `"moondream_cloud"`, `"moondream_local"`, `"anthropic"`, or `"openai"`. May be a **different** provider than `ai_provider` (e.g. tier 1 = `openai`, tier 2 = `moondream_cloud`) — it reuses that provider's own credential fields already configured above, no separate API key needed. This field always has a value (its dropdown can't be left blank) — `ai_escalation_enabled` is the actual disable switch. |
| `ai_escalation_model` | `""` | Model ID for the escalation provider (e.g. `"gpt-4o"`, `"claude-opus-4-5"`, or a Moondream fine-tune ID). Leave empty to use that provider's default/base model. Not applicable to `moondream_local`, which has no selectable model. |

Recommended pairings: `gpt-4o-mini` + `gpt-4o`, `gpt-5.4-nano` + `gpt-5.4`, or an
inexpensive tier 1 escalating to a more capable tier 2 of a *different* provider
entirely. Tier-1 and tier-2 token usage are tracked separately — the AI Usage tab
shows the escalation model as its own row (priced at its own rate) plus a running
count of how many clips were escalated.

The AI tab's Fetch Models picker works for the escalation model too, once
`ai_escalation_enabled` is on and `ai_escalation_provider` is connected — fetch,
pick from the list, and copy the id to paste into `ai_escalation_model`, the same
copy-to-clipboard flow the tier-1 model picker already uses.

> **Deprecated as of 5.0.0:** `openai_escalation_model` (OpenAI-only, second
> OpenAI model) is deprecated in favor of
> `ai_escalation_enabled`/`ai_escalation_provider`/`ai_escalation_model`
> above, which work for every provider, not just OpenAI — leave it blank on
> any new configuration. It was briefly removed from this add-on's
> Configuration options entirely in 5.0.0, which caused installs that
> already had it set to permanently log an "unknown option" warning from
> the Supervisor (a value can still be present in your saved options with
> no matching schema entry to validate it against); it's back in
> Configuration, clearly marked deprecated, purely to stop that warning. If
> you already had `openai_escalation_model` set, nothing changes for you:
> it's still read and automatically promoted to `ai_escalation_enabled=true`
> + `ai_escalation_provider="openai"` + `ai_escalation_model` on startup.

### Analysis Prompt & Behaviour

| Option | Default | Description |
|--------|---------|-------------|
| `ai_prompt` | _(see config.yaml)_ | Global prompt sent to the AI for each clip. Must request a JSON response with `"suspicious"`, `"confidence"`, and `"description"` keys. |
| `ai_car_description` | `""` | Description of a vehicle to protect (e.g. `"Silver Kia Forte, parked in the driveway"`). When set, the AI applies strict distance rules and flags anyone within ~2 feet of the vehicle as suspicious. |
| `ai_car_cameras` | `[]` | Camera names for which car-proximity rules apply. Must exactly match your Blink camera name (case-sensitive). Leave empty to apply to all cameras. Cameras not listed focus only on their own description, preventing false positives on cameras that cannot see the car. Easier to set via the **Vehicles** tab in the web UI, which lists your actual cameras instead of requiring you to type the name. |
| `ai_min_confidence` | `0.5` | Minimum confidence threshold (0.0–1.0) for sending suspicious-activity alerts. Clips are still analysed and stored; only alert dispatch is gated. The default matches the confidence floor the AI prompt itself uses for a genuine suspicious verdict, so low-confidence hedges don't spam notifications. Set to `0.0` to send alerts for every result, or higher (e.g. `0.7`) to only alert when the AI is very certain. |
| `ai_suspicious_keywords` | _(list)_ | Words that trigger a suspicious flag when found in an AI plain-text response (used as fallback when the AI does not return valid JSON). |

### Frame Extraction

| Option | Default | Description |
|--------|---------|-------------|
| `ai_max_frames` | `5` | Number of frames extracted per clip for analysis (1–100). More frames = better coverage but higher API cost. |
| `ai_frame_interval` | `2.0` | Seconds between frame extraction points (0.5–30). |
| `ai_frame_strategy` | `"smart"` | How frames are selected and sent to the AI (see below). |

`ai_max_frames`/`ai_frame_interval` apply as configured to clips estimated at 30
seconds or less. Longer clips automatically get `ai_max_frames + 2` frames — a small
bump that keeps API/token cost predictable while giving longer clips enough coverage
to describe what happened across the whole clip, not just its first half.

#### Frame Strategies

| Value | Behaviour |
|-------|-----------|
| `"smart"` | (Default) Extracts 2× `ai_max_frames` candidates then uses inter-frame motion-diff (PIL) to pick the entry frame, peak-motion frame, and exit frame, then fills any remaining slots by motion score while enforcing a minimum spacing between picks so they spread across the clip's timeline instead of clustering around a single motion burst. Best accuracy for the same or fewer API calls. |
| `"sequential"` | Analyses each frame individually via separate AI calls and returns the most alarming result (suspicious > non-suspicious; higher confidence when tied). Works well when the AI performs better on single images than on batches. Works with all six providers. |
| `"uniform"` | Extracts exactly `ai_max_frames` (or the long-clip bonus count) at fixed time intervals (legacy behaviour, no motion analysis). |

### Per-Camera Configuration

You can set a description and a custom prompt for each camera without editing YAML —
use the **Camera Configurations** section in the web UI **AI tab**. It lists your
actual Blink cameras so you can't mistype or mismatch a name, and changes are saved
to `/data/camera_configs.json` and take effect immediately without restarting the
add-on. This is the recommended way to configure per-camera settings.

You can also configure these in `config.yaml` for YAML-based setups, but the `camera`
value in each entry must exactly match your Blink camera's name (case-sensitive) —
the add-on has no way to validate a typo against your actual cameras from the options
form, and a mismatched name silently fails to apply:

| Option | Default | Description |
|--------|---------|-------------|
| `ai_camera_descriptions` | `[]` | Per-camera descriptions. Example: `[{camera: "Driveway", description: "Points at driveway; silver Kia Forte parked left"}]` |
| `ai_camera_prompts` | `[]` | Per-camera prompt overrides. Example: `[{camera: "Driveway", prompt: "Focus on vehicle proximity."}]` |

### Vehicles Tab — protected-vehicle monitoring

Everything about protecting a specific vehicle lives in its own **Vehicles**
nav tab (moved out of the AI tab in 5.0.0, since it's a distinct concern from
per-camera AI prompt tuning):

- **Protected Vehicle Description** — describe the vehicle (make/model/color)
  so the AI can identify it. This is now editable directly from the web UI
  (previously only settable via the add-on's Configuration tab as
  `ai_car_description`) and takes effect immediately, no restart needed.
- **"Protected vehicle visible from this camera"** — per camera, enables
  car-proximity rules for that camera without editing `ai_car_cameras` in the
  add-on options. Has no effect until the description above is also set — the
  AI needs to know *what vehicle* to protect, not just which camera can see
  it; the tab shows a warning banner until both are set.
- **Visual car-zone picker** — once a camera is checked, pick one of its
  recent clips and either click-drag a rectangle over where the vehicle
  normally sits (drag the corner handles to resize, drag the body to move),
  or switch to **Freeform** and trace a lasso outline around the vehicle's
  actual silhouette — release the pointer anywhere to auto-close the shape,
  no need to trace precisely back to the start point. Freeform is worth
  reaching for when a rectangle would necessarily include a lot of
  neighboring pavement/another vehicle (e.g. a car parked at an angle, or
  tightly boxed in). Clicking **Save zone** captures a still reference image
  of the exact frame the zone was drawn on alongside it: once saved, the tab
  always shows that frozen snapshot with the zone overlaid, not whatever
  clip happens to be newest whenever you revisit it later — so a car, person,
  or anything else that shows up in a *newer* clip never appears to overlap
  a zone that was set before it arrived. Click **Edit zone** to redraw from a
  fresh frame, or **Clear zone** (with a confirmation prompt) to remove it
  and its reference image entirely. Since a Blink camera doesn't move, the
  zone itself is stable ground truth that doesn't depend on any single
  clip's object detection succeeding: it sharpens accuracy in two ways — a
  code-computed "zone motion" signal (what share of the clip's overall
  motion happened inside the zone vs. elsewhere, using true point-in-polygon
  matching for freeform zones) is added to the AI's evidence, and for
  Moondream providers it's used as a fallback proximity reference when a
  clip's vehicle detection finds nothing at all. Clear the zone to skip it —
  everything else (distance rules, description-based disambiguation) works
  the same with or without one configured.

> **Priority:** `camera_configs.json`/`vehicle_settings.json` (both set via
> the web UI) are the primary source for descriptions, custom prompts,
> car-camera flags, zones, and the protected-vehicle description.
> `ai_camera_descriptions`/`ai_camera_prompts`/`ai_car_description` in
> `options.json` serve as fallbacks for cameras/settings not yet configured
> in the web UI.

### Smart Security Brain (Anomaly Detection)

The add-on builds a behavioural baseline for each camera over time, recording per-camera
hourly event frequency and average clip duration in the clip library database with every
download. This
data powers an **anomaly score** (0.0–1.0) computed for every clip that goes through AI
analysis.

- **Anomaly score 0.0–0.5:** Normal activity for this camera and time of day.
- **Anomaly score ≥ 0.6:** The AI prompt automatically includes a **BEHAVIOR ALERT**
  flag, telling the model to apply heightened scrutiny.
- **Activation threshold:** The system activates after approximately 30 events per
  camera to avoid false positives on new installs.

Every AI call also includes the clip's local time label ("early morning", "evening",
"night", etc.) derived from the clip timestamp, so the model can calibrate what
constitutes suspicious behaviour for that time of day.

The `anomaly_score` is stored in the `analysis_results` table and returned by the
`/api/ai/results/{clip_id}` endpoint (and as part of each clip's data from
`/api/clips`) so you can query and filter by it.

### Visual Scene Baseline

Blink cameras are fixed in place, so a given camera's clips almost always frame the
same porch, driveway, or yard. The add-on takes advantage of this: alongside the
time-of-day/frequency anomaly score above, it also learns what each camera's
background normally looks like.

- Each analysed clip's opening frame is reduced to a small grayscale thumbnail and
  blended into a running per-camera baseline.
- Once a camera has built up enough history (20 clips), each new clip's opening frame
  is compared against that baseline. A frame that closely matches the camera's usual
  background nudges the AI toward a calm, routine read of the activity; a frame that
  looks visually different (an unfamiliar object, vehicle, or obstruction in view) is
  flagged in the prompt as worth a closer look, without being treated as a verdict on
  its own.
- The baseline only learns from clips the AI did **not** flag as suspicious, so a
  genuine intruder or one-off anomaly can't teach the system to treat itself as
  normal.
- This activates automatically — no configuration needed — and complements rather
  than replaces the anomaly score above.

### Feedback & Adaptive Learning

Every analysed clip's AI panel (in the clip modal) has **✅ Correct** / **❌ Not
suspicious** buttons, plus an optional note. This feedback actually changes future
behaviour, per camera, in two ways:

- **Notification threshold auto-tuning.** Repeated "not suspicious" corrections on
  false-positive alerts for a camera gradually raise that camera's effective
  confidence threshold for sending a *notification* — clips are still analysed and
  the description is still stored either way, only whether it's worth interrupting
  you is affected. The adjustment decays automatically as old corrections age out of
  the trailing window, so it never needs a manual reset.
- **Prompt guidance.** Corrections with a note are folded into that camera's prompt
  as bounded few-shot context ("a past clip on this camera was marked suspicious, but
  a human reviewer said this was WRONG: ..."), so the same mistake — in either
  direction, missed or over-flagged — is less likely to repeat.

This is why accurate feedback matters: it's the mechanism for teaching the AI your
specific cameras, routine, and protected assets over time, not just a rating left on
a shelf.

### Prompt Debugging

`ai_prompt_debug_enabled` (off by default) stores each clip's exact prompt text
(not the image frames) alongside its analysis, viewable via a **📝 Prompt** button in
the clip's AI panel. Useful when tuning per-camera prompts/descriptions and wanting
to see exactly what the model was asked, including the automatically injected
proximity/zone/anomaly hints described above.

### Moondream Fine-Tuning

For `moondream_cloud`, the AI tab's **Fine-Tuning** panel manages Moondream's cloud
fine-tuning API directly from the web UI: create a fine-tune, list checkpoints, and
**Activate** one to use for live inference instead of the base model — this sets
`moondream_finetune_model` live without a restart (save it in `config.yaml` too if you
want it to persist across restarts).

**Training from your feedback.** Every 👍/👎 you give a clip's AI verdict (see
"Feedback on AI analysis" above) is stored, and the panel's **Train from Feedback**
button turns unused feedback into real training steps: for each pending correction, it
re-extracts a representative frame from that clip, pairs it with the camera's analysis
prompt and the corrected suspicious/not-suspicious verdict (falling back to the
original verdict for 👍 feedback with no explicit correction), and runs one supervised
fine-tuning step per example against the fine-tune you select. Feedback rows are marked
consumed once trained, so repeated runs only pick up what's new since the last one —
the panel shows how many corrections are currently queued. After a training run,
**Save Checkpoint** persists the result so it shows up under **Checkpoints** to
activate.

### Computer-Vision Enhancement Pipeline (optional, heavy)

⚠️ **Resource warning:** both options below require substantially more CPU and RAM
than the rest of this add-on, and download large ML models (100MB-800MB+ each,
cached under `/data` after first use). Comfortable on a Raspberry Pi 5 (8GB) or
better. Both are off by default, and clip analysis works exactly as it did before
this section existed with both left disabled — the AI provider
(Ollama/Anthropic/OpenAI/Moondream) still makes every suspicious/not-suspicious
call; these stages only feed it better evidence.

ℹ️ **Raspberry Pi 4 and older:** PyTorch (which both options below depend on) has
long-standing, still-unresolved upstream crash reports (`illegal instruction`, e.g.
[pytorch/pytorch#176993](https://github.com/pytorch/pytorch/issues/176993)) on the
Pi 4's Cortex-A72 CPU, which lacks the ARMv8.1 LSE atomic instructions PyTorch's
official ARM builds assume are available. Raspberry Pi 5's Cortex-A76 does not have
this limitation. Because this is a hardware-level crash rather than a Python
exception — nothing catches it after the fact — this add-on checks CPU compatibility
(`/proc/cpuinfo` for the `atomics` feature flag) *before* ever attempting the import
that would trigger it, for every stage below plus local face recognition. On an
incompatible CPU, those stages automatically report themselves unavailable (a
warning in the add-on log, no crash) instead of running; everything else is
unaffected. You don't need to manually avoid enabling these options on a Pi 4 — the
add-on won't let the risky code path run either way — this is just the reason
you'll see them report unavailable there.

| Option | Default | What it adds |
|---|---|---|
| `ai_enhanced_detection_enabled` | `false` | Frame preprocessing (CLAHE contrast enhancement + light denoising, OpenCV), object detection + tracking (YOLO + ByteTrack, via Ultralytics), monocular depth estimation (Depth Anything V2, via transformers), and pixel-level contact segmentation (SAM2, via transformers) — one switch for all four. Depth estimation and contact segmentation have always required object detection to run at all, and there was no real value in toggling preprocessing/detection independently, so earlier versions' four separate settings just multiplied untested on/off combinations without a matching benefit. Adds a code-computed **OBJECT DETECTION** hint (what was detected — people, vehicles, and animals — and how close a detected person or animal is to a detected vehicle), a **TRACKING** hint (lingering/casing vs. briefly passing through, via ByteTrack's frame-to-frame continuity), a **DEPTH ESTIMATE** hint ("overlapping in the 2D frame" vs. "actually at the same distance from the camera" — catches the case where a person or animal only *looks* close to the protected vehicle because of the camera angle), and a **CONTACT ANALYSIS** hint (refining a bounding-box overlap into an actual touching-or-not judgment, e.g. a dog jumping on the car vs. merely standing nearby, using each object's real visible outline). Vehicle-distance/depth/contact language only ever applies on a camera actually designated to view the protected vehicle (`ai_car_cameras`) — other cameras stay isolated even if they happen to detect an unrelated car. |
| `ai_object_detection_model` | `yolo11n.pt` | Which Ultralytics model the detection stage above runs. "n" (nano) models are fastest/lightest and the recommended starting point on CPU-only hardware; larger models (s/m/l/x) are more accurate but much slower. |
| `ai_face_recognition_enabled` | `false` | Local-only face recognition (facenet-pytorch) to suppress alerts for enrolled household members — see below. Kept as its own toggle since it's privacy-sensitive rather than just heavier compute. |

None of these packages are required to install or run the add-on normally; if a
package fails to install in the Docker image (see the Dockerfile) or isn't present
for any other reason, the corresponding option simply reports itself unavailable at
runtime and analysis proceeds exactly as if it were disabled.

**Why this exists:** these stages sit on top of the existing prompt pipeline, not in
place of it — each one produces a bounded, hedged hint appended to the same prompt
the "SCENE BASELINE" and "ZONE MOTION" hints already use, so the model still judges
each clip on what it can actually see, with better evidence to work with.

#### Biometrics Tab — face-recognition enrollment and the suspicious-flag bypass

Once `ai_face_recognition_enabled` is on, enroll household members from the
**Biometrics** nav tab (moved out of the AI tab in 5.0.0). Two ways to
enroll:

- **From a clip (recommended)** — pick a camera and one of its recent clips,
  extract several frames from it, and select as many as you like that show
  the face clearly. Motion-triggered clips often don't have a good angle on
  the face in the very first frame (e.g. a front door camera catching the
  moment the door opens); pulling multiple frames from a real clip and
  picking the good ones gives recognition several real reference angles to
  match against instead of one posed photo, which is what actually makes
  recognition reliable enough to cut down false positives on a camera the
  same few people pass every day.
- **From a photo** — the simpler original flow: give a name and a single
  clear reference photo.

Each selected photo is converted to a numeric face embedding (the photo
itself is not stored) and kept only in this add-on's own database — **never
uploaded anywhere**, regardless of which `ai_provider` is configured. Even
the advisory hint sent to the AI model (any provider, including cloud ones)
is strictly name-free — it only ever says how many locally-enrolled members
matched, never who. A recognized person's actual name is only ever used
afterward, entirely locally, to personalize your own notification text
(e.g. "Brian walked up the driveway" instead of "A person walked up the
driveway").

**Approved for bypass.** Each enrolled person has an "approved" toggle
(on by default). When every face detected in a clip belongs to an approved
enrollment — and only then — the clip's suspicious flag is automatically
cleared, on top of whatever the AI model itself concluded. This is
deliberately all-or-nothing: a single stranger, or someone recognized but
not (or no longer) approved, standing next to an approved family member
still gets the clip flagged normally. Turn a person's approval off (without
deleting them) to keep recognizing/labeling someone — e.g. a regular visitor
— without granting them bypass trust. Enrolled photos and the approved flag
can be managed per person (rename, approve/un-approve, or remove every photo
for that person at once) from the Biometrics tab.

**Reporting recognition accuracy.** A clip's AI panel has a face-recognition
feedback button, separate from the regular correct/incorrect suspicious-flag
feedback: "Wrong match" on a clip where the bypass fired (it recognized the
wrong person, or shouldn't have applied), or "Report a missed face match" on
a clip where an enrolled person was actually present but wasn't recognized.
This is a review trail, not an automatic tuning knob — reports show up on
the Biometrics tab's Face-bypass activity card for you to act on (e.g.
re-enrolling someone with clearer reference photos) rather than silently
loosening matching on their own, since an over-loose match is exactly the
false-bypass risk this feature is designed to avoid.

---

## AI Alerts (Extended Notifications)

When AI analysis flags a clip as suspicious — or a camera's battery goes low,
see [Low-Battery Alerts](#low-battery-alerts) below — it can notify you
through four independent channels. The **Automations** tab has a
**Notification Channels** panel with a one-off test button for each channel
(email, Discord, mobile app push, HA persistent notification) so you can
verify credentials are correct before actually enabling a channel for real
alerts.

### HA Mobile App Push

| Option | Default | Description |
|--------|---------|-------------|
| `mobile_app_enabled` | `false` | Enable push notifications to the HA mobile app |
| `mobile_app_target` | `""` | HA mobile app entity to send alerts to (e.g. `mobile_app_my_phone`) |

### Email (SMTP)

| Option | Default | Description |
|--------|---------|-------------|
| `smtp_enabled` | `false` | Enable email alerts for suspicious clips |
| `smtp_host` | `""` | SMTP server hostname or IP |
| `smtp_port` | `587` | SMTP server port (usually 587 for STARTTLS, 465 for SSL) |
| `smtp_user` | `""` | SMTP login username |
| `smtp_password` | `""` | SMTP login password |
| `smtp_recipients` | `[]` | List of email addresses to send alerts to |
| `smtp_sender` | `""` | From address used in outgoing alert emails |

Once `smtp_host` and `smtp_recipients` are set, the **AI tab** shows a **✉️ Send Test
Email** button (in the Email Alerts card) that sends a one-off test message using those
settings — even while `smtp_enabled` is still `false` — so you can confirm your SMTP
credentials work before turning real alerts on.

### Discord

| Option | Default | Description |
|--------|---------|-------------|
| `discord_enabled` | `false` | Enable Discord alerts for suspicious clips |
| `discord_webhook_url` | `""` | Discord webhook URL to post suspicious-clip alerts to |

### Home Assistant Persistent Notification

| Option | Default | Description |
|--------|---------|-------------|
| `notify_ha_suspicious` | `false` | Create a Home Assistant persistent notification for suspicious clips |

Independent of `notify_ha` (see [HA Notifications](#ha-notifications) above),
which covers every new clip download plus the daily digest and system events
— enable this instead if you only want an in-HA alert for suspicious clips
specifically, without the per-download noise `notify_ha` also produces. Like
`notify_ha`, this shows in HA's own notification panel, not as a phone push
— enable `mobile_app_enabled` above (in [HA Mobile App
Push](#ha-mobile-app-push)) for an actual push notification to your phone.

### Low-Battery Alerts

| Option | Default | Description |
|--------|---------|-------------|
| `battery_alerts_enabled` | `false` | Alert when a camera's battery goes low, via the channels above |

Reuses the exact same four channels configured above (mobile push, email,
Discord, HA persistent notification) — there's nothing separate to set up.
An alert fires once, the moment a camera's battery transitions from normal
to low; it does not repeat while the battery stays low, and there's no
alert for the battery recovering (that's still visible in the Status tab's
battery history, see [Web Library UI](#web-library-ui) below). Enabling this
for the first time never alerts retroactively for a camera that's already
low when you turn it on — only a genuine transition observed after that
counts.

---

## Web Library UI

The built-in web interface lets you browse, search, play, star, tag, and delete clips
from any browser without leaving Home Assistant.

### Features

- **Library tab** — scrollable grid with thumbnails (each showing camera, date,
  source, size, tags, and clip **duration** — both as a badge on the thumbnail and
  as text alongside the rest of the clip's details), sort by
  newest/oldest/camera/size/duration, starred filter, and a camera sidebar. The
  storage stat shows both free disk space and, when `max_storage_gb` is
  configured, how much of that quota is used — since a large amount of free disk
  space doesn't mean much if the configured quota is small.
- **Status tab** — a per-camera battery strip at the top (Normal/Low, color-coded;
  click a camera to see its history — when it went low, when it recovered, and how
  long each low period lasted — see [Low-Battery Alerts](#low-battery-alerts)
  above for the matching alert), Blink connection status, library stats,
  per-camera breakdown, a 7-day activity chart, and an **AI Analysis** card
  showing provider name, online/offline status, model, pending queue count, and
  suspicious-clip count.
- **AI tab** — AI provider configuration (with a Fetch Models picker for both the
  tier-1 and, when configured, tier-2 escalation model), connection health check,
  Camera Configurations panel (descriptions/custom prompts), AI Usage statistics,
  and a **Test Analysis** button that runs a full analysis on the most recently
  downloaded clip to confirm the AI backend is working end-to-end.
- **Models tab** — reference info on each AI provider's capabilities.
- **Live View tab** — watch a live stream from any camera on your Blink account,
  one camera at a time, directly in the browser — see [Live
  View](#live-view) below.
- **Vehicles tab** — protected-vehicle description, per-camera "sees the vehicle"
  flag, and a visual rectangle-or-freeform zone picker drawn over an actual recent
  frame from that camera, with a persisted reference snapshot — see
  **Vehicles Tab** above.
- **Biometrics tab** — enroll household members' faces (from a clip's frames or a
  single photo), approve/un-approve/rename/remove them, and the all-or-nothing
  suspicious-flag bypass this powers — see **Biometrics Tab** above.
- **Storage tab** — view and delete archived clips, run archiving on demand
  instead of waiting for the next poll cycle, and connect a Google Drive
  account to back clips up, including retrying any upload that failed — see
  [Storage Tab — Google Drive Backup](#storage-tab--google-drive-backup)
  below.
- **Automations tab** — ready-to-paste HA automation YAML snippets, plus a
  **Notification Channels** panel to test-send an email/Discord message/mobile
  push before enabling that channel for real.
- **Video.js player** — in-browser streaming with play/pause, seek, fullscreen, PiP,
  loop, autoplay-next, theater mode, and playback-rate selection.
- **Per-clip AI Analysis panel** — each clip modal includes a collapsible 🤖 **AI
  Analysis** section showing the suspicion badge, confidence score, AI summary, and
  model/timestamp. An **Analyze Now** button triggers analysis on demand; a
  **Re-analyze** button re-runs it. The full raw AI response is available via a
  **Full response** toggle.
- **AI Usage tab** — per-provider token usage statistics including prompt tokens,
  completion tokens, per-model breakdown, and estimated API cost (for Anthropic and
  OpenAI, priced per model). Moondream Cloud shows request count with a billing note.
  When `ai_escalation_enabled` is on, escalated analyses appear as their own row
  and count toward a separate "Escalations" total.
  A **Clear Stats** button resets these counters — handy after switching providers
  so old usage doesn't keep piling into the total — without touching per-clip
  analysis history.
- **Bulk select** — star, delete, export multiple clips as a ZIP archive,
  **analyze** them all with AI at once (behind a confirmation dialog stating the
  exact count, since analysis spends real provider tokens; capped at 25 clips per
  batch and processed one at a time rather than all in parallel), or **upload**
  them to Google Drive (once connected — see the Storage tab) via a folder
  picker that lets you choose an existing folder or create a new one.
- **Tag management** — add/remove freeform tags per clip; filter the library by tag.
- **Browser notifications** — opt-in desktop notifications when new clips arrive.
- **Dark/Light theme** — automatically follows the OS/browser preference; a ☀/🌙
  toggle in the nav bar lets you override. Preference is saved across page loads.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play / pause |
| `← →` | Seek ±10 s |
| `↑ ↓` | Previous / next clip |
| `F` | Toggle fullscreen |
| `M` | Toggle mute |
| `L` | Toggle loop |
| `Esc` | Close player or help overlay |
| `?` | Show / hide keyboard shortcut help |

---

## Live View

Watch a live stream from any camera on your Blink account, one camera at a
time, directly in the web UI — no separate app or RTSP client needed.

### How it works

- Open the **Live View** tab and pick a camera; the stream starts within a
  few seconds.
- Uses blinkpy's live-view session API against your existing, already
  logged-in Blink account — no second sign-in, no extra port, and nothing
  about the normal clip-download/AI-analysis pipeline is affected.
- Only one camera streams at a time across the whole install — picking a
  different camera (from any browser tab) stops the previous stream first.
  A second tab watching the *same* camera joins the existing stream instead
  of starting a new one.
- A session stops automatically after a period of inactivity, always after
  a 30-minute safety cap, and immediately when you navigate away from the
  tab — there's also a **Stop** button for stopping it manually at any
  time.

### Limitations

- Some latency is inherent to how Blink's live-view protocol works —
  expect a real but modest delay versus true real time, not a
  video-call-grade connection.
- There is currently no way to save/record a clip from a live view session
  — it's watch-only. Clips are still captured normally by the regular
  motion-triggered download pipeline in parallel.
- A camera generally can't stream live view and produce a motion-triggered
  recording at the exact same instant — this is a limitation of the Blink
  camera hardware/cloud service itself (the same is true of the official
  Blink app), not something this add-on can control.

---

## Security Feed

A grid of near-live snapshot tiles, one per camera, that refresh on their
own on a timer — a lighter-weight alternative to Live View for keeping an
eye on multiple cameras at once rather than watching one at a time.

### How it works

- Open the **Security Feed** tab to see every camera as a tile, refreshing
  automatically.
- Each tile shows whatever image the add-on's own poll cycle most recently
  cached — opening this tab (or leaving it open) doesn't ask Blink for
  anything extra beyond what the add-on already does, and it stops making
  any requests the moment you navigate away.
- The **Customize** panel (collapsed by default) lets you pick which
  cameras to show, how many tiles per row (1–3 — beyond 3 a tile shrinks
  too small to make out what it's showing), and how often tiles refresh
  (5–300 seconds). Changes apply once you click **Save**.
- The grid uses however much width is available, so it fills more of the
  screen if your browser or the Home Assistant sidebar gives it more room.

### Limitations

- Tiles are snapshots, not a live video stream — for an actual moving
  picture from one camera, use the **Live View** tab instead.
- A tile only updates as often as both its configured refresh interval
  *and* the add-on's own Blink poll interval allow — lowering the refresh
  interval here doesn't make the add-on poll Blink any faster.

---

## Storage Tab — Google Drive Backup

The Storage tab has two parts: an **Archived Clips** list (view/delete clips
`archive_enabled` has already compressed into ZIP files), and a **Google
Drive Backup** card that connects a Google account and uploads clips there
as an extra copy.

Archived clips are grouped by ZIP file and grouped by camera inside each
expanded archive. The camera and from/to date filters at the top of the
Archived Clips section apply to both the archive list and the clips inside an
expanded archive, including archives containing multiple cameras. Selecting
only the left date includes that entire calendar day; selecting both dates
includes the full inclusive range. Expanded archives load 50 clips per page and
keep a fixed-height scroll area so moving between pages does not shift the
surrounding Storage page.

The steps below are also available in-app — click the ⓘ button next to
**Google Drive Setup** on the Storage tab for the same walkthrough without
leaving the page.

### Why a one-time Google Cloud Console step is needed

Google requires every application talking to its APIs to be registered as an
OAuth client. This add-on can't ship a single shared client the way Home
Assistant Core's own built-in integrations sometimes do — Core is a large,
Google-verified project; a single add-on's shared client would mean every
installer sharing one API quota bucket, and a client secret that's public in
this repo's source. Instead, **you register your own free OAuth client** in
your own Google account, the same pattern used by many self-hosted tools
(e.g. rclone's Google Drive backend). It takes a few minutes and is only
needed once.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create
   a new project (or reuse an existing one).
2. Under **APIs & Services → Library**, enable the **Google Drive API**.
3. Under **APIs & Services → OAuth consent screen**, configure it (External
   is fine for personal use) and add yourself as a test user.
4. Under **APIs & Services → Credentials → Create Credentials → OAuth client
   ID**, choose application type **"TVs and Limited Input devices"**. This is
   the type that supports the code-based device sign-in the Storage tab uses
   — there is no redirect URL to configure.
5. Copy the generated **Client ID** and **Client Secret**.
6. **Important:** under **OAuth consent screen**, move the app from
   "Testing" to "In production" once you've confirmed it works. Google
   commonly expires refresh tokens after about a week for apps left in
   Testing status, which would otherwise force you to reconnect weekly. For
   the narrow `drive.file` scope this add-on requests,
   moving to production doesn't require Google's full verification review.

### Connecting from the Storage tab

Everything past that one-time registration happens in the app itself:

1. Open the **Storage** tab and paste the **Client ID**/**Client Secret**
   from step 5 above into the **Google Drive Setup** card, choose a backup
   policy, and click **Save Setup**.
2. Click **Connect Google Drive**. The card shows a short code and a URL
   (`google.com/device`) — open that URL on any device, sign in, and enter
   the code. The Storage tab polls automatically and updates once you've
   finished.
3. Choose a folder for your backups — browse your existing Drive folders or
   create a new one — or use **Change Folder** later to switch.

Backup policy (also settable from the same card):

- **Archived clips only** (default) — clips `archive_enabled` compresses
  into a ZIP are automatically queued for backup once connected.
- **All clips** — every downloaded clip is queued for backup, not just
  archived ones.

A **Back Up Existing Clips Now** button queues everything already eligible
under the current policy — without it, connecting Drive for the first time
would only cover clips going forward. It also retries anything that
previously failed (see below), not just clips that were never queued.

### Failed uploads

An individual clip can fail to upload (a dropped connection, a Drive API
error, a full quota) without affecting the rest of the queue. When at least
one clip has failed, a **Failed Uploads** list appears on the Google Drive
card showing each one's camera and error message, with a **Retry** button
per clip and a **Retry All Failed** button for all of them at once — both
just reset the clip back to pending, so it's picked up by the same upload
queue as anything else. **Back Up Existing Clips Now** above also retries
every failed clip as part of its normal sweep.

### What this does and doesn't do

- Uploads are additive: a successful Drive backup never causes early local
  deletion. Local retention/archiving behavior is unchanged.
- Deleting a clip (from the Library or the Storage tab's archived list) also
  moves its Drive copy to Drive's own trash (recoverable there for about 30
  days), not a permanent delete.
- Deleting an *archived* clip removes its database record and Drive copy,
  but not the bytes inside its shared monthly ZIP file (a ZIP can't have a
  single member removed without rewriting the whole archive) — local disk
  space for that ZIP isn't reclaimed by deleting one clip from it.
- The folder browser only ever lists and creates **folders** — it never
  shows or touches files of any other kind, so there's nothing here that
  could interact with unrelated files elsewhere in your Drive. The clip
  list itself is always this add-on's own upload record, not a live scan of
  the folder's contents — a file you drop into that folder yourself via
  Drive's own app stays completely invisible to this add-on.
- There's no download/restore-from-Drive feature — clips already live
  locally; Drive is a backup destination, not a second source to sync from.

---

## Home Assistant Integration

### Sensor

After every poll the add-on updates a virtual sensor:

- **entity_id**: `sensor.blink_downloader_status`
- **state**: total clips downloaded (lifetime)
- **attributes**: `total_downloaded`, `session_downloads`, `used_mb`, `free_gb`,
  `last_download`

### Events

For every downloaded clip the add-on fires the event `blink_clip_downloaded`:

```json
{
  "clip_id": "99001",
  "camera": "Front Door",
  "path": "/share/blink-clips/Front_Door/2024-06-15/Front_Door_20240615_083000.mp4",
  "timestamp": "2024-06-15T08:30:00+00:00",
  "size_bytes": 1048576,
  "duration": 5,
  "source": "pir"
}
```

Example automation — TTS alert when a doorbell clip arrives:

```yaml
alias: Announce doorbell clip
trigger:
  - platform: event
    event_type: blink_clip_downloaded
    event_data:
      camera: Doorbell
action:
  - service: tts.speak
    data:
      message: "Doorbell clip just downloaded"
```

---

## Manual Trigger

Touch the file `/data/trigger_download` to force an immediate poll without waiting
for the next scheduled interval:

```bash
touch /data/trigger_download
```

The add-on checks for this file every 10 seconds and deletes it after triggering.
You can also click **⬇ Sync** in the web UI Library tab.

---

## Accessing Clips Outside the Web UI

Downloaded clips are saved under the `share` folder, accessible via:

- **Media Browser** — Home Assistant UI → Settings → Media.
- **Samba share** — if the Samba add-on is installed, browse to `\\ha\share\blink-clips`.
- **SSH** — `/share/blink-clips/` inside the HA OS container.

---

## Data Files

| Path | Description |
|------|-------------|
| `/data/auth_credentials.json` | Cached Blink auth tokens (do not edit) |
| `/data/blink_hardware_id.txt` | Stable device ID presented to Blink during login (do not edit) |
| `/data/downloaded_clips.json` | Tracker of downloaded clip IDs |
| `/data/clip_manifest.json` | Newline-delimited JSON log of all downloads |
| `/data/postgresql/17/main/` | Bundled PostgreSQL data directory powering the web UI and AI analysis |
| `/data/stats.json` | Latest statistics snapshot |
| `/data/last_digest.json` | Timestamp of the last daily digest |
| `/data/two_fa_code.txt` | Write your 2FA code here when prompted |
| `/data/trigger_download` | Touch to force an immediate poll |
| `/data/camera_configs.json` | Per-camera descriptions, custom prompts, car-camera flags, and car zones set via the web UI |
| `/data/vehicle_settings.json` | Protected-vehicle description set via the Vehicles tab |
| `/data/vehicle_zone_snapshots/` | One reference frame (JPEG) per camera with a saved car zone — the exact image the zone was drawn on, so the Vehicles tab's preview never silently shows a different, newer frame |
| `/data/moondream_packages/` | The `moondream` Python package itself, installed here via the AI tab's **Install** button so it persists across restarts |
| `/data/model_cache/` | Downloaded computer-vision models (YOLO, Depth Anything V2, SAM2, facenet-pytorch) — see **Disk Space** above |
| `/data/google_drive_settings.json` | Google OAuth client ID/secret and backup policy, set via the Storage tab |
| `/data/google_drive_credentials.json` | Cached Google Drive OAuth token and selected backup folder (do not edit) |

> All `/data/` files are stored inside the add-on's private data directory and are
> automatically removed by the supervisor when the add-on is uninstalled.

---

## Troubleshooting

**Clips are not being downloaded**
- Check the add-on log for authentication errors.
- Verify your Blink credentials are correct.
- Ensure `/share/` is writable (`share:rw` is set in the add-on's volume mapping).

**2FA loop keeps triggering**
- Your refresh token may have expired. Delete `/data/auth_credentials.json` and restart.

**Storage keeps filling up**
- Lower `retention_days` or `max_storage_gb`.
- Enable `archive_enabled` to compress old clips into ZIP files.
- Add cameras to `camera_filter` to limit which cameras are archived.

**Clips are missing after motion events**
- Increase `post_motion_delay` — Blink can take up to 60 s to upload a clip.
- Enable `watch_ha_events` and ensure your Blink motion sensors are in HA.

**Web UI shows blank / API errors via HA sidebar**
- The add-on uses HA ingress, which automatically proxies the panel URL. No manual
  port forwarding is needed for the sidebar panel.
- If using direct access (`http://<ha-ip>:8099`), ensure port `8099/tcp` is exposed.

**Clips from only one camera are downloading**
- Check `camera_filter` — names must match exactly as shown in the Blink app.

**`/bin/sh: can't open /init: Permission denied` in the add-on log**
- This was a known issue fixed in v2.1.0.  Update the add-on and rebuild/reinstall
  to get the corrected S6-overlay v3 service definition.  If you are already on
  v2.1.0 and still see it, check that the add-on was fully reinstalled (not just
  restarted) so the new container image is in use.

**AI analysis is not running**
- Confirm `ai_analysis_enabled: true` and `enable_library_db: true` in the add-on
  configuration.
- Check the AI tab in the web UI for the provider connection status. Use the
  **Test Analysis** button to confirm end-to-end connectivity.
- If using Ollama, verify `ollama_url` is reachable from the HA host and that the
  model named in `ollama_model` is installed (`ollama pull <model>`).

**AI responses contain technical terms like "bounding box" or "normalized"**
- Update to v3.0.1 or later. Prior versions did not suppress internal spatial data
  from user-visible descriptions.

**Moondream Local installs successfully but health-checks as unavailable / analysis
never returns results**
- On-device Moondream inference requires an NVIDIA CUDA or Apple Silicon GPU (the
  `moondream` package no longer supports pure-CPU inference). This applies on both
  amd64 and aarch64 — a Jetson-class aarch64 board with an NVIDIA GPU can work, but a
  typical Raspberry Pi or other GPU-less aarch64 host cannot, the same as a GPU-less
  amd64 host. Check the add-on log for "Failed to load Moondream local model" — if
  the host has no such GPU passed
  through to the container, this is expected; use `moondream_cloud` or `ollama`
  instead.

**AI suspicious-activity alerts are not being sent**
- Check `ai_min_confidence` — if set above 0.0, low-confidence results are stored
  but alerts are suppressed.
- For Discord, verify `discord_webhook_url` is correct and `discord_enabled: true`.
- For email, confirm SMTP credentials and that `smtp_recipients` is populated.
- For mobile app, ensure `mobile_app_target` matches your HA mobile app entity name
  exactly (e.g. `mobile_app_my_phone`).

**Anomaly detection is flagging everything as suspicious**
- The anomaly baseline requires approximately 30 events per camera to activate. On a
  new install, allow time for normal activity to be recorded before the scores
  become meaningful.

**AI confidence is always 0% in Discord embeds (Moondream)**
- Update to v2.8.8 or later, which derives a non-zero confidence via keyword matching
  when the model returns 0.0.

**Local-storage clips from the Sync Module USB drive are not downloading**
- Enable `download_local_storage: true` in the add-on configuration.
- Verify that the USB drive is detected by the Sync Module (check the Blink app).
  The add-on fetches the clip list via the Blink cloud API, so internet access is
  required even for "local" storage clips.
