"""Configuration loader and validator."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

OPTIONS_FILE = Path("/data/options.json")
_VALID_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})
_VALID_AI_PROVIDERS = frozenset(
    {
        "ollama",
        "ollama_cloud",
        "moondream_cloud",
        "moondream_local",
        "anthropic",
        "openai",
    }
)
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
    # Required for the Vehicles tab's car-zone picker; Biometrics enrollment
    # extracts its own frames on demand but its clip-browsing strip also
    # relies on thumbnails. See config.yaml's own comment on this option.
    download_thumbnails: bool = True
    concurrent_downloads: int = 3
    retry_attempts: int = 3
    retry_delay: float = 5.0

    # --- HA integration ---
    notify_ha: bool = False
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

    # --- Google Drive backup ---
    # Backs up clips to Google Drive via OAuth device flow — HA ingress gives
    # this add-on no fixed, pre-registerable redirect URL, so a standard
    # redirect-based OAuth sign-in isn't viable here (see gdrive_client.py).
    # Everything else about this feature (OAuth client id/secret, connect/
    # disconnect, backup policy, folder choice) is configured entirely from
    # the Storage tab, not here — see google_drive_settings.json/
    # google_drive_credentials.json — these two are the only knobs that stay
    # config.yaml options, matching ai_batch_size/ai_check_interval's
    # precedent for background-queue tuning.
    gdrive_batch_size: int = 5
    gdrive_check_interval: int = 300

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
    # Model ID of a Moondream Cloud fine-tuned checkpoint to use for inference
    # instead of the base model (e.g. "moondream3-preview/abc123@50"). Set by
    # the AI tab's Fine-Tuning panel — see MoondreamFineTuneManager.get_model_id().
    moondream_finetune_model: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Deprecated as of 4.0.0, removed from config.yaml's options/schema in
    # 5.0.0 — use ai_escalation_provider="openai" and ai_escalation_model
    # instead, which support escalating to any provider, not just a second
    # OpenAI model. This field/dataclass attribute stays for one more major
    # version purely for the migration below: an existing install's
    # /data/options.json still has whatever value it was last saved with
    # regardless of what config.yaml currently declares, and _parse_config's
    # migration reads it directly from that raw dict — so upgrading to 5.0.0
    # still auto-promotes it to ai_escalation_provider/ai_escalation_model
    # rather than silently dropping it. Safe to delete this field entirely
    # once no realistic upgrade path still has it set.
    openai_escalation_model: str = ""
    # Two-tier escalation: when True, every clip is first analyzed with the
    # normal ai_provider/model (tier 1); only clips tier 1 flags as suspicious
    # are re-analyzed by ai_escalation_provider/model (tier 2) for a closer
    # second opinion, and the tier-2 verdict is what gets recorded. The
    # provider/model fields below are only consulted when this is True —
    # _resolve_ai_escalation() forces both back to "" when it's False, so
    # nothing downstream needs to check this flag separately.
    ai_escalation_enabled: bool = False
    # Tier-2 provider for two-tier escalation, only used when
    # ai_escalation_enabled is True. May be a DIFFERENT provider than tier 1
    # (e.g. tier 1 = openai, tier 2 = moondream_cloud) — reuses that
    # provider's own credential fields already configured above (no separate
    # API key needed).
    ai_escalation_provider: str = ""
    # Model ID to use for the ai_escalation_provider above. For moondream_cloud
    # this is a fine-tuned checkpoint ID (like moondream_finetune_model); the
    # base model is used if left empty. Not applicable to moondream_local,
    # which has no selectable model.
    ai_escalation_model: str = ""
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
        "SUSPICIOUS (suspicious=true) — ANY of these applies:\n"
        "  • Person is prying, forcing, kicking, shouldering, or otherwise tampering with a "
        "locked door, window, or vehicle — not simply opening or using it normally.\n"
        "  • Person repeatedly jiggles, pulls on, or manipulates a door handle or lock beyond "
        "a single normal key/fob/code use, or otherwise appears to be trying to force a lock.\n"
        "  • Person attempts entry through a window, side gate, or any opening other "
        "than the normal front entrance.\n"
        "  • Person touches, reaches for, covers, sprays, or otherwise tampers with a security "
        "camera itself.\n"
        "  • A vehicle makes physical contact with — bumps, scrapes, or sideswipes — another "
        "vehicle or parked property, even a minor low-speed contact, regardless of whether "
        "the other party stops or drives away afterward.\n"
        "  • Person scrapes, bangs into, kicks, or otherwise physically contacts a parked "
        "vehicle.\n"
        "  • Person is crouching beside or hiding next to property.\n"
        "  • Person repeatedly looks around, peers into windows or vehicles, or paces the "
        "property without any clear purpose (casing behavior) — favor flagging this as "
        "suspicious when in doubt, since this pattern is otherwise easy to miss.\n"
        "  • Person picks up mail or a delivered package and then leaves the property on "
        "foot or by vehicle instead of entering the home — a mail/package theft signature.\n"
        "  • Person runs away immediately after contacting property.\n"
        "  • An animal (e.g. a dog) jumps on, paws at, or urinates/defecates on a vehicle or "
        "other protected property — flag this even though no person is involved.\n"
        "  • Lawn equipment flings a rock, stick, or debris that visibly strikes a vehicle "
        "with force, or wind-blown debris (a trash can, lid, branch, or similar) visibly "
        "strikes, bounces off, dents, or scrapes a vehicle rather than merely resting "
        "against it — flag this even with no person at fault and even if unintentional; "
        "any event that visibly damages or risks damaging a protected asset is worth "
        "reporting.\n"
        "NOT SUSPICIOUS (suspicious=false):\n"
        "  • A person walking up to the front door, unlocking or opening it normally, "
        "and going inside — this is ordinary coming and going (a resident or expected "
        "visitor) and must NOT be flagged just because they touched or opened the door.\n"
        "  • A person opening the front door from inside and stepping out to leave — e.g. "
        "leaving for work, taking out trash or mail, or walking to a car — is exactly as "
        "routine as entering and must NOT be flagged simply for opening the door and "
        "walking away. Only flag it if the person then lingers, repeatedly looks around, "
        "or otherwise matches one of the SUSPICIOUS behaviors above (e.g. casing, running "
        "immediately after tampering) — opening a door and calmly walking off on its own "
        "is never enough.\n"
        "  • A person walking up to a mailbox or porch, picking up mail or a package, and "
        "going back inside the home — this is the resident retrieving their own delivery "
        "and is routine even though the camera cannot confirm who they are; judge this by "
        "the retrieve-then-enter pattern, not by identity.\n"
        "  • Person walking along a public road, sidewalk, or across the scene without stopping.\n"
        "  • Person standing more than 3 feet from protected assets.\n"
        "  • Vehicle, bicycle, or shadow passing on the street.\n"
        "  • Animal, insect, or environmental motion (wind, leaves, lights).\n"
        "  • Lawn care equipment (mower, trimmer, blower) being operated near a vehicle by a "
        "neighbor or landscaper, with no visible impact on the vehicle — routine yard "
        "maintenance, not suspicious.\n"
        "  • Wind-blown debris — a trash can, lid, or branch — merely coming to rest against "
        "or blowing near a vehicle or property, with no person involved and no visible "
        "impact.\n"
        "  • Delivery driver at the front door (under 60 seconds).\n"
        "  • ANY of the above lawn-equipment or wind-blown-debris scenarios where the object "
        "visibly strikes, bounces off, dents, or scrapes the vehicle rather than merely "
        "resting near or against it is a DIFFERENT case — see SUSPICIOUS above. Damage or "
        "risk of damage to a protected asset is always worth reporting, even with no person "
        "at fault and even if unintentional.\n\n"
        "Confidence = how certain you are in the suspicious/not-suspicious verdict above, "
        "based on how clearly the BEHAVIOR itself is visible — NOT how bright or sharp "
        "the video looks. A dim or distant clip that still clearly shows tampering "
        "deserves high confidence; a bright, clear clip of ordinary activity is not "
        "suspicious regardless of image quality "
        "(0.1 = you can barely tell what is happening, 1.0 = the behavior is unambiguous). "
        "When genuinely uncertain whether behavior is suspicious (e.g. it could be casing "
        "or could be innocent loitering), prefer a lower-confidence suspicious=true over "
        "suppressing it entirely — occasional false positives are acceptable, but missing "
        "real suspicious activity is not.\n\n"
        "Respond ONLY with this exact JSON — no other text:\n"
        '{"suspicious": true/false, "confidence": 0.1-1.0, '
        '"description": "one short plain-English sentence naming the notable person, '
        "vehicle, or animal and what they are doing (e.g. 'A person is walking past the "
        "car' or 'A person is standing very close to the car and appears to be looking "
        "inside it') — omit static background scenery like parked vehicles that aren't "
        'involved, weather, foliage, or utility poles/power lines"}'
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
    # Below 0.5, the AI provider itself was never instructed to call something
    # suspicious (see the confidence-floor rule in analyzer._build_prompt) —
    # a lower default let low-confidence hedges ("maybe suspicious", never a
    # real detection) spam notifications. 0.5 aligns the alert gate with the
    # prompt's own stated floor for a genuine suspicious verdict.
    ai_min_confidence: float = 0.5
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
    # Debugging/prompt-tuning aid: store each clip's exact prompt text (not the
    # image frames) so it can be inspected via the "Prompt" button in the web
    # UI's AI panel. Off by default — prompts are long and this is opt-in,
    # not something every install needs kept in the database.
    ai_prompt_debug_enabled: bool = False

    # --- Computer-vision enhancement pipeline (heavy, optional) ---
    # WARNING: these features require substantially more CPU/RAM than the
    # rest of this add-on and download large ML models (100MB-800MB+ each,
    # cached under /data after first use) on first use. Only enable on
    # hardware with real spare capacity (a modern NUC/mini-PC, Raspberry Pi
    # 5 8GB, or better). Off by default; clip analysis behaves exactly as it
    # did before this pipeline existed with both of these left disabled. See
    # vision.py and DOCS.md.
    #
    # Frame preprocessing (CLAHE contrast + light denoising), object
    # detection/tracking (Ultralytics YOLO + ByteTrack), monocular depth
    # estimation (Depth Anything V2), and pixel-level contact segmentation
    # (SAM2) bundled behind a single toggle — depth/segmentation have
    # always required object detection to run at all, and enabling
    # detection without preprocessing (or vice versa) never provided real
    # standalone value, so five separate settings just multiplied the
    # untested on/off combinations without a matching benefit. Feeds a
    # code-computed OBJECT DETECTION/TRACKING/DEPTH ESTIMATE/CONTACT
    # ANALYSIS hint into the analysis prompt — a more precise signal than
    # motion-diff-based zone/trajectory hints alone.
    ai_enhanced_detection_enabled: bool = False
    # Which Ultralytics YOLO model the object-detection stage above runs.
    # "n" (nano) models are the fastest/lightest and the recommended
    # starting point on CPU-only hardware; larger models (s/m/l/x) are more
    # accurate but much slower.
    ai_object_detection_model: str = "yolo11n.pt"
    # Local-only face recognition to suppress alerts for enrolled household
    # members. Kept as its own toggle (rather than folded into
    # ai_enhanced_detection_enabled above) since it's a meaningfully
    # different kind of feature — privacy-sensitive rather than just
    # heavier compute. Enrollment (reference photos, computed embeddings)
    # is stored entirely on-device in the add-on's own database and never
    # uploaded to any cloud AI provider, and never leaves your network,
    # regardless of which ai_provider is configured. Off by default; enroll
    # household members via the web UI's AI tab.
    ai_face_recognition_enabled: bool = False

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
    # Separate from notify_ha (below): that one fires on every new-clip
    # download plus the daily digest and system events (2FA/auth/storage),
    # opt-in noise some users want and others don't. This fires only for
    # clips actually marked suspicious, alongside mobile/email/Discord.
    notify_ha_suspicious: bool = False
    # Master on/off switch for low-battery alerts, mirroring
    # notify_ha_suspicious's role: the channels themselves (mobile/SMTP/
    # Discord/HA-persistent, all above) are shared with suspicious-clip
    # alerts and configured once, but a user may want one alert type
    # without the other, so this gates battery alerts independently.
    battery_alerts_enabled: bool = False

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


def _parse_credentials(data: dict) -> tuple[str, str]:
    username = str(data.get("username", "")).strip()
    if not username:
        raise ValueError("username is required and cannot be empty")

    password = str(data.get("password", "")).strip()
    if not password:
        raise ValueError("password is required and cannot be empty")

    return username, password


def _parse_poll_limits(data: dict) -> tuple[int, int, int]:
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

    return poll_interval, retention_days, max_clips


def _sanitize_for_log(value: str) -> str:
    """Strip characters that could forge fake log lines out of an
    add-on-config-supplied string before it's interpolated into a log
    message (CR/LF log injection, CWE-117)."""
    return value.replace("\r", "").replace("\n", "")


def _parse_log_level(data: dict) -> str:
    log_level = str(data.get("log_level", "info")).lower()
    if log_level not in _VALID_LOG_LEVELS:
        _LOGGER.warning(
            "Unknown log_level %r, falling back to 'info'",
            _sanitize_for_log(log_level),
        )
        log_level = "info"
    return log_level


def _resolve_ai_escalation(data: dict, ai_provider: str) -> tuple[bool, str, str, str]:
    """Resolve the escalation enabled flag, provider, and model, honoring the
    deprecated ``openai_escalation_model`` option.

    ``ai_escalation_provider`` is a required schema field (always has a real
    provider value, defaulting to "ollama") rather than optional — an earlier
    design left it optional/absent-when-unset, but HA Supervisor's
    Configuration UI never renders a row for an optional select field with no
    value in options.json, which made it impossible for a user to ever set it
    through the UI on a fresh install (confirmed against a real Supervisor
    instance). ``ai_escalation_enabled`` is the actual on/off switch instead;
    the provider/model below are only meaningful when it's True.

    Returns ``(ai_escalation_enabled, ai_escalation_provider,
    ai_escalation_model, legacy_openai_escalation_model)``.
    """
    ai_escalation_enabled = bool(data.get("ai_escalation_enabled", False))
    ai_escalation_provider = (
        str(data.get("ai_escalation_provider", "") or "").strip().lower()
    )
    ai_escalation_model = str(data.get("ai_escalation_model", "") or "").strip()
    legacy_openai_escalation_model = str(
        data.get("openai_escalation_model", "") or ""
    ).strip()
    if (
        not ai_escalation_enabled
        and legacy_openai_escalation_model
        and ai_provider == "openai"
    ):
        _LOGGER.warning(
            "openai_escalation_model is deprecated as of 4.0.0 — use the "
            "'Enable Tier-2 Escalation' toggle with ai_escalation_provider="
            "'openai' and ai_escalation_model=%r instead. Continuing to "
            "honor the legacy option for now.",
            _sanitize_for_log(legacy_openai_escalation_model),
        )
        ai_escalation_enabled = True
        ai_escalation_provider = "openai"
        ai_escalation_model = legacy_openai_escalation_model
    if ai_escalation_provider and ai_escalation_provider not in _VALID_AI_PROVIDERS:
        _LOGGER.warning(
            "Unknown ai_escalation_provider %r, disabling escalation",
            _sanitize_for_log(ai_escalation_provider),
        )
        ai_escalation_enabled = False
        ai_escalation_provider = ""
        ai_escalation_model = ""
    if not ai_escalation_enabled:
        ai_escalation_provider = ""
        ai_escalation_model = ""
    return (
        ai_escalation_enabled,
        ai_escalation_provider,
        ai_escalation_model,
        legacy_openai_escalation_model,
    )


def _parse_storage_kwargs(data: dict) -> dict[str, Any]:
    camera_filter = [
        c.strip()
        for c in data.get("camera_filter", [])
        if isinstance(c, str) and c.strip()
    ]
    return {
        "download_path": Path(str(data.get("download_path", "/share/blink-clips"))),
        "organize_by_camera": bool(data.get("organize_by_camera", True)),
        "organize_by_date": bool(data.get("organize_by_date", True)),
        "filename_format": str(data.get("filename_format", "{camera}_{timestamp}")),
        "max_storage_gb": float(data.get("max_storage_gb", 10.0)),
        "camera_filter": camera_filter,
        "motion_only": bool(data.get("motion_only", False)),
        "time_window_start": str(data.get("time_window_start", "") or ""),
        "time_window_end": str(data.get("time_window_end", "") or ""),
        "download_thumbnails": bool(data.get("download_thumbnails", True)),
        "concurrent_downloads": max(
            1, min(10, int(data.get("concurrent_downloads", 3)))
        ),
        "retry_attempts": max(1, min(10, int(data.get("retry_attempts", 3)))),
        "retry_delay": max(0.0, min(300.0, float(data.get("retry_delay", 5.0)))),
    }


def _parse_notification_kwargs(data: dict) -> dict[str, Any]:
    return {
        "notify_ha": bool(data.get("notify_ha", False)),
        "ha_notification_title": str(
            data.get("ha_notification_title", "Blink Clip Downloaded")
        ),
        "webhook_url": str(data.get("webhook_url", "") or ""),
        "create_clip_manifest": bool(data.get("create_clip_manifest", True)),
    }


def _parse_media_server_kwargs(data: dict) -> dict[str, Any]:
    return {
        "enable_library_db": bool(data.get("enable_library_db", True)),
        "enable_media_server": bool(data.get("enable_media_server", True)),
        "media_server_port": max(
            1024, min(65535, int(data.get("media_server_port", 8099)))
        ),
        "watch_ha_events": bool(data.get("watch_ha_events", True)),
        "fast_poll_duration": max(
            10, min(3600, int(data.get("fast_poll_duration", 120)))
        ),
        "fast_poll_interval": max(5, min(60, int(data.get("fast_poll_interval", 15)))),
        "event_cameras": [
            c.strip()
            for c in data.get("event_cameras", [])
            if isinstance(c, str) and c.strip()
        ],
        "post_motion_delay": max(5, min(300, int(data.get("post_motion_delay", 30)))),
        "min_clip_duration": max(0, int(data.get("min_clip_duration", 0))),
    }


def _parse_digest_archive_kwargs(data: dict) -> dict[str, Any]:
    return {
        "digest_enabled": bool(data.get("digest_enabled", True)),
        "digest_time": str(data.get("digest_time", "08:00")),
        "archive_enabled": bool(data.get("archive_enabled", False)),
        "archive_after_days": max(1, int(data.get("archive_after_days", 60))),
        "download_local_storage": bool(data.get("download_local_storage", False)),
    }


def _parse_gdrive_kwargs(data: dict) -> dict[str, Any]:
    return {
        "gdrive_batch_size": max(1, min(int(data.get("gdrive_batch_size", 5)), 50)),
        "gdrive_check_interval": max(
            30, min(int(data.get("gdrive_check_interval", 300)), 3600)
        ),
    }


def _parse_ai_frame_strategy(data: dict) -> str:
    strategy = str(data.get("ai_frame_strategy", "smart") or "smart").strip().lower()
    return strategy if strategy in {"smart", "sequential", "uniform"} else "smart"


def _parse_ai_provider_kwargs(
    data: dict,
    ai_provider: str,
    ai_escalation_enabled: bool,
    ai_escalation_provider: str,
    ai_escalation_model: str,
    legacy_openai_escalation_model: str,
) -> dict[str, Any]:
    return {
        "ai_analysis_enabled": bool(data.get("ai_analysis_enabled", False)),
        "ai_provider": ai_provider,
        "ollama_url": str(data.get("ollama_url", "") or "").strip().rstrip("/"),
        "ollama_model": str(data.get("ollama_model", "") or "").strip(),
        "ollama_cloud_api_key": str(data.get("ollama_cloud_api_key", "") or "").strip(),
        "moondream_api_key": str(data.get("moondream_api_key", "") or "").strip(),
        "moondream_finetune_model": str(
            data.get("moondream_finetune_model", "") or ""
        ).strip(),
        "anthropic_api_key": str(data.get("anthropic_api_key", "") or "").strip(),
        "anthropic_model": str(data.get("anthropic_model", "") or "").strip()
        or "claude-haiku-4-5",
        "openai_api_key": str(data.get("openai_api_key", "") or "").strip(),
        "openai_model": str(data.get("openai_model", "") or "").strip()
        or "gpt-4o-mini",
        "openai_escalation_model": legacy_openai_escalation_model,
        "ai_escalation_enabled": ai_escalation_enabled,
        "ai_escalation_provider": ai_escalation_provider,
        "ai_escalation_model": ai_escalation_model,
    }


def _parse_ai_prompt_kwargs(data: dict) -> dict[str, Any]:
    return {
        "ai_prompt": str(data.get("ai_prompt", "") or "").strip()
        or AppConfig.ai_prompt,
        "ai_car_description": str(data.get("ai_car_description", "") or "").strip(),
        "ai_max_frames": max(1, min(100, int(data.get("ai_max_frames", 5)))),
        "ai_frame_interval": max(
            0.5, min(30.0, float(data.get("ai_frame_interval", 2.0)))
        ),
        "ai_suspicious_keywords": [
            k.strip()
            for k in data.get("ai_suspicious_keywords", [])
            if isinstance(k, str) and k.strip()
        ]
        or list(_DEFAULT_SUSPICIOUS_KEYWORDS),
        "ai_schedule_start": str(data.get("ai_schedule_start", "") or "").strip(),
        "ai_schedule_end": str(data.get("ai_schedule_end", "") or "").strip(),
        "ai_batch_size": max(1, min(50, int(data.get("ai_batch_size", 10)))),
        "ai_check_interval": max(10, min(3600, int(data.get("ai_check_interval", 60)))),
        "ai_min_confidence": max(
            0.0, min(1.0, float(data.get("ai_min_confidence", 0.5)))
        ),
    }


def _parse_ai_camera_kwargs(data: dict) -> dict[str, Any]:
    return {
        "ai_camera_prompts": [
            {"camera": str(item["camera"]), "prompt": str(item["prompt"])}
            for item in data.get("ai_camera_prompts", [])
            if isinstance(item, dict) and item.get("camera") and item.get("prompt")
        ],
        "ai_camera_descriptions": [
            {
                "camera": str(item["camera"]),
                "description": str(item.get("description", "")),
            }
            for item in data.get("ai_camera_descriptions", [])
            if isinstance(item, dict) and item.get("camera")
        ],
        "ai_frame_strategy": _parse_ai_frame_strategy(data),
        "ai_car_cameras": [
            c.strip()
            for c in data.get("ai_car_cameras", [])
            if isinstance(c, str) and c.strip()
        ],
    }


def _parse_ai_detection_kwargs(data: dict) -> dict[str, Any]:
    return {
        "ai_prompt_debug_enabled": bool(data.get("ai_prompt_debug_enabled", False)),
        "ai_enhanced_detection_enabled": bool(
            data.get("ai_enhanced_detection_enabled", False)
        ),
        "ai_object_detection_model": str(
            data.get("ai_object_detection_model", "") or ""
        ).strip()
        or "yolo11n.pt",
        "ai_face_recognition_enabled": bool(
            data.get("ai_face_recognition_enabled", False)
        ),
    }


def _parse_ai_kwargs(
    data: dict,
    ai_provider: str,
    ai_escalation_enabled: bool,
    ai_escalation_provider: str,
    ai_escalation_model: str,
    legacy_openai_escalation_model: str,
) -> dict[str, Any]:
    kwargs = _parse_ai_provider_kwargs(
        data,
        ai_provider,
        ai_escalation_enabled,
        ai_escalation_provider,
        ai_escalation_model,
        legacy_openai_escalation_model,
    )
    kwargs.update(_parse_ai_prompt_kwargs(data))
    kwargs.update(_parse_ai_camera_kwargs(data))
    kwargs.update(_parse_ai_detection_kwargs(data))
    return kwargs


def _parse_extended_notification_kwargs(data: dict) -> dict[str, Any]:
    return {
        "mobile_app_target": str(data.get("mobile_app_target", "") or "").strip(),
        "mobile_app_enabled": bool(data.get("mobile_app_enabled", False)),
        "smtp_host": str(data.get("smtp_host", "") or "").strip(),
        "smtp_port": max(25, min(65535, int(data.get("smtp_port", 587)))),
        "smtp_user": str(data.get("smtp_user", "") or "").strip(),
        "smtp_password": str(data.get("smtp_password", "") or ""),
        "smtp_recipients": [
            r.strip()
            for r in data.get("smtp_recipients", [])
            if isinstance(r, str) and r.strip()
        ],
        "smtp_sender": str(data.get("smtp_sender", "") or "").strip(),
        "smtp_enabled": bool(data.get("smtp_enabled", False)),
        "discord_webhook_url": str(data.get("discord_webhook_url", "") or "").strip(),
        "discord_enabled": bool(data.get("discord_enabled", False)),
        "notify_ha_suspicious": bool(data.get("notify_ha_suspicious", False)),
        "battery_alerts_enabled": bool(data.get("battery_alerts_enabled", False)),
    }


def _parse_config(data: dict) -> AppConfig:
    """Parse a raw options dict into a validated :class:`AppConfig`."""
    username, password = _parse_credentials(data)
    poll_interval, retention_days, max_clips = _parse_poll_limits(data)
    log_level = _parse_log_level(data)
    ai_provider = str(data.get("ai_provider", "ollama") or "ollama").strip().lower()
    (
        ai_escalation_enabled,
        ai_escalation_provider,
        ai_escalation_model,
        legacy_openai_escalation_model,
    ) = _resolve_ai_escalation(data, ai_provider)

    kwargs: dict[str, Any] = {
        "username": username,
        "password": password,
        "poll_interval": poll_interval,
        "max_clips_per_poll": max_clips,
        "retention_days": retention_days,
        "log_level": log_level,
    }
    kwargs.update(_parse_storage_kwargs(data))
    kwargs.update(_parse_notification_kwargs(data))
    kwargs.update(_parse_media_server_kwargs(data))
    kwargs.update(_parse_digest_archive_kwargs(data))
    kwargs.update(_parse_gdrive_kwargs(data))
    kwargs.update(
        _parse_ai_kwargs(
            data,
            ai_provider,
            ai_escalation_enabled,
            ai_escalation_provider,
            ai_escalation_model,
            legacy_openai_escalation_model,
        )
    )
    kwargs.update(_parse_extended_notification_kwargs(data))
    return AppConfig(**kwargs)
