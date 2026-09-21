"""Tests for the optional audio-tagging stage (vision/audio.py).

Same approach as tests/test_vision.py: transformers is not installed in
this environment, so the model is mocked through sys.modules and what gets
exercised for real is everything around it — the ffmpeg extraction, the
silence and no-track paths, the window selection, the relevance filter and
the hint wording.

The ffmpeg half is generated rather than mocked. Whether a clip with no
audio track is distinguishable from one with a silent track is precisely
the thing this stage has to get right, and asserting it against real files
is the only way to know ffmpeg still behaves the way the code assumes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from blink_downloader.analyzer.base import _audio_labels_json
from blink_downloader.prompt_segments import vision_hint_segments
from blink_downloader.vision import runtime
from blink_downloader.vision.audio import (
    _MAX_LABELS,
    _SAMPLE_RATE,
    AudioTagger,
    AudioTags,
    _loudest_window,
    _rms,
    build_audio_hint,
    is_relevant_label,
)
from blink_downloader.vision.pipeline import VisionConfig, VisionHints, VisionPipeline

# shutil.which, not a subprocess probe: running "ffmpeg -version" to find
# out whether ffmpeg exists raises FileNotFoundError when it does not, at
# import time, which collapses collection of this whole file instead of
# skipping it. CI's test job has no ffmpeg unless it is installed, and this
# machine does, so the broken form passed locally and failed there.
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _make_clip(path: Path, *, audio: str) -> Path:
    """Render a tiny real clip. *audio* is "tone", "silent" or "none"."""
    args = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=160x120:rate=10",
    ]
    if audio == "tone":
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac"]
    elif audio == "silent":
        args += ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(args, check=True, capture_output=True)
    return path


def _fake_transformers(results: list[dict]) -> MagicMock:
    module = MagicMock()
    module.pipeline.side_effect = lambda **_kw: lambda _inp, **_call: results
    return module


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "Speech",
        "Shout",
        "Breaking glass",
        "Dog",
        "Car alarm",
        "Gunshot, gunfire",
        "Walk, footsteps",
        "Smoke detector, smoke alarm",
        "Power tool",
    ],
)
def test_security_relevant_labels_are_kept(label: str) -> None:
    assert is_relevant_label(label) is True


@pytest.mark.parametrize(
    "label", ["Music", "Inside, small room", "Piano", "Rustling leaves", "Silence"]
)
def test_irrelevant_labels_are_dropped(label: str) -> None:
    """AudioSet has 527 classes and most say nothing about security."""
    assert is_relevant_label(label) is False


def test_relevance_matching_respects_word_boundaries() -> None:
    """ "Cartoon" contains "car" but is not a vehicle."""
    assert is_relevant_label("Cartoon") is False
    assert is_relevant_label("Car") is True


def test_rms_of_empty_audio_is_zero() -> None:
    assert _rms(np.array([], dtype=np.float32)) == 0.0


def test_rms_measures_signal_level() -> None:
    assert _rms(np.full(100, 0.5, dtype=np.float32)) == pytest.approx(0.5)


def test_loudest_window_returns_short_audio_unchanged() -> None:
    samples = np.zeros(_SAMPLE_RATE, dtype=np.float32)
    assert _loudest_window(samples) is samples


def test_loudest_window_finds_the_noisy_stretch() -> None:
    """The point of the window: a long clip is mostly the quiet before it."""
    quiet = np.zeros(_SAMPLE_RATE * 30, dtype=np.float32)
    quiet[_SAMPLE_RATE * 20 : _SAMPLE_RATE * 25] = 0.9
    window = _loudest_window(quiet)
    assert window.size == _SAMPLE_RATE * 10
    assert _rms(window) > 0.5


def test_loudest_window_still_sees_the_very_end_of_a_clip() -> None:
    """A clip triggered by a sound often has the sound at the end, and the
    stride only lands exactly on the end when the length divides by it."""
    samples = np.zeros(int(_SAMPLE_RATE * 32.5), dtype=np.float32)
    samples[-_SAMPLE_RATE * 2 :] = 0.9
    assert _rms(_loudest_window(samples)) > 0.3


def test_no_hint_without_tags() -> None:
    assert build_audio_hint(None) is None
    assert build_audio_hint(AudioTags()) is None
    assert build_audio_hint(AudioTags(silent=True)) is None


def test_hint_names_the_sounds_and_hedges() -> None:
    hint = build_audio_hint(AudioTags(labels=[("Shout", 0.61), ("Glass", 0.2)]))
    assert hint is not None
    assert "shout (61%)" in hint and "glass (20%)" in hint
    # The privacy promise and the uncertainty both have to reach the model.
    assert "not a transcript" in hint
    assert "weak supporting evidence" in hint


def test_any_tags_reflects_labels() -> None:
    assert AudioTags().any_tags is False
    assert AudioTags(labels=[("Dog", 0.5)]).any_tags is True


# --------------------------------------------------------------------------
# Extraction, against real files
# --------------------------------------------------------------------------


@needs_ffmpeg
async def test_extracts_mono_16k_audio_from_a_real_clip(tmp_path: Path) -> None:
    clip = _make_clip(tmp_path / "tone.mp4", audio="tone")
    samples = await AudioTagger().extract_audio(str(clip))
    assert samples is not None
    # ~2 seconds at 16 kHz, with signal in it.
    assert _SAMPLE_RATE * 1.5 < samples.size < _SAMPLE_RATE * 2.5
    assert _rms(samples) > 0.01


@needs_ffmpeg
async def test_a_clip_with_no_audio_track_yields_nothing(tmp_path: Path) -> None:
    """The common case: the camera's microphone is off, or it has none."""
    clip = _make_clip(tmp_path / "mute.mp4", audio="none")
    assert await AudioTagger().extract_audio(str(clip)) is None


