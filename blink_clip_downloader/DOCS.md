# Blink Clip Downloader — Documentation

## Overview

This add-on continuously polls the Blink API for new camera clips and saves them to
your local storage (under `/share/blink-clips` by default). It includes a built-in
web library UI, SQLite clip database, event-driven instant download, daily digest
notifications, ZIP archiving, and full Home Assistant integration.

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
| `download_thumbnails` | `false` | Save a JPEG thumbnail alongside each clip |
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
| `enable_library_db` | `true` | Store clip metadata in a SQLite database |

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

### Logging

| Option | Default | Description |
|--------|---------|-------------|
| `log_level` | `info` | `debug`, `info`, `warning`, or `error` |

---

## Web Library UI

The built-in web interface lets you browse, search, play, star, tag, and delete clips
from any browser without leaving Home Assistant.

### Features

- **Library tab** — scrollable grid with thumbnails, camera/date/source/tag filters,
  sort by newest/oldest/camera/size/duration, starred filter, and a camera sidebar.
- **Status tab** — Blink connection status, library stats, per-camera breakdown, and
  a 7-day activity chart.
- **Automations tab** — ready-to-paste HA automation YAML snippets.
- **Video.js player** — in-browser streaming with play/pause, seek, fullscreen, PiP,
  loop, autoplay-next, theater mode, and playback-rate selection.
- **Bulk select** — star, delete, or export multiple clips as a ZIP archive.
- **Tag management** — add/remove freeform tags per clip; filter the library by tag.
- **Browser notifications** — opt-in desktop notifications when new clips arrive.

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
| `/data/clip_library.db` | SQLite database powering the web UI |
| `/data/stats.json` | Latest statistics snapshot |
| `/data/last_digest.json` | Timestamp of the last daily digest |
| `/data/two_fa_code.txt` | Write your 2FA code here when prompted |
| `/data/trigger_download` | Touch to force an immediate poll |

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
