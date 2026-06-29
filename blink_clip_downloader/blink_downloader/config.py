"""Configuration loader and validator."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

OPTIONS_FILE = Path("/data/options.json")
_VALID_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})
_DEFAULT_SUSPICIOUS_KEYWORDS = [
    "suspicious",
    "break-in",
    "theft",
    "vandalism",
    "intruder",
    "trespassing",
]


@dataclass
class AppConfig:  # pylint: disable=too-many-instance-attributes
    """Home Assistant add-on runtime options loaded from options.json."""

    # --- Required ---
    username: str
    password: str

    # --- Storage ---
    download_path: Path = field(default_factory=lambda: Path("/share/blink-clips"))
    organize_by_camera: bool = True
    organize_by_date: bool = True
    filename_format: str = "{camera}_{timestamp}"

    # --- Polling ---
    poll_interval: int = 300
    max_clips_per_poll: int = 50

    # --- Retention & quota ---
    retention_days: int = 30
    max_storage_gb: float = 10.0

    # --- Filtering ---
    camera_filter: list[str] = field(default_factory=list)
    motion_only: bool = False
    time_window_start: str = ""
    time_window_end: str = ""

    # --- Download options ---
    download_thumbnails: bool = False
    concurrent_downloads: int = 3
    retry_attempts: int = 3
    retry_delay: float = 5.0

    # --- HA integration ---
    notify_ha: bool = True
    ha_notification_title: str = "Blink Clip Downloaded"

    # --- Extra features ---
    webhook_url: str = ""
    create_clip_manifest: bool = True

    # --- Library database ---
    enable_library_db: bool = True

    # --- Media server ---
    enable_media_server: bool = True
    media_server_port: int = 8099

    # --- Instant download on HA motion events ---
    watch_ha_events: bool = True
    fast_poll_duration: int = 120
    fast_poll_interval: int = 15
    event_cameras: list[str] = field(default_factory=list)
    post_motion_delay: int = 30

    # --- Clip filtering ---
    min_clip_duration: int = 0

    # --- Daily digest ---
    digest_enabled: bool = True
    digest_time: str = "08:00"

    # --- ZIP archiving ---
    archive_enabled: bool = False
    archive_after_days: int = 60

    # --- Sync Module local storage (USB drive clips) ---
    # When True, each poll cycle also downloads clips stored on the physical
    # USB drive attached to the Blink Sync Module.  Blink's API does not
    # support direct LAN access; clips are temporarily uploaded to the Blink
    # cloud then fetched from there, so an internet connection is required.
    download_local_storage: bool = False

    # --- AI Video Analysis ---
    ai_analysis_enabled: bool = False
    ai_provider: str = "ollama"  # "ollama" | "ollama_cloud" | "moondream_cloud" | "moondream_local" | "anthropic" | "openai"
    ollama_url: str = ""
    ollama_model: str = ""
    ollama_cloud_api_key: str = ""
    moondream_api_key: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    ai_prompt: str = (
        "You are a security camera analyst. Review this motion-triggered clip and determine "
        "if anything suspicious is happening.\n\n"
        "Step 1 — Identify what triggered the motion:\n"
        "- Person, animal, passing vehicle, shadow/reflection, wind/leaves, or environmental motion.\n"
        "- A car or shadow moving on a public road is NOT suspicious — distinguish carefully.\n\n"
        "Step 2 — If a person is present, observe:\n"
        "- Where they are standing or moving, and in which direction.\n"
        "- How close they are to any vehicles, doors, windows, or other property.\n"
        "- What they appear to be doing (walking past, lingering, reaching, running, etc.).\n"
        "- Lighting and visibility conditions.\n\n"
        "Step 3 — Apply these rules:\n"
        "SUSPICIOUS (suspicious=true) — ALL must apply:\n"
        "  • Person is physically touching, pressing against, or trying to open a vehicle, door, or window.\n"
        "  • Person is crouching beside or hiding next to property.\n"
        "  • Person runs away immediately after contacting property.\n"
        "NOT SUSPICIOUS (suspicious=false):\n"
        "  • Person walking along a public road, sidewalk, or across the scene without stopping.\n"
        "  • Person standing more than 3 feet from protected assets.\n"
        "  • Vehicle, bicycle, or shadow passing on the street.\n"
        "  • Animal, insect, or environmental motion (wind, leaves, lights).\n"
        "  • Delivery driver at the front door (under 60 seconds).\n\n"
        "Confidence = how clearly you can see the scene "
        "(0.1 = very dark or blurry, 1.0 = crystal-clear daylight).\n\n"
        "Respond ONLY with this exact JSON — no other text:\n"
        '{"suspicious": true/false, "confidence": 0.1-1.0, '
        '"description": "2-3 sentence plain-English description of what you see and why it is or is not suspicious"}'
    )
    ai_car_description: str = ""
    ai_max_frames: int = 5
    ai_frame_interval: float = 2.0
    ai_suspicious_keywords: list[str] = field(
        default_factory=lambda: list(_DEFAULT_SUSPICIOUS_KEYWORDS)
    )
    ai_schedule_start: str = ""
    ai_schedule_end: str = ""
    ai_batch_size: int = 10
    ai_check_interval: int = 60
    ai_min_confidence: float = 0.0
    ai_camera_prompts: list[dict] = field(default_factory=list)
    ai_camera_descriptions: list[dict] = field(default_factory=list)
    # Frame extraction strategy:
    #   "smart"      – oversample 2x then pick entry/peak-motion/exit frames (default)
    #   "sequential" – analyse each frame individually, return most alarming result
    #   "uniform"    – legacy: extract exactly ai_max_frames at fixed intervals
    ai_frame_strategy: str = "smart"
    # List of camera names that have the protected vehicle in view.
    # When non-empty, car-protection distance rules are only injected into prompts
    # for cameras in this list.  Leave empty to apply to all cameras (default).
    ai_car_cameras: list[str] = field(default_factory=list)

    # --- Extended Notifications (AI alerts) ---
    mobile_app_target: str = ""
    mobile_app_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_recipients: list[str] = field(default_factory=list)
    smtp_sender: str = ""
    smtp_enabled: bool = False
    discord_webhook_url: str = ""
    discord_enabled: bool = False

    # --- Logging ---
    log_level: str = "info"

    # --- Runtime (injected, not from options.json) ---
    supervisor_token: str = field(
        default_factory=lambda: os.environ.get("SUPERVISOR_TOKEN", "")
    )
    two_fa_timeout: float = 600.0
    # Set to a non-empty string when load_config() fails; the app starts the
    # web server in error-display mode rather than attempting Blink auth.
    startup_error: str = ""


def load_config(options_path: Path = OPTIONS_FILE) -> AppConfig:
    """Load and validate configuration from *options_path*."""
    if not options_path.exists():
        raise FileNotFoundError(f"Options file not found: {options_path}")

    with options_path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    return _parse_config(data)


def _parse_config(data: dict) -> AppConfig:
    """Parse a raw options dict into a validated :class:`AppConfig`."""
    username = str(data.get("username", "")).strip()
    if not username:
        raise ValueError("username is required and cannot be empty")

    password = str(data.get("password", "")).strip()
    if not password:
        raise ValueError("password is required and cannot be empty")

    poll_interval = int(data.get("poll_interval", 300))
    if not 30 <= poll_interval <= 3600:
        raise ValueError(
            f"poll_interval must be between 30 and 3600 seconds, got {poll_interval}"
        )

    retention_days = int(data.get("retention_days", 30))
    if not 0 <= retention_days <= 365:
        raise ValueError(
            f"retention_days must be between 0 and 365, got {retention_days}"
        )

    max_clips = int(data.get("max_clips_per_poll", 50))
    if not 1 <= max_clips <= 500:
        raise ValueError(
            f"max_clips_per_poll must be between 1 and 500, got {max_clips}"
        )

    log_level = str(data.get("log_level", "info")).lower()
    if log_level not in _VALID_LOG_LEVELS:
        _LOGGER.warning("Unknown log_level %r, falling back to 'info'", log_level)
        log_level = "info"

    camera_filter = [
        c.strip()
        for c in data.get("camera_filter", [])
        if isinstance(c, str) and c.strip()
    ]

    return AppConfig(
        username=username,
        password=password,
        download_path=Path(str(data.get("download_path", "/share/blink-clips"))),
        organize_by_camera=bool(data.get("organize_by_camera", True)),
        organize_by_date=bool(data.get("organize_by_date", True)),
        filename_format=str(data.get("filename_format", "{camera}_{timestamp}")),
        poll_interval=poll_interval,
        max_clips_per_poll=max_clips,
        retention_days=retention_days,
        max_storage_gb=float(data.get("max_storage_gb", 10.0)),
        camera_filter=camera_filter,
        motion_only=bool(data.get("motion_only", False)),
        time_window_start=str(data.get("time_window_start", "") or ""),
        time_window_end=str(data.get("time_window_end", "") or ""),
        download_thumbnails=bool(data.get("download_thumbnails", False)),
        concurrent_downloads=max(1, min(10, int(data.get("concurrent_downloads", 3)))),
        retry_attempts=max(1, min(10, int(data.get("retry_attempts", 3)))),
        retry_delay=max(0.0, float(data.get("retry_delay", 5.0))),
        notify_ha=bool(data.get("notify_ha", True)),
        ha_notification_title=str(
            data.get("ha_notification_title", "Blink Clip Downloaded")
        ),
        webhook_url=str(data.get("webhook_url", "") or ""),
        create_clip_manifest=bool(data.get("create_clip_manifest", True)),
        enable_library_db=bool(data.get("enable_library_db", True)),
        enable_media_server=bool(data.get("enable_media_server", True)),
        media_server_port=max(
            1024, min(65535, int(data.get("media_server_port", 8099)))
        ),
        watch_ha_events=bool(data.get("watch_ha_events", True)),
        fast_poll_duration=max(10, int(data.get("fast_poll_duration", 120))),
        fast_poll_interval=max(5, min(60, int(data.get("fast_poll_interval", 15)))),
        event_cameras=[
            c.strip()
            for c in data.get("event_cameras", [])
            if isinstance(c, str) and c.strip()
        ],
        post_motion_delay=max(5, min(300, int(data.get("post_motion_delay", 30)))),
        min_clip_duration=max(0, int(data.get("min_clip_duration", 0))),
        digest_enabled=bool(data.get("digest_enabled", True)),
        digest_time=str(data.get("digest_time", "08:00")),
        archive_enabled=bool(data.get("archive_enabled", False)),
        archive_after_days=max(1, int(data.get("archive_after_days", 60))),
        download_local_storage=bool(data.get("download_local_storage", False)),
        # AI Video Analysis
        ai_analysis_enabled=bool(data.get("ai_analysis_enabled", False)),
        ai_provider=str(data.get("ai_provider", "ollama") or "ollama").strip().lower(),
        ollama_url=str(data.get("ollama_url", "") or "").strip().rstrip("/"),
        ollama_model=str(data.get("ollama_model", "") or "").strip(),
        ollama_cloud_api_key=str(data.get("ollama_cloud_api_key", "") or "").strip(),
        moondream_api_key=str(data.get("moondream_api_key", "") or "").strip(),
        anthropic_api_key=str(data.get("anthropic_api_key", "") or "").strip(),
        anthropic_model=str(data.get("anthropic_model", "") or "").strip()
        or "claude-haiku-4-5",
        openai_api_key=str(data.get("openai_api_key", "") or "").strip(),
        openai_model=str(data.get("openai_model", "") or "").strip() or "gpt-4o-mini",
        ai_prompt=str(data.get("ai_prompt", "") or "").strip() or AppConfig.ai_prompt,
        ai_car_description=str(data.get("ai_car_description", "") or "").strip(),
        ai_max_frames=max(1, min(100, int(data.get("ai_max_frames", 5)))),
        ai_frame_interval=max(
            0.5, min(30.0, float(data.get("ai_frame_interval", 2.0)))
        ),
        ai_suspicious_keywords=[
            k.strip()
            for k in data.get("ai_suspicious_keywords", [])
            if isinstance(k, str) and k.strip()
        ]
        or list(_DEFAULT_SUSPICIOUS_KEYWORDS),
        ai_schedule_start=str(data.get("ai_schedule_start", "") or "").strip(),
        ai_schedule_end=str(data.get("ai_schedule_end", "") or "").strip(),
        ai_batch_size=max(1, min(50, int(data.get("ai_batch_size", 10)))),
        ai_check_interval=max(10, min(3600, int(data.get("ai_check_interval", 60)))),
        ai_min_confidence=max(0.0, min(1.0, float(data.get("ai_min_confidence", 0.0)))),
        ai_camera_prompts=[
            {"camera": str(item["camera"]), "prompt": str(item["prompt"])}
            for item in data.get("ai_camera_prompts", [])
            if isinstance(item, dict) and item.get("camera") and item.get("prompt")
        ],
        ai_camera_descriptions=[
            {
                "camera": str(item["camera"]),
                "description": str(item.get("description", "")),
            }
            for item in data.get("ai_camera_descriptions", [])
            if isinstance(item, dict) and item.get("camera")
        ],
        ai_frame_strategy=str(data.get("ai_frame_strategy", "smart") or "smart")
        .strip()
        .lower()
        if str(data.get("ai_frame_strategy", "smart") or "smart").strip().lower()
        in {"smart", "sequential", "uniform"}
        else "smart",
        ai_car_cameras=[
            c.strip()
            for c in data.get("ai_car_cameras", [])
            if isinstance(c, str) and c.strip()
        ],
        # Extended notifications
        mobile_app_target=str(data.get("mobile_app_target", "") or "").strip(),
        mobile_app_enabled=bool(data.get("mobile_app_enabled", False)),
        smtp_host=str(data.get("smtp_host", "") or "").strip(),
        smtp_port=max(25, min(65535, int(data.get("smtp_port", 587)))),
        smtp_user=str(data.get("smtp_user", "") or "").strip(),
        smtp_password=str(data.get("smtp_password", "") or ""),
        smtp_recipients=[
            r.strip()
            for r in data.get("smtp_recipients", [])
            if isinstance(r, str) and r.strip()
        ],
        smtp_sender=str(data.get("smtp_sender", "") or "").strip(),
        smtp_enabled=bool(data.get("smtp_enabled", False)),
        discord_webhook_url=str(data.get("discord_webhook_url", "") or "").strip(),
        discord_enabled=bool(data.get("discord_enabled", False)),
        log_level=log_level,
    )
