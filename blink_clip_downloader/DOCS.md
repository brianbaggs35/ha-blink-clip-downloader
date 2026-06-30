# Blink Clip Downloader — Documentation

## Overview

This add-on continuously polls the Blink API for new camera clips and saves them to
your local storage (under `/share/blink-clips` by default). It includes a built-in
web library UI, SQLite clip database, event-driven instant download, daily digest
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
| `notify_ha` | `true` | Send a persistent HA notification when new clips arrive |
| `ha_notification_title` | `"Blink Clip Downloaded"` | Title for HA notifications |

### Extra Features

| Option | Default | Description |
|--------|---------|-------------|
| `webhook_url` | `""` | POST clip metadata to this URL after each download |
| `create_clip_manifest` | `true` | Append metadata to `/data/clip_manifest.json` |

### Clip Library Database

| Option | Default | Description |
|--------|---------|-------------|
| `enable_library_db` | `true` | Store clip metadata in a SQLite database. Required for the web UI and AI analysis. |

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
| `archive_after_days` | `60` | Clips older than N days are archived (1–365) |

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

Runs the Moondream 0.5B INT8 model directly on the host device. The model file (~430 MB)
is downloaded automatically the first time via the **Install** button in the AI tab.

> **Architecture note:** Moondream Local requires an x86_64 (amd64) host. On aarch64
> (Raspberry Pi), the AI tab shows an unsupported-architecture notice — use
> `moondream_cloud` or `ollama` instead.

#### Anthropic (Claude) — `anthropic`

Uses the Anthropic Claude API for high-quality vision analysis. Billed per token.

