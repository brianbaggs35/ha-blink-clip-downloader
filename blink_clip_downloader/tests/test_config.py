"""Tests for blink_downloader.config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blink_downloader.config import AppConfig, _parse_config, load_config

# ---------------------------------------------------------------------------
# Default ai_prompt content
# ---------------------------------------------------------------------------


def test_default_ai_prompt_treats_leaving_home_as_routine():
    """A resident opening the front door and stepping out to leave (work,
    trash, walking to the car) must be covered by an explicit NOT SUSPICIOUS
    carve-out, symmetric with the existing "opening it... and going inside"
    rule — otherwise only entering the house is treated as routine and
    ordinary departures get flagged as suspicious."""
    prompt = AppConfig.ai_prompt
    assert "going inside" in prompt
    assert "stepping out to leave" in prompt


def test_default_ai_prompt_distinguishes_impact_from_mere_presence():
    """Lawn equipment/wind-blown debris merely being near a vehicle is
    routine, but an object actually striking/damaging it must still be
    flagged even with no person at fault — two opposite verdicts for the
    same "object near vehicle" trigger, disambiguated by contact/impact."""
    prompt = AppConfig.ai_prompt
    assert "with no visible impact on the vehicle" in prompt
    assert "visibly strikes a vehicle with force" in prompt
    assert "risks damaging a protected asset is worth reporting" in prompt


def test_default_ai_prompt_flags_animal_contact_with_vehicle():
    prompt = AppConfig.ai_prompt
    assert "urinates/defecates on a vehicle" in prompt


def test_default_ai_prompt_matches_config_yaml_default():
    """config.yaml's options.ai_prompt (what a fresh install actually gets)
    must stay in sync with this Python-level default — a stale, weaker
    config.yaml default would silently under-detect for every new install
    that doesn't hand-edit the AI prompt before first use."""
    import yaml

    yaml_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with yaml_path.open() as f:
        manifest = yaml.safe_load(f)

    assert manifest["options"]["ai_prompt"] == AppConfig.ai_prompt


# ---------------------------------------------------------------------------
# _parse_config
# ---------------------------------------------------------------------------


def test_minimal_valid_config():
    cfg = _parse_config({"username": "u@x.com", "password": "secret"})
    assert cfg.username == "u@x.com"
    assert cfg.password == "secret"
    assert cfg.poll_interval == 300
    assert cfg.retention_days == 30
    assert cfg.log_level == "info"
    assert cfg.camera_filter == []


def test_full_config(tmp_path):
    data = {
        "username": "user@example.com",
        "password": "p@ssw0rd",
        "download_path": str(tmp_path),
        "poll_interval": 120,
        "retention_days": 7,
        "max_storage_gb": 5.0,
        "camera_filter": ["Front Door", "Backyard"],
        "motion_only": True,
        "time_window_start": "22:00",
        "time_window_end": "06:00",
        "download_thumbnails": True,
        "concurrent_downloads": 4,
        "retry_attempts": 5,
        "notify_ha": False,
        "ha_notification_title": "My Title",
        "webhook_url": "https://hooks.example.com/blink",
        "create_clip_manifest": False,
        "log_level": "debug",
        "max_clips_per_poll": 200,
        "organize_by_camera": False,
        "organize_by_date": False,
        "filename_format": "{id}_{camera}",
    }
    cfg = _parse_config(data)
    assert cfg.poll_interval == 120
    assert cfg.retention_days == 7
    assert cfg.camera_filter == ["Front Door", "Backyard"]
    assert cfg.motion_only is True
    assert cfg.time_window_start == "22:00"
    assert cfg.download_thumbnails is True
    assert cfg.concurrent_downloads == 4
    assert cfg.webhook_url == "https://hooks.example.com/blink"
    assert cfg.organize_by_camera is False
    assert cfg.filename_format == "{id}_{camera}"


def test_missing_username_raises():
    with pytest.raises(ValueError, match="username"):
        _parse_config({"password": "p"})


def test_empty_username_raises():
    with pytest.raises(ValueError, match="username"):
        _parse_config({"username": "  ", "password": "p"})


def test_missing_password_raises():
    with pytest.raises(ValueError, match="password"):
        _parse_config({"username": "u"})


def test_poll_interval_too_low():
    with pytest.raises(ValueError, match="poll_interval"):
        _parse_config({"username": "u", "password": "p", "poll_interval": 5})