async def test_missing_ffmpeg_disables_the_stage_rather_than_raising() -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        assert await AudioTagger().extract_audio("/anything.mp4") is None


async def test_a_hung_ffmpeg_is_killed_rather_than_left_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing else would ever time this out, and a lingering child per
    clip is worse than the missing hint."""
    monkeypatch.setattr("blink_downloader.vision.audio._FFMPEG_TIMEOUT", 0.01)
    proc = MagicMock()

    async def never_finishes() -> tuple[bytes, bytes]:
        await asyncio.sleep(30)
        return b"", b""

    proc.communicate = never_finishes
    proc.wait = AsyncMock()
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await AudioTagger().extract_audio("/hangs.mp4") is None

    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


async def test_an_unreadable_clip_disables_the_stage_rather_than_raising() -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("boom")):
        assert await AudioTagger().extract_audio("/anything.mp4") is None


# --------------------------------------------------------------------------
# Model loading
# --------------------------------------------------------------------------


async def test_ensure_ready_false_without_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "transformers", raising=False)
    with patch("builtins.__import__", side_effect=ImportError("no transformers")):
        assert await AudioTagger().ensure_ready() is False


async def test_ensure_ready_false_when_cpu_cannot_run_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
    assert await AudioTagger().ensure_ready() is False


async def test_ensure_ready_reports_a_hugging_face_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = MagicMock()
    module.pipeline.side_effect = RuntimeError("401 Client Error: huggingface.co")
    monkeypatch.setitem(sys.modules, "transformers", module)
    monkeypatch.setattr(
        "blink_downloader.vision.runtime._is_huggingface_auth_error", lambda _exc: True
    )
    assert await AudioTagger().ensure_ready() is False


async def test_ensure_ready_survives_any_other_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = MagicMock()
    module.pipeline.side_effect = RuntimeError("out of memory")
    monkeypatch.setitem(sys.modules, "transformers", module)
    assert await AudioTagger().ensure_ready() is False


async def test_ensure_ready_loads_once_for_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four clips arriving at once must not load four copies of the model."""
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = MagicMock()

    def slow_load(**_kw):
        # A real load takes seconds. Without the delay the first caller
        # finishes before the others are even scheduled, so the lock and
        # its double-check are never actually put under contention.
        time.sleep(0.05)
        return lambda _inp, **_call: []

    module.pipeline.side_effect = slow_load
    monkeypatch.setitem(sys.modules, "transformers", module)

    tagger = AudioTagger()
    assert all(await asyncio.gather(*(tagger.ensure_ready() for _ in range(4))))
    module.pipeline.assert_called_once()