| Option | Default | Description |
|--------|---------|-------------|
| `anthropic_api_key` | `""` | API key from [console.anthropic.com](https://console.anthropic.com) |
| `anthropic_model` | `"claude-haiku-4-5"` | Claude model to use. Use **Fetch Models** in the web UI to browse available models and pricing. |

Claude models receive a dedicated system prompt that separates role/format instructions
from user content, improving JSON compliance. `claude-haiku-4-5` is the default as
the most cost-effective option.

#### OpenAI (GPT) — `openai`

Uses the OpenAI API for vision analysis. Billed per token. Any OpenAI vision model is
supported (GPT-4o, GPT-4.1, GPT-4-Turbo, and variants).

| Option | Default | Description |
|--------|---------|-------------|
| `openai_api_key` | `""` | API key from [platform.openai.com](https://platform.openai.com) |
| `openai_model` | `"gpt-4o-mini"` | GPT model to use. Use **Fetch Models** in the web UI to browse models with pricing. |

GPT models use `response_format: json_object` to guarantee valid JSON output, and
`"high"` image detail for better scene analysis. `gpt-4o-mini` is selected by default
as the most cost-effective option.

### Analysis Prompt & Behaviour

| Option | Default | Description |
|--------|---------|-------------|
| `ai_prompt` | _(see config.yaml)_ | Global prompt sent to the AI for each clip. Must request a JSON response with `"suspicious"`, `"confidence"`, and `"description"` keys. |
| `ai_car_description` | `""` | Description of a vehicle to protect (e.g. `"Silver Kia Forte, parked in the driveway"`). When set, the AI applies strict distance rules and flags anyone within ~2 feet of the vehicle as suspicious. |
| `ai_car_cameras` | `[]` | Camera names for which car-proximity rules apply. Leave empty to apply to all cameras. Cameras not listed focus only on their own description, preventing false positives on cameras that cannot see the car. |
| `ai_min_confidence` | `0.0` | Minimum confidence threshold (0.0–1.0) for sending suspicious-activity alerts. Clips are still analysed and stored; only alert dispatch is gated. Example: `0.3` to suppress low-confidence detections. |
| `ai_suspicious_keywords` | _(list)_ | Words that trigger a suspicious flag when found in an AI plain-text response (used as fallback when the AI does not return valid JSON). |

### Frame Extraction

| Option | Default | Description |
|--------|---------|-------------|
| `ai_max_frames` | `5` | Number of frames extracted per clip for analysis (1–100). More frames = better coverage but higher API cost. |
| `ai_frame_interval` | `2.0` | Seconds between frame extraction points (0.5–30). |
| `ai_frame_strategy` | `"smart"` | How frames are selected and sent to the AI (see below). |

#### Frame Strategies

| Value | Behaviour |
|-------|-----------|
| `"smart"` | (Default) Extracts 2× `ai_max_frames` candidates then uses inter-frame motion-diff (PIL) to pick the entry frame, peak-motion frame, and exit frame. Best accuracy for the same or fewer API calls. |
| `"sequential"` | Analyses each frame individually via separate AI calls and returns the most alarming result (suspicious > non-suspicious; higher confidence when tied). Works well when the AI performs better on single images than on batches. Works with all six providers. |
| `"uniform"` | Extracts exactly `ai_max_frames` at fixed time intervals (legacy behaviour). |

### Per-Camera Configuration

You can set a description and a custom prompt for each camera without editing YAML —
use the **Camera Configurations** section in the web UI **AI tab**. Changes are saved
to `/data/camera_configs.json` and take effect immediately without restarting the add-on.

You can also configure these in `config.yaml` for YAML-based setups:

| Option | Default | Description |
|--------|---------|-------------|
| `ai_camera_descriptions` | `[]` | Per-camera descriptions. Example: `[{camera: "Driveway", description: "Points at driveway; silver Kia Forte parked left"}]` |
| `ai_camera_prompts` | `[]` | Per-camera prompt overrides. Example: `[{camera: "Driveway", prompt: "Focus on vehicle proximity."}]` |

The **"Protected vehicle visible from this camera"** checkbox in the Camera Configurations
panel enables car-proximity rules for individual cameras without editing `ai_car_cameras`
in the add-on options.

> **Priority:** `camera_configs.json` (set via the web UI) is the primary source for
> descriptions, custom prompts, and car-camera flags. `ai_camera_descriptions` and
> `ai_camera_prompts` in `options.json` serve as fallbacks for cameras not yet
> configured in the web UI.

### Smart Security Brain (Anomaly Detection)

The add-on builds a behavioural baseline for each camera over time, recording per-camera
hourly event frequency and average clip duration in SQLite with every download. This
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
`/api/ai/analysis` endpoint so you can query and filter by it.

---

## AI Alerts (Extended Notifications)

When AI analysis flags a clip as suspicious it can notify you through three channels
in addition to HA persistent notifications.

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

### Discord

| Option | Default | Description |
|--------|---------|-------------|
| `discord_enabled` | `false` | Enable Discord alerts for suspicious clips |
| `discord_webhook_url` | `""` | Discord webhook URL to post suspicious-clip alerts to |

---

## Web Library UI

The built-in web interface lets you browse, search, play, star, tag, and delete clips
from any browser without leaving Home Assistant.

### Features

- **Library tab** — scrollable grid with thumbnails, camera/date/source/tag filters,
  sort by newest/oldest/camera/size/duration, starred filter, and a camera sidebar.
- **Status tab** — Blink connection status, library stats, per-camera breakdown, a
  7-day activity chart, and an **AI Analysis** card showing provider name,
  online/offline status, model, pending queue count, and suspicious-clip count.
- **AI tab** — AI provider configuration, connection health check, Camera
  Configurations panel, AI Usage statistics, and a **Test Analysis** button that runs
  a full analysis on the most recently downloaded clip to confirm the AI backend is
  working end-to-end.
- **Automations tab** — ready-to-paste HA automation YAML snippets.
- **Video.js player** — in-browser streaming with play/pause, seek, fullscreen, PiP,
  loop, autoplay-next, theater mode, and playback-rate selection.
- **Per-clip AI Analysis panel** — each clip modal includes a collapsible 🤖 **AI
  Analysis** section showing the suspicion badge, confidence score, AI summary, and
  model/timestamp. An **Analyze Now** button triggers analysis on demand; a
  **Re-analyze** button re-runs it. The full raw AI response is available via a
  **Full response** toggle.
- **AI Usage tab** — per-provider token usage statistics including prompt tokens,
  completion tokens, per-model breakdown, and estimated API cost (for Anthropic and
  OpenAI). Moondream Cloud shows request count with a billing note.
- **Camera Configurations panel** (AI tab) — set per-camera descriptions, custom
  prompts, and the "Protected vehicle" checkbox without editing YAML. Changes apply
  immediately without restarting.
- **Bulk select** — star, delete, or export multiple clips as a ZIP archive.
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
| `/data/clip_library.db` | SQLite database powering the web UI and AI analysis |
| `/data/stats.json` | Latest statistics snapshot |
| `/data/last_digest.json` | Timestamp of the last daily digest |
| `/data/two_fa_code.txt` | Write your 2FA code here when prompted |
| `/data/trigger_download` | Touch to force an immediate poll |
| `/data/camera_configs.json` | Per-camera descriptions, custom prompts, and car-camera flags set via the web UI |
| `/data/moondream_packages/` | Moondream Local model files (downloaded on first use; persists across restarts) |

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

**Moondream Local fails to install on Raspberry Pi**
- Moondream Local (`moondream_local`) only supports x86_64 (amd64) hosts. Use
  `moondream_cloud` or `ollama` instead on aarch64 devices.

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