def test_poll_interval_too_high():
    with pytest.raises(ValueError, match="poll_interval"):
        _parse_config({"username": "u", "password": "p", "poll_interval": 9999})


def test_retention_days_negative():
    with pytest.raises(ValueError, match="retention_days"):
        _parse_config({"username": "u", "password": "p", "retention_days": -1})


def test_max_clips_out_of_range():
    with pytest.raises(ValueError, match="max_clips_per_poll"):
        _parse_config({"username": "u", "password": "p", "max_clips_per_poll": 0})


def test_unknown_log_level_defaults_to_info():
    cfg = _parse_config({"username": "u", "password": "p", "log_level": "verbose"})
    assert cfg.log_level == "info"


def test_camera_filter_strips_whitespace():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "camera_filter": ["  Front Door  ", " Backyard "],
        }
    )
    assert cfg.camera_filter == ["Front Door", "Backyard"]


def test_camera_filter_skips_empty_strings():
    cfg = _parse_config(
        {"username": "u", "password": "p", "camera_filter": ["", "  ", "Cam1"]}
    )
    assert cfg.camera_filter == ["Cam1"]


def test_concurrent_downloads_clamped():
    cfg = _parse_config({"username": "u", "password": "p", "concurrent_downloads": 999})
    assert cfg.concurrent_downloads == 10


def test_retry_delay_clamped_to_upper_bound():
    """Regression test: retry_delay previously had only a floor, unlike
    every sibling numeric option (concurrent_downloads, retry_attempts,
    ai_frame_interval, smtp_port), so a typo'd huge value would stall every
    failed-download retry indefinitely."""
    cfg = _parse_config({"username": "u", "password": "p", "retry_delay": 99999})
    assert cfg.retry_delay == 300.0


def test_retry_delay_floor_still_applies():
    cfg = _parse_config({"username": "u", "password": "p", "retry_delay": -5})
    assert cfg.retry_delay == 0.0


def test_fast_poll_duration_clamped_to_upper_bound():
    """Regression test: fast_poll_duration previously had only a floor,
    unlike its sibling fast_poll_interval/post_motion_delay (and unlike its
    own config.yaml schema, which already declares int(10,3600)) — a
    typo'd huge value would leave the add-on polling Blink at the aggressive
    fast_poll_interval rate indefinitely after a single motion event."""
    cfg = _parse_config({"username": "u", "password": "p", "fast_poll_duration": 99999})
    assert cfg.fast_poll_duration == 3600


def test_fast_poll_duration_floor_still_applies():
    cfg = _parse_config({"username": "u", "password": "p", "fast_poll_duration": 1})
    assert cfg.fast_poll_duration == 10


def test_download_path_as_path_object(tmp_path):
    cfg = _parse_config(
        {"username": "u", "password": "p", "download_path": str(tmp_path)}
    )
    assert isinstance(cfg.download_path, Path)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_from_file(options_file):
    cfg = load_config(options_file)
    assert cfg.username == "user@test.com"
    assert cfg.poll_interval == 120


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config(Path("/nonexistent/options.json"))


def test_load_config_invalid_json(tmp_path):
    bad_file = tmp_path / "options.json"
    bad_file.write_text("{not valid json}")
    with pytest.raises(json.JSONDecodeError):
        load_config(bad_file)


def test_post_motion_delay_default():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.post_motion_delay == 30


def test_post_motion_delay_clamped_to_min():
    cfg = _parse_config({"username": "u", "password": "p", "post_motion_delay": 1})
    assert cfg.post_motion_delay == 5


def test_post_motion_delay_clamped_to_max():
    cfg = _parse_config({"username": "u", "password": "p", "post_motion_delay": 9999})
    assert cfg.post_motion_delay == 300


def test_post_motion_delay_custom():
    cfg = _parse_config({"username": "u", "password": "p", "post_motion_delay": 60})
    assert cfg.post_motion_delay == 60


# ---------------------------------------------------------------------------
# startup_error field
# ---------------------------------------------------------------------------


def test_startup_error_defaults_to_empty_string():
    """startup_error is empty on a successfully parsed config."""
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.startup_error == ""


def test_appconfig_startup_error_can_be_set_directly():
    """AppConfig can be constructed with startup_error for web-only mode."""
    from blink_downloader.config import AppConfig

    cfg = AppConfig(username="", password="", startup_error="options.json not found")
    assert cfg.startup_error == "options.json not found"
    assert cfg.username == ""


