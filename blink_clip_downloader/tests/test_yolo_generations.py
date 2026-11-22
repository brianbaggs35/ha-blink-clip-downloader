"""YOLO generations: which are current, which are deprecated, and the notice.

YOLO11 is deprecated, YOLO26 is current, and YOLO27 is expected -- Ultralytics
has announced it but (as of ultralytics 8.4.216) publishes no weights, so it
is deliberately not offered yet. These tests pin the bookkeeping that makes
adding it a two-line change that cannot be half done.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from blink_downloader.app import BlinkClipDownloaderApp
from blink_downloader.config import AppConfig
from blink_downloader.model_catalog import (
    YOLO_CURRENT_GENERATIONS,
    YOLO_DEPRECATED_GENERATIONS,
    yolo_deprecation_notice,
    yolo_generation,
)

_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("model", "generation"),
    [
        ("yolo11n.pt", 11),
        ("yolo11x-pose.pt", 11),
        ("YOLO26S.pt", 26),
        ("yolo26m-pose.pt", 26),
        ("/data/models/yolo11s.pt", 11),
        ("C:\\models\\yolo11m.pt", 11),
        ("yolo27n.pt", 27),
        ("yolo12n.pt", 12),
        ("yolov8n.pt", None),
        ("yolo11.pt", None),
        ("my-detector.pt", None),
        ("", None),
    ],
)
def test_the_generation_is_read_from_the_file_name(
    model: str, generation: int | None
) -> None:
    assert yolo_generation(model) == generation


def test_a_deprecated_checkpoint_is_told_what_to_use_instead() -> None:
    notice = yolo_deprecation_notice("ai_object_detection_model", "yolo11s.pt")
    assert notice is not None
    assert "ai_object_detection_model" in notice
    assert "YOLO11 is deprecated" in notice
    assert "'yolo26s.pt'" in notice


def test_the_pose_checkpoint_keeps_its_size_and_task() -> None:
    notice = yolo_deprecation_notice("ai_pose_model", "yolo11x-pose.pt")
    assert notice is not None
    assert "'yolo26x-pose.pt'" in notice


def test_a_checkpoint_given_as_a_path_is_pointed_at_the_bare_name() -> None:
    notice = yolo_deprecation_notice("ai_object_detection_model", "/data/yolo11m.pt")
    assert notice is not None
    assert "'yolo26m.pt'" in notice


@pytest.mark.parametrize(
    "model", ["yolo26n.pt", "yolo27n.pt", "yolo12n.pt", "yolov8n.pt", "custom.pt"]
)
def test_nothing_else_is_ever_flagged(model: str) -> None:
    assert yolo_deprecation_notice("ai_object_detection_model", model) is None


def test_no_generation_is_both_current_and_deprecated() -> None:
    assert not set(YOLO_CURRENT_GENERATIONS) & set(YOLO_DEPRECATED_GENERATIONS)
    assert YOLO_CURRENT_GENERATIONS


def _schema_generations(option: str) -> set[int]:
    manifest = yaml.safe_load((_ROOT / "config.yaml").read_text())
    names = re.findall(r"yolo\d+[a-z-]*\.pt", manifest["schema"][option])
    found = {yolo_generation(name) for name in names}
    assert None not in found
    return {g for g in found if g is not None}


@pytest.mark.parametrize("option", ["ai_object_detection_model", "ai_pose_model"])
def test_the_schema_offers_exactly_the_registered_generations(option: str) -> None:
    """Adding a generation to the schema without registering it here (or the
    reverse) fails: a half-added YOLO27 would either offer weights that do not
    download or never warn about the one it replaced."""
    assert _schema_generations(option) == set(YOLO_CURRENT_GENERATIONS) | set(
        YOLO_DEPRECATED_GENERATIONS
    )


@pytest.mark.parametrize(
    ("option", "default"),
    [
        ("ai_object_detection_model", AppConfig.ai_object_detection_model),
        ("ai_pose_model", AppConfig.ai_pose_model),
    ],
)
def test_the_shipped_defaults_are_the_current_generation(
    option: str, default: str
) -> None:
    manifest = yaml.safe_load((_ROOT / "config.yaml").read_text())
    assert manifest["options"][option] == default
    assert yolo_generation(default) == max(YOLO_CURRENT_GENERATIONS)


_DEPRECATED_WORD = {
    "en": "deprecated",
    "de": "veraltet",
    "es": "obsoleto",
    "fr": "obsolète",
    "ru": "устарел",
    "zh": "已弃用",
}


@pytest.mark.parametrize("option", ["ai_object_detection_model", "ai_pose_model"])
def test_every_translation_calls_yolo11_deprecated(option: str) -> None:
    """The Home Assistant configuration page is where the choice is made, so
    all six languages must say it, not only English."""
    paths = sorted((_ROOT / "translations").glob("*.yaml"))
    assert {p.stem for p in paths} == set(_DEPRECATED_WORD)
    for path in paths:
        text = yaml.safe_load(path.read_text(encoding="utf-8"))["configuration"][
            option
        ]["description"]
        assert "YOLO11" in text, path.name
        assert _DEPRECATED_WORD[path.stem] in text, path.name
        assert "YOLO27" in text, path.name


# ---------------------------------------------------------------------------
# The start-up warning
# ---------------------------------------------------------------------------


def _config(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "ai_enhanced_detection_enabled": True,
        "ai_vision_backend": "local",
        "ai_object_detection_model": "yolo11n.pt",
        "ai_pose_estimation_enabled": True,
        "ai_pose_model": "yolo11n-pose.pt",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _warnings(caplog: pytest.LogCaptureFixture, config: SimpleNamespace) -> str:
    with caplog.at_level("WARNING", logger="blink_downloader.app"):
        BlinkClipDownloaderApp._warn_deprecated_yolo(config)  # type: ignore[arg-type]
    return caplog.text


def test_both_deprecated_checkpoints_are_named_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    text = _warnings(caplog, _config())
    assert "ai_object_detection_model is 'yolo11n.pt'" in text
    assert "ai_pose_model is 'yolo11n-pose.pt'" in text


def test_the_current_generation_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config(
        ai_object_detection_model="yolo26n.pt", ai_pose_model="yolo26n-pose.pt"
    )
    assert _warnings(caplog, config) == ""


def test_pose_is_only_mentioned_when_it_will_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    text = _warnings(caplog, _config(ai_pose_estimation_enabled=False))
    assert "ai_object_detection_model" in text
    assert "ai_pose_model" not in text


@pytest.mark.parametrize(
    "changes",
    [
        {"ai_enhanced_detection_enabled": False},
        {"ai_vision_backend": "huggingface"},
    ],
)
def test_nothing_is_said_when_no_local_yolo_will_load(
    caplog: pytest.LogCaptureFixture, changes: dict[str, object]
) -> None:
    assert _warnings(caplog, _config(**changes)) == ""