async def test_the_model_is_only_loaded_on_the_first_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = _fake_transformers([])
    monkeypatch.setitem(sys.modules, "transformers", module)

    tagger = AudioTagger()
    assert await tagger.ensure_ready() is True
    assert await tagger.ensure_ready() is True
    module.pipeline.assert_called_once()


def test_load_sync_refuses_an_incompatible_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
    with pytest.raises(runtime.CPUIncompatibleError):
        AudioTagger()._load_sync()


def test_a_custom_model_id_overrides_the_default() -> None:
    assert AudioTagger("some/other-model")._model_id == "some/other-model"
    assert AudioTagger()._model_id == AudioTagger.DEFAULT_MODEL_ID


# --------------------------------------------------------------------------
# End to end, real audio through a mocked classifier
# --------------------------------------------------------------------------


@needs_ffmpeg
async def test_tags_real_audio_filtered_sorted_and_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        _fake_transformers(
            [
                {"label": "Inside, small room", "score": 0.95},  # irrelevant
                {"label": "Speech", "score": 0.44},
                {"label": "Shout", "score": 0.61},
                {"label": "Glass", "score": 0.20},
                {"label": "Music", "score": 0.50},  # irrelevant
                {"label": "Dog", "score": 0.02},  # below the floor
                {"label": "Car", "score": 0.19},
            ]
        ),
    )
    clip = _make_clip(tmp_path / "tone.mp4", audio="tone")
    tagger = AudioTagger()
    assert await tagger.ensure_ready() is True
    tags = await tagger.tag(str(clip))

    assert tags is not None
    assert len(tags.labels) == _MAX_LABELS
    assert [label for label, _ in tags.labels] == ["Shout", "Speech", "Glass"]
    assert tags.silent is False


@needs_ffmpeg
async def test_a_silent_audio_track_is_not_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classifying digital silence returns confident nonsense."""
    module = _fake_transformers([{"label": "Speech", "score": 0.99}])
    monkeypatch.setitem(sys.modules, "transformers", module)
    clip = _make_clip(tmp_path / "silent.mp4", audio="silent")

    tags = await AudioTagger().tag(str(clip))

    assert tags == AudioTags(labels=[], silent=True)
    assert build_audio_hint(tags) is None
    module.pipeline.assert_not_called()


@needs_ffmpeg
async def test_no_audio_track_produces_no_tags(tmp_path: Path) -> None:
    clip = _make_clip(tmp_path / "mute.mp4", audio="none")
    assert await AudioTagger().tag(str(clip)) is None


@needs_ffmpeg
async def test_tag_returns_nothing_when_the_model_will_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
    clip = _make_clip(tmp_path / "tone.mp4", audio="tone")
    tagger = AudioTagger()
    assert await tagger.tag(str(clip)) is None
    # The load it kicked off has to be reaped, and it has to have failed.
    assert tagger._load_task is not None
    assert await tagger._load_task is False


@needs_ffmpeg
async def test_the_first_clip_does_not_wait_for_the_model_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of loading in the background.

    A first-ever load pulls a few hundred MB. Waiting for it would put
    every clip queued behind that download, which is exactly the
    minutes-late verdict this stage must never cause.
    """
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    started = threading.Event()
    release = threading.Event()
    module = MagicMock()

    def slow_download(**_kw):
        started.set()
        release.wait(10)
        return lambda _inp, **_call: [{"label": "Shout", "score": 0.8}]

    module.pipeline.side_effect = slow_download
    monkeypatch.setitem(sys.modules, "transformers", module)

    clip = _make_clip(tmp_path / "tone.mp4", audio="tone")
    tagger = AudioTagger()

    began = time.monotonic()
    assert await tagger.tag(str(clip)) is None
    elapsed = time.monotonic() - began

    # Returned without waiting -- in fact without even yielding to the
    # load it started, which is why `started` is only checked after
    # giving the loop a turn.
    assert elapsed < 5
    assert tagger._load_task is not None and not tagger._load_task.done()
    await asyncio.sleep(0.2)
    assert started.is_set()

    # More clips arriving meanwhile queue no second download.
    assert await tagger.tag(str(clip)) is None
    assert await tagger.tag(str(clip)) is None
    assert module.pipeline.call_count == 1

    # ...and the clip after it gets the model the first one paid nothing for.
    release.set()
    assert await tagger._load_task is True
    tags = await tagger.tag(str(clip))
    assert tags is not None and tags.labels == [("Shout", 0.8)]
    module.pipeline.assert_called_once()