# ---------------------------------------------------------------------------
# download_local_storage (v2.5.5)
# ---------------------------------------------------------------------------


def test_download_local_storage_defaults_to_false():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.download_local_storage is False


def test_download_local_storage_can_be_enabled():
    cfg = _parse_config(
        {"username": "u", "password": "p", "download_local_storage": True}
    )
    assert cfg.download_local_storage is True


def test_download_thumbnails_defaults_to_true():
    """Required for the Vehicles tab's car-zone picker and Biometrics'
    clip-browsing strip — on by default so both work out of the box."""
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.download_thumbnails is True


def test_download_thumbnails_can_be_disabled():
    cfg = _parse_config(
        {"username": "u", "password": "p", "download_thumbnails": False}
    )
    assert cfg.download_thumbnails is False


# ---------------------------------------------------------------------------
# AI Video Analysis config (v2.7.0)
# ---------------------------------------------------------------------------


def test_ai_analysis_defaults_to_disabled():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.ai_analysis_enabled is False
    assert cfg.ollama_url == ""
    assert cfg.ollama_model == ""
    assert cfg.ai_max_frames == 5
    assert cfg.ai_frame_interval == 2.0
    assert cfg.ai_car_description == ""
    assert cfg.ai_schedule_start == ""
    assert cfg.ai_schedule_end == ""
    assert cfg.ai_batch_size == 10
    assert cfg.ai_check_interval == 60
    assert cfg.ai_min_confidence == 0.5


def test_ai_min_confidence_can_be_lowered_to_zero():
    """0.0 is a legitimate explicit override (alert on every result) and
    must not be confused with "unset" — only an absent key falls back to
    the 0.5 default."""
    cfg = _parse_config({"username": "u", "password": "p", "ai_min_confidence": 0.0})
    assert cfg.ai_min_confidence == 0.0


def test_ai_analysis_full_config():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_analysis_enabled": True,
            "ollama_url": "http://192.168.1.100:11434",
            "ollama_model": "llava:7b",
            "ai_car_description": "Silver 2020 Honda Civic",
            "ai_max_frames": 5,
            "ai_frame_interval": 3.0,
            "ai_schedule_start": "22:00",
            "ai_schedule_end": "06:00",
            "ai_batch_size": 20,
            "ai_check_interval": 120,
        }
    )
    assert cfg.ai_analysis_enabled is True
    assert cfg.ollama_url == "http://192.168.1.100:11434"
    assert cfg.ollama_model == "llava:7b"
    assert cfg.ai_car_description == "Silver 2020 Honda Civic"
    assert cfg.ai_max_frames == 5
    assert cfg.ai_frame_interval == 3.0
    assert cfg.ai_schedule_start == "22:00"
    assert cfg.ai_schedule_end == "06:00"


def test_gdrive_defaults():
    """Client id/secret/backup_policy are NOT config.yaml options — they're
    edited from the Storage tab and live in google_drive_settings.json (see
    gdrive_client.py). Only batch_size/check_interval remain here, matching
    ai_batch_size/ai_check_interval's precedent for queue tuning."""
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.gdrive_batch_size == 5
    assert cfg.gdrive_check_interval == 300


def test_gdrive_explicit_values():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "gdrive_batch_size": 20,
            "gdrive_check_interval": 900,
        }
    )
    assert cfg.gdrive_batch_size == 20
    assert cfg.gdrive_check_interval == 900


def test_gdrive_batch_size_clamped_to_range():
    low = _parse_config({"username": "u", "password": "p", "gdrive_batch_size": 0})
    high = _parse_config({"username": "u", "password": "p", "gdrive_batch_size": 500})
    assert low.gdrive_batch_size == 1
    assert high.gdrive_batch_size == 50


def test_gdrive_check_interval_clamped_to_range():
    low = _parse_config({"username": "u", "password": "p", "gdrive_check_interval": 1})
    high = _parse_config(
        {"username": "u", "password": "p", "gdrive_check_interval": 999999}
    )
    assert low.gdrive_check_interval == 30
    assert high.gdrive_check_interval == 3600


def test_openai_escalation_model_defaults_empty():
    """Empty means escalation is disabled — unlike openai_model, there is no
    fallback default."""
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.openai_escalation_model == ""


def test_openai_escalation_model_parsed_and_stripped():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "openai_model": "gpt-4o-mini",
            "openai_escalation_model": "  gpt-4o  ",
        }
    )
    assert cfg.openai_model == "gpt-4o-mini"
    assert cfg.openai_escalation_model == "gpt-4o"


# ---------------------------------------------------------------------------
# Cross-provider escalation (v4.0.0) + legacy openai_escalation_model migration
# ---------------------------------------------------------------------------


def test_ai_escalation_provider_and_model_parsed_and_stripped():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_provider": "openai",
            "ai_escalation_enabled": True,
            "ai_escalation_provider": "  Moondream_Cloud  ",
            "ai_escalation_model": "  moondream3-preview/abc@50  ",
        }
    )
    assert cfg.ai_escalation_enabled is True
    assert cfg.ai_escalation_provider == "moondream_cloud"
    assert cfg.ai_escalation_model == "moondream3-preview/abc@50"


def test_ai_escalation_provider_defaults_empty():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.ai_escalation_enabled is False
    assert cfg.ai_escalation_provider == ""
    assert cfg.ai_escalation_model == ""


def test_ai_escalation_disabled_ignores_provider_and_model_even_when_set():
    """The toggle is the sole on/off switch — a real provider/model sitting
    in options.json (e.g. left over from before the user turned the toggle
    off, or the schema's own "ollama" default the dropdown always shows)
    must not leak into an active escalation when ai_escalation_enabled is
    False."""
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_provider": "openai",
            "ai_escalation_enabled": False,
            "ai_escalation_provider": "anthropic",
            "ai_escalation_model": "claude-opus-4-5",
        }
    )
    assert cfg.ai_escalation_enabled is False
    assert cfg.ai_escalation_provider == ""
    assert cfg.ai_escalation_model == ""


def test_ai_escalation_enabled_with_different_provider_than_tier_one():
    """Tier 2 must be able to use a completely different provider than
    tier 1 — this is the whole point of cross-provider escalation."""
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_provider": "openai",
            "openai_model": "gpt-5.4-nano",
            "ai_escalation_enabled": True,
            "ai_escalation_provider": "anthropic",
            "ai_escalation_model": "claude-opus-4-5",
        }
    )
    assert cfg.ai_provider == "openai"
    assert cfg.openai_model == "gpt-5.4-nano"
    assert cfg.ai_escalation_enabled is True
    assert cfg.ai_escalation_provider == "anthropic"
    assert cfg.ai_escalation_model == "claude-opus-4-5"


def test_legacy_openai_escalation_model_migrates_when_provider_is_openai():
    """Upgrading an existing install with openai_escalation_model set (and no
    new ai_escalation_provider/model) must keep working without editing YAML."""
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_provider": "openai",
            "openai_escalation_model": "gpt-4o",
        }
    )
    assert cfg.ai_escalation_provider == "openai"
    assert cfg.ai_escalation_model == "gpt-4o"
    assert cfg.openai_escalation_model == "gpt-4o"


def test_legacy_openai_escalation_model_not_migrated_for_other_providers():
    """The legacy field only ever meant something for ai_provider='openai' —
    migrating it for a different provider would silently enable escalation
    to a provider the user never configured."""
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_provider": "anthropic",
            "anthropic_api_key": "key",
            "openai_escalation_model": "gpt-4o",
        }
    )
    assert cfg.ai_escalation_provider == ""
    assert cfg.ai_escalation_model == ""
    assert cfg.openai_escalation_model == "gpt-4o"


def test_explicit_escalation_provider_takes_precedence_over_legacy():
    """With ai_escalation_enabled explicitly on, the legacy migration path
    (keyed off "not enabled") must not fire and clobber an explicit
    provider/model choice with the stale legacy field."""
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_provider": "openai",
            "openai_escalation_model": "gpt-4o",
            "ai_escalation_enabled": True,
            "ai_escalation_provider": "anthropic",
            "ai_escalation_model": "claude-opus-4-5",
        }
    )
    assert cfg.ai_escalation_provider == "anthropic"
    assert cfg.ai_escalation_model == "claude-opus-4-5"


def test_unknown_ai_escalation_provider_disables_escalation():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_escalation_provider": "not-a-real-provider",
            "ai_escalation_model": "some-model",
        }
    )
    assert cfg.ai_escalation_provider == ""
    assert cfg.ai_escalation_model == ""


def test_moondream_finetune_model_defaults_empty():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.moondream_finetune_model == ""


def test_moondream_finetune_model_parsed_and_stripped():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "moondream_finetune_model": "  moondream3-preview/abc123@50  ",
        }
    )
    assert cfg.moondream_finetune_model == "moondream3-preview/abc123@50"