@needs_ffmpeg
async def test_a_stalled_stage_gives_up_instead_of_delaying_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard ceiling on what this stage can cost one clip."""
    monkeypatch.setattr("blink_downloader.vision.audio._STAGE_BUDGET", 0.05)
    clip = _make_clip(tmp_path / "tone.mp4", audio="tone")
    tagger = AudioTagger()

    async def never_returns(_path: str) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(tagger, "_tag", never_returns)
    began = time.monotonic()
    assert await tagger.tag(str(clip)) is None
    assert time.monotonic() - began < 5


async def test_the_budget_expiring_takes_ffmpeg_with_it() -> None:
    """Cancellation mid-decode must not leave an ffmpeg per clip behind."""
    proc = MagicMock()

    async def never_finishes() -> tuple[bytes, bytes]:
        await asyncio.sleep(30)
        return b"", b""

    proc.communicate = never_finishes
    proc.wait = AsyncMock()
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        task = asyncio.ensure_future(AudioTagger().extract_audio("/slow.mp4"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


async def test_a_permanently_missing_dependency_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Retrying per clip is right -- an interrupted download resumes --
    but the usual cause never resolves, and one warning per clip would
    bury the rest of the log."""
    monkeypatch.delitem(sys.modules, "transformers", raising=False)
    tagger = AudioTagger()
    with (
        caplog.at_level(logging.WARNING, logger="blink_downloader.vision.audio"),
        patch("builtins.__import__", side_effect=ImportError("no transformers")),
    ):
        assert await tagger.ensure_ready() is False
        assert await tagger.ensure_ready() is False
        assert await tagger.ensure_ready() is False

    assert caplog.text.count("transformers package is not installed") == 1


@needs_ffmpeg
async def test_a_failing_classifier_never_breaks_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = MagicMock()

    def explode(**_kw):
        def run(_inp, **_call):
            raise RuntimeError("inference blew up")

        return run

    module.pipeline.side_effect = explode
    monkeypatch.setitem(sys.modules, "transformers", module)
    clip = _make_clip(tmp_path / "tone.mp4", audio="tone")
    tagger = AudioTagger()
    assert await tagger.ensure_ready() is True

    assert await tagger.tag(str(clip)) is None