def test_ai_prompt_debug_enabled_defaults_false():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.ai_prompt_debug_enabled is False


def test_ai_prompt_debug_enabled_can_be_turned_on():
    cfg = _parse_config(
        {"username": "u", "password": "p", "ai_prompt_debug_enabled": True}
    )
    assert cfg.ai_prompt_debug_enabled is True


def test_cv_pipeline_options_default_disabled():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.ai_enhanced_detection_enabled is False
    assert cfg.ai_object_detection_model == "yolo11n.pt"
    assert cfg.ai_face_recognition_enabled is False


def test_cv_pipeline_options_can_all_be_enabled():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_enhanced_detection_enabled": True,
            "ai_object_detection_model": "yolo11s.pt",
            "ai_face_recognition_enabled": True,
        }
    )
    assert cfg.ai_enhanced_detection_enabled is True
    assert cfg.ai_object_detection_model == "yolo11s.pt"
    assert cfg.ai_face_recognition_enabled is True


def test_ai_object_detection_model_blank_falls_back_to_default():
    cfg = _parse_config(
        {"username": "u", "password": "p", "ai_object_detection_model": ""}
    )
    assert cfg.ai_object_detection_model == "yolo11n.pt"


def test_ai_max_frames_clamped():
    cfg = _parse_config({"username": "u", "password": "p", "ai_max_frames": 150})
    assert cfg.ai_max_frames == 100
    cfg2 = _parse_config({"username": "u", "password": "p", "ai_max_frames": 0})
    assert cfg2.ai_max_frames == 1


def test_ollama_url_trailing_slash_stripped():
    cfg = _parse_config(
        {"username": "u", "password": "p", "ollama_url": "http://host:11434/"}
    )
    assert cfg.ollama_url == "http://host:11434"


def test_ai_suspicious_keywords_filters_empty():
    cfg = _parse_config(
        {
            "username": "u",
            "password": "p",
            "ai_suspicious_keywords": ["", "  ", "theft"],
        }
    )
    assert cfg.ai_suspicious_keywords == ["theft"]


# ---------------------------------------------------------------------------
# Extended Notifications config (v2.7.0)
# ---------------------------------------------------------------------------


def test_notification_channels_default_disabled():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.mobile_app_enabled is False
    assert cfg.mobile_app_target == ""
    assert cfg.smtp_enabled is False
    assert cfg.smtp_host == ""
    assert cfg.smtp_port == 587
    assert cfg.smtp_recipients == []
    assert cfg.discord_enabled is False
    assert cfg.discord_webhook_url == ""
    assert cfg.notify_ha_suspicious is False


def test_notify_ha_suspicious_enabled():
    """Independent of notify_ha — see notification_channels.py's dispatch()."""
    cfg = _parse_config(
        {"username": "u", "password": "p", "notify_ha_suspicious": True}
    )
    assert cfg.notify_ha_suspicious is True


def test_smtp_port_clamped():
    cfg = _parse_config({"username": "u", "password": "p", "smtp_port": 10})
    assert cfg.smtp_port == 25
    cfg2 = _parse_config({"username": "u", "password": "p", "smtp_port": 99999})
    assert cfg2.smtp_port == 65535


# ---------------------------------------------------------------------------
# AI provider config (v2.8.0)
# ---------------------------------------------------------------------------


def test_ai_provider_defaults_to_ollama():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.ai_provider == "ollama"


def test_ai_provider_moondream_cloud():
    cfg = _parse_config(
        {"username": "u", "password": "p", "ai_provider": "moondream_cloud"}
    )
    assert cfg.ai_provider == "moondream_cloud"


def test_ai_provider_moondream_local():
    cfg = _parse_config(
        {"username": "u", "password": "p", "ai_provider": "moondream_local"}
    )
    assert cfg.ai_provider == "moondream_local"


def test_ai_provider_normalised_lowercase():
    cfg = _parse_config({"username": "u", "password": "p", "ai_provider": "OLLAMA"})
    assert cfg.ai_provider == "ollama"


def test_moondream_api_key_defaults_to_empty():
    cfg = _parse_config({"username": "u", "password": "p"})
    assert cfg.moondream_api_key == ""


def test_moondream_api_key_parsed():
    cfg = _parse_config(
        {"username": "u", "password": "p", "moondream_api_key": "  sk-abc123  "}
    )
    assert cfg.moondream_api_key == "sk-abc123"