@needs_ffmpeg
async def test_scores_are_independent_per_sound_not_shares_of_one_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AudioSet is multi-label and this checkpoint was trained that way.

    The pipeline's own default is softmax, which makes all 527 classes
    share one probability budget: glass breaking *and* a shout -- the
    combination worth hearing about -- would then suppress each other, and
    every score would land far below what the model actually asserts,
    under the reporting floor.
    """
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    seen: dict[str, Any] = {}
    module = MagicMock()

    def build(**_kw):
        def run(_inp, **call_kwargs):
            seen.update(call_kwargs)
            return [{"label": "Shout", "score": 0.8}]

        return run

    module.pipeline.side_effect = build
    monkeypatch.setitem(sys.modules, "transformers", module)

    clip = _make_clip(tmp_path / "tone.mp4", audio="tone")
    tagger = AudioTagger()
    assert await tagger.ensure_ready() is True
    await tagger.tag(str(clip))

    assert seen["function_to_apply"] == "sigmoid"
    # Enough candidates that a relevant label ranked below the ambience
    # classes AudioSet puts on top is still reachable.
    assert int(seen["top_k"]) >= 20


# --------------------------------------------------------------------------
# Wiring into the vision pipeline
# --------------------------------------------------------------------------


async def test_audio_stage_is_off_by_default() -> None:
    pipeline = VisionPipeline(VisionConfig())
    with patch.object(AudioTagger, "tag", autospec=True) as tag:
        hints = await pipeline.process_clip([b"frame"], clip_path="/clip.mp4")
    assert hints.audio_hint is None
    tag.assert_not_called()


async def test_audio_stage_needs_a_clip_path() -> None:
    """The other stages work from decoded frames; this one reads the file."""
    pipeline = VisionPipeline(VisionConfig(audio_analysis_enabled=True))
    with patch.object(AudioTagger, "tag", autospec=True) as tag:
        hints = await pipeline.process_clip([b"frame"])
    assert hints.audio_hint is None
    tag.assert_not_called()


async def test_enabled_audio_stage_reaches_the_hints() -> None:
    pipeline = VisionPipeline(VisionConfig(audio_analysis_enabled=True))
    tags = AudioTags(labels=[("Breaking glass", 0.7)])
    with patch.object(AudioTagger, "tag", autospec=True, return_value=tags) as tag:
        hints = await pipeline.process_clip([b"frame"], clip_path="/clip.mp4")

    tag.assert_awaited_once()
    assert tag.await_args is not None
    assert tag.await_args.args[1] == "/clip.mp4"
    assert hints.audio_hint is not None
    assert "breaking glass (70%)" in hints.audio_hint


async def test_a_clip_with_no_audio_does_not_dent_the_evidence_score() -> None:
    """Silence says nothing about what the camera saw.

    unavailable_sources feeds security/evidence.py's stage-coverage score,
    whose denominator counts the four *visual* stages it was calibrated
    against. A fifth, non-visual entry would quietly penalise every clip
    that simply had nothing audible in it.
    """
    pipeline = VisionPipeline(VisionConfig(audio_analysis_enabled=True))
    with patch.object(AudioTagger, "tag", autospec=True, return_value=None):
        enabled = await pipeline.process_clip([b"frame"], clip_path="/clip.mp4")
    off = await VisionPipeline(VisionConfig()).process_clip([b"frame"])

    assert enabled.audio_hint is None
    assert enabled.unavailable_sources == off.unavailable_sources


def test_the_audio_hint_is_offered_to_the_prompt() -> None:
    hints = VisionHints(audio_hint="\n\nAUDIO: a shout")
    assert "\n\nAUDIO: a shout" in vision_hint_segments(hints)


def test_the_configured_model_and_token_reach_the_tagger() -> None:
    pipeline = VisionPipeline(
        VisionConfig(
            audio_analysis_enabled=True, audio_model="some/model", hf_token="tok"
        )
    )
    assert pipeline._audio._model_id == "some/model"
    assert pipeline._audio._hf_token == "tok"


# --------------------------------------------------------------------------
# Reaching the clip modal
# --------------------------------------------------------------------------


def test_recognized_sounds_are_stored_for_the_clip_modal() -> None:
    """The stage is otherwise invisible: without this a user who turns it
    on has no way to see it working, or to tell why a clip was flagged."""
    hints = VisionHints(audio_tags=AudioTags(labels=[("Shout", 0.612345)]))
    stored = _audio_labels_json(hints)
    assert json.loads(stored) == [{"label": "Shout", "score": 0.6123}]


@pytest.mark.parametrize(
    "hints",
    [None, VisionHints(), VisionHints(audio_tags=AudioTags(silent=True))],
    ids=["no-pipeline", "stage-off", "silent-track"],
)
def test_nothing_heard_stores_nothing(hints: VisionHints | None) -> None:
    """All three cases mean the same thing to the modal: no sound chips."""
    assert _audio_labels_json(hints) == ""
