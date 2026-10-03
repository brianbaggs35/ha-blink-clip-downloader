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
import warnings
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
    _SILENCE_RMS,
    AudioTagger,
    AudioTags,
    _loudest_window,
    _peak_level,
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


def _make_clip(path: Path, *, audio: str, seconds: int = 2) -> Path:
    """Render a tiny real clip.

    *audio* is "tone" (plainly audible), "silent" (digital zero),
    "noisefloor" (a muted microphone's inaudible hiss), "quiet-tail"
    (silence except for a modest sound in the final two seconds),
    "brief-sound" (a muted camera's noise floor with half a second of real
    sound in it, which is what an actual security clip looks like) or
    "none" (no audio track at all).
    """
    args = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=5",
    ]  # fmt: skip
    sources = {
        "tone": [f"sine=frequency=440:duration={seconds}"],
        "silent": ["anullsrc=r=16000:cl=mono"],
        "noisefloor": [f"anoisesrc=color=white:amplitude=0.001:duration={seconds}"],
        "quiet-tail": [f"sine=frequency=880:duration={seconds}"],
        "brief-sound": [
            f"anoisesrc=color=pink:amplitude=0.0012:duration={seconds}",
            f"sine=frequency=800:duration={seconds}",
        ],
    }
    if audio in sources:
        for source in sources[audio]:
            args += ["-f", "lavfi", "-i", source]
        if audio == "quiet-tail":
            quiet = f"volume=enable='lt(t,{seconds - 2})':volume=0"
            loud = f"volume=enable='gte(t,{seconds - 2})':volume=0.05"
            args += ["-filter:a", f"{quiet},{loud}"]
        if audio == "brief-sound":
            # Half a second of sound partway through an otherwise
            # inaudible track: loud enough to hear and classify, far too
            # short to lift the ten-second average that used to gate this.
            burst = "volume=0.02,atrim=0:0.5,adelay=800|800"
            args += [
                "-filter_complex",
                (
                    f"[2:a]{burst},apad=whole_dur={seconds}[b];"
                    f"[1:a][b]amix=inputs=2:duration=first:normalize=0[a]"
                ),
                "-map",
                "[a]",
                "-map",
                "0:v",
            ]
        args += ["-c:a", "aac"]
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


def test_peak_level_of_empty_audio_is_zero() -> None:
    assert _peak_level(np.array([], dtype=np.float32)) == 0.0


def test_peak_level_of_audio_shorter_than_one_frame_measures_all_of_it() -> None:
    """Under a tenth of a second there is nothing to take a peak over."""
    short = np.full(100, 0.5, dtype=np.float32)
    assert _peak_level(short) == pytest.approx(_rms(short))


def test_peak_level_finds_a_brief_sound_the_average_would_bury() -> None:
    """The bug this replaced: a real sound discarded as silence.

    Half a second of sound in a ten-second window is a fortieth of it, so
    the window's average sits far closer to the quiet either side than to
    the sound itself — which is exactly the shape of every clip a camera
    records, and exactly what the old gate measured.
    """
    # 3e-3 against a 1e-4 floor: audible, ~30x the room tone around it, and
    # still quiet enough that averaging it over ten seconds hides it.
    samples = np.full(_SAMPLE_RATE * 10, 1e-4, dtype=np.float32)
    samples[_SAMPLE_RATE * 2 : _SAMPLE_RATE * 2 + _SAMPLE_RATE // 2] = 3e-3

    assert _rms(samples) < _SILENCE_RMS
    assert _peak_level(samples) > _SILENCE_RMS


def test_peak_level_hears_a_sound_the_clip_ends_on() -> None:
    """A clip triggered by a sound often ends on it, and the frames do not
    divide evenly — so the remainder has to be measured too."""
    samples = np.full(_SAMPLE_RATE * 10 + 1234, 1e-5, dtype=np.float32)
    samples[-(_SAMPLE_RATE // 20) :] = 0.05
    assert _peak_level(samples) > _SILENCE_RMS


def test_peak_level_still_reads_a_flat_noise_floor_as_silence() -> None:
    """The case the gate exists for: a muted camera's track is flat, so
    its peak is barely above its average and both stay inaudible."""
    floor = np.full(_SAMPLE_RATE * 10, 2e-4, dtype=np.float32)
    assert _peak_level(floor) < _SILENCE_RMS


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


def test_hint_names_the_sounds_and_hedges() -> None:
    hint = build_audio_hint(AudioTags(labels=[("Shout", 0.61), ("Glass", 0.2)]))
    assert hint is not None
    assert "shout (61%)" in hint
    assert "glass (20%)" in hint
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


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"\x01\x02\x03", "three bytes is not a whole 4-byte sample"),
        (b"\x00" * 4097, "one byte past a whole number of samples"),
    ],
)
async def test_audio_cut_off_mid_sample_does_not_fail_the_clip(
    payload: bytes, reason: str
) -> None:
    """np.frombuffer raises on a buffer that is not a whole number of
    elements rather than truncating it, and ffmpeg killed mid-write, a
    disk filling up, or a container it only partly understood all produce
    exactly that. Escaping here would fail a perfectly analyzable clip
    over an optional hint — vision/pipeline.py deliberately wraps nothing,
    so each stage contains its own failures.
    """
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(payload, b""))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await AudioTagger().tag("/truncated.mp4") is None, reason


async def test_a_defect_in_the_stage_never_fails_the_clip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stage boundary, matching _assess_security's own reasoning: a
    bug in an additive hint must not turn an analyzable clip into a
    failed, retried one — but it is our code, so it is logged loudly
    rather than swallowed."""
    with (
        patch.object(AudioTagger, "_tag", side_effect=RuntimeError("defect")),
        caplog.at_level(logging.ERROR, logger="blink_downloader.vision.audio"),
    ):
        assert await AudioTagger().tag("/c.mp4") is None
    assert "Audio analysis failed" in caplog.text
    assert "RuntimeError" in caplog.text


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


async def test_default_ast_load_suppresses_only_its_known_mel_filter_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = MagicMock()

    def build(**_kw):
        warnings.warn_explicit(
            "At least one mel filter has all zero values. The value for "
            "`num_mel_filters` (128) may be set too high.",
            UserWarning,
            filename="transformers/audio_utils.py",
            lineno=821,
            module="transformers.audio_utils",
        )
        warnings.warn("unrelated model warning", UserWarning)
        return lambda _inp, **_call: []

    module.pipeline.side_effect = build
    monkeypatch.setitem(sys.modules, "transformers", module)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert await AudioTagger().ensure_ready() is True

    assert [str(warning.message) for warning in caught] == ["unrelated model warning"]


async def test_custom_model_keeps_mel_filter_warnings_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = MagicMock()

    def build(**_kw):
        warnings.warn_explicit(
            "At least one mel filter has all zero values.",
            UserWarning,
            filename="transformers/audio_utils.py",
            lineno=821,
            module="transformers.audio_utils",
        )
        return lambda _inp, **_call: []

    module.pipeline.side_effect = build
    monkeypatch.setitem(sys.modules, "transformers", module)

    tagger = AudioTagger("custom/audio-model")
    with pytest.warns(UserWarning, match="At least one mel filter has all zero values"):
        ready = await tagger.ensure_ready()

    assert ready is True


def test_load_sync_refuses_an_incompatible_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
    tagger = AudioTagger()
    with pytest.raises(runtime.CPUIncompatibleError):
        tagger._load_sync()


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


@needs_ffmpeg
@pytest.mark.parametrize(
    "audio", ["silent", "noisefloor"], ids=["digital", "muted-mic"]
)
async def test_a_track_with_no_audible_sound_is_not_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audio: str, caplog
) -> None:
    """A camera with audio recording off in the Blink app still writes a
    track. Whether that track is digital zero or an inaudible noise floor
    depends on the hardware, and both must short-circuit: classifying
    room tone burns an inference per clip to be told it heard a room.
    """
    module = _fake_transformers([{"label": "Speech", "score": 0.99}])
    monkeypatch.setitem(sys.modules, "transformers", module)
    clip = _make_clip(tmp_path / f"{audio}.mp4", audio=audio)

    with caplog.at_level(logging.INFO, logger="blink_downloader.vision.audio"):
        assert await AudioTagger().tag(str(clip)) is None

    module.pipeline.assert_not_called()
    # And it says where to look, because the cause is a setting in a
    # different app entirely.
    assert "Blink app" in caplog.text


@needs_ffmpeg
async def test_the_no_sound_notice_is_logged_once_not_per_clip(
    tmp_path: Path, caplog
) -> None:
    """Every clip from that camera is silent, so an unguarded notice would
    be one log line per clip forever."""
    clip = _make_clip(tmp_path / "silent.mp4", audio="silent")
    tagger = AudioTagger()
    with caplog.at_level(logging.INFO, logger="blink_downloader.vision.audio"):
        for _ in range(3):
            assert await tagger.tag(str(clip)) is None
    assert caplog.text.count("Blink app") == 1


@needs_ffmpeg
async def test_a_quiet_event_in_a_long_clip_is_still_heard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is on the window actually classified, not the whole track.

    A two-second sound in a sixty-second clip is diluted several-fold by
    the silence around it; gating on the track average would discard
    exactly the short quiet events worth hearing.
    """
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = _fake_transformers([{"label": "Glass", "score": 0.8}])
    monkeypatch.setitem(sys.modules, "transformers", module)

    clip = _make_clip(tmp_path / "quiet-event.mp4", audio="quiet-tail", seconds=60)
    tagger = AudioTagger()
    assert await tagger.ensure_ready() is True
    samples = await tagger.extract_audio(str(clip))
    assert samples is not None
    # Diluted across the whole track it would look like silence...
    assert _rms(samples) < _SILENCE_RMS
    # ...but the window that gets classified does not, so it is heard.
    tags = await tagger.tag(str(clip))
    assert tags is not None
    assert tags.labels == [("Glass", 0.8)]


@needs_ffmpeg
async def test_a_brief_real_sound_is_not_mistaken_for_a_silent_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real clip whose sound the window average used to swallow.

    Half a second of sound well above the camera's own noise floor, in an
    otherwise inaudible twenty-second track — which is what a camera
    records when something brief happens. Gating on the window's average
    read this as a muted microphone and told the user to go check a
    setting that was never wrong.
    """
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = _fake_transformers([{"label": "Dog", "score": 0.7}])
    monkeypatch.setitem(sys.modules, "transformers", module)

    clip = _make_clip(tmp_path / "brief.mp4", audio="brief-sound", seconds=20)
    tagger = AudioTagger()
    assert await tagger.ensure_ready() is True
    samples = await tagger.extract_audio(str(clip))
    assert samples is not None
    window = _loudest_window(samples)
    # The statistic that used to gate this still reads it as silence...
    assert _rms(window) < _SILENCE_RMS
    # ...while the loudest moment in it is plainly audible.
    assert _peak_level(window) > _SILENCE_RMS

    tags = await tagger.tag(str(clip))
    assert tags is not None
    assert tags.labels == [("Dog", 0.7)]


@needs_ffmpeg
@pytest.mark.parametrize("seconds", [1, 2, 3, 5, 9, 20])
async def test_a_sound_is_heard_whatever_the_clip_length(
    seconds: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plenty of Blink clips are well under the classifier's own window.

    The same sound has to be heard in a one-second clip and in a
    twenty-second one. It is not a given: the average level of a clip
    carrying one brief sound falls as the clip gets longer -- the same
    sound measured -59 dBFS across one second and -67 dBFS across nine --
    so a gate reading the average heard short clips and went deaf to long
    ones. Reading the loudest moment instead is what makes the answer
    depend on the sound rather than on the clip's length.
    """
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    module = _fake_transformers([{"label": "Dog", "score": 0.7}])
    monkeypatch.setitem(sys.modules, "transformers", module)

    clip = _make_clip(
        tmp_path / f"len{seconds}.mp4", audio="brief-sound", seconds=seconds
    )
    tagger = AudioTagger()
    assert await tagger.ensure_ready() is True

    tags = await tagger.tag(str(clip))
    assert tags is not None, f"a {seconds}s clip's sound went unheard"
    assert tags.labels == [("Dog", 0.7)]


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
    assert tagger._load_task is not None
    assert not tagger._load_task.done()
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
    assert tags is not None
    assert tags.labels == [("Shout", 0.8)]
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
# The same paths, without ffmpeg
# --------------------------------------------------------------------------
#
# The real-file tests above are what prove ffmpeg still behaves the way this
# stage assumes, and they are skipped wherever ffmpeg is not installed.
# Everything the stage does once ffmpeg has answered -- the silence gate, the
# background model load, the classification, the time budget -- needs no
# ffmpeg to exercise, so these drive it with canned audio and a fake
# subprocess. They keep those branches covered on a machine without ffmpeg
# instead of leaving them to whichever CI image happens to have it.


def _audible(seconds: float = 2.0) -> np.ndarray:
    """A plainly audible tone, well above the silence gate."""
    t = np.arange(int(_SAMPLE_RATE * seconds)) / _SAMPLE_RATE
    return (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _inaudible(seconds: float = 2.0) -> np.ndarray:
    """Digital zero: what a camera with its microphone off still records."""
    return np.zeros(int(_SAMPLE_RATE * seconds), dtype=np.float32)


def _tagger_hearing(
    samples: np.ndarray, monkeypatch: pytest.MonkeyPatch
) -> AudioTagger:
    """A tagger whose extraction yields *samples*, as if ffmpeg had decoded them."""
    tagger = AudioTagger()
    monkeypatch.setattr(tagger, "extract_audio", AsyncMock(return_value=samples))
    return tagger


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, b""), (0, b""), (1, b"\x00" * 8)],
    ids=["no-audio-track", "empty-output", "failed-with-output"],
)
async def test_ffmpeg_finding_no_usable_audio_means_no_tags(
    returncode: int, stdout: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """A clip with no audio track makes ffmpeg exit non-zero with nothing on
    stdout, which is the signal; a non-zero exit is never trusted for its
    output, and a clean exit with nothing decoded has nothing to classify."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        caplog.at_level(logging.DEBUG, logger="blink_downloader.vision.audio"),
    ):
        assert await AudioTagger().extract_audio("/mute.mp4") is None
    assert "No audio track in /mute.mp4" in caplog.text


async def test_a_whole_number_of_samples_is_decoded_exactly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = np.array([0.0, 0.25, -0.5, 1.0], dtype=np.float32)
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(expected.tobytes(), b""))
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        caplog.at_level(logging.WARNING, logger="blink_downloader.vision.audio"),
    ):
        samples = await AudioTagger().extract_audio("/tone.mp4")

    assert samples is not None
    assert samples.dtype == np.float32
    assert np.array_equal(samples, expected)
    assert "mid-sample" not in caplog.text


async def test_a_trailing_partial_sample_is_dropped_and_the_rest_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The complete samples before a torn one are still worth classifying."""
    expected = np.array([0.5, -0.5, 0.25, 0.0], dtype=np.float32)
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(expected.tobytes() + b"\x01\x02", b""))
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        caplog.at_level(logging.WARNING, logger="blink_downloader.vision.audio"),
    ):
        samples = await AudioTagger().extract_audio("/torn.mp4")

    assert samples is not None
    assert np.array_equal(samples, expected)
    assert "ended mid-sample (18 bytes); using the 4 complete samples" in caplog.text


async def test_a_silent_track_is_not_classified_and_says_where_to_look(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    module = _fake_transformers([{"label": "Speech", "score": 0.99}])
    monkeypatch.setitem(sys.modules, "transformers", module)
    tagger = _tagger_hearing(_inaudible(), monkeypatch)

    with caplog.at_level(logging.INFO, logger="blink_downloader.vision.audio"):
        assert await tagger.tag("/silent.mp4") is None

    module.pipeline.assert_not_called()
    # Not even a model load is started for a clip with nothing to classify.
    assert tagger._load_task is None
    # And it names the setting to check, because the cause lives in a
    # different app entirely.
    assert "Blink app" in caplog.text


async def test_the_no_sound_notice_is_said_once_not_per_clip(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every clip from a muted camera is silent, so an unguarded notice would
    be one log line per clip forever; later clips only get the debug line."""
    tagger = _tagger_hearing(_inaudible(), monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="blink_downloader.vision.audio"):
        for _ in range(3):
            assert await tagger.tag("/silent.mp4") is None

    assert caplog.text.count("Blink app") == 1
    assert caplog.text.count("carries no audible sound") == 3
    notice = [r for r in caplog.records if "Blink app" in r.getMessage()]
    assert [r.levelno for r in notice] == [logging.INFO]


async def test_a_loaded_model_is_ready_without_starting_another_load() -> None:
    tagger = AudioTagger()
    tagger._pipe = MagicMock()
    assert tagger._model_ready() is True
    assert tagger._load_task is None


async def test_an_unloaded_model_starts_one_background_load_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first ask kicks off the load and answers "not yet"; asking again
    while it is in flight starts nothing new; once it has finished without
    leaving a model behind (a failed load) the next ask tries again."""
    tagger = AudioTagger()
    release = asyncio.Event()

    async def slow_load() -> bool:
        await release.wait()
        return False

    monkeypatch.setattr(tagger, "ensure_ready", slow_load)

    assert tagger._model_ready() is False
    first = tagger._load_task
    assert first is not None
    assert not first.done()

    assert tagger._model_ready() is False
    assert tagger._load_task is first

    release.set()
    assert await first is False
    assert tagger._model_ready() is False
    assert tagger._load_task is not first
    assert tagger._load_task is not None
    await tagger._load_task  # reaped, so nothing is left pending


async def test_a_clip_is_analyzed_without_audio_while_the_model_loads(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A clip's verdict never waits on the model download."""
    tagger = _tagger_hearing(_audible(), monkeypatch)
    monkeypatch.setattr(tagger, "ensure_ready", AsyncMock(return_value=False))

    with caplog.at_level(logging.DEBUG, logger="blink_downloader.vision.audio"):
        assert await tagger.tag("/tone.mp4") is None

    assert "Audio model not loaded yet, skipping /tone.mp4" in caplog.text
    assert tagger._load_task is not None
    assert await tagger._load_task is False


async def test_tags_audio_filtered_sorted_and_capped(
    monkeypatch: pytest.MonkeyPatch,
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
    tagger = _tagger_hearing(_audible(), monkeypatch)
    assert await tagger.ensure_ready() is True

    tags = await tagger.tag("/tone.mp4")

    assert tags is not None
    assert len(tags.labels) == _MAX_LABELS
    assert [label for label, _ in tags.labels] == ["Shout", "Speech", "Glass"]


async def test_the_model_is_handed_a_writable_16khz_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """frombuffer aliases the bytes ffmpeg wrote, so the decoded track is
    read-only; torch warns on (and can refuse) a non-writable buffer, so the
    window that leaves this module has to be a copy."""
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    seen: dict[str, Any] = {}
    module = MagicMock()

    def build(**_kw):
        def run(inputs, **_call):
            seen.update(inputs)
            return [{"label": "Shout", "score": 0.8}]

        return run

    module.pipeline.side_effect = build
    monkeypatch.setitem(sys.modules, "transformers", module)
    decoded = np.frombuffer(_audible().tobytes(), dtype=np.float32)
    assert not decoded.flags.writeable
    tagger = _tagger_hearing(decoded, monkeypatch)
    assert await tagger.ensure_ready() is True

    assert await tagger.tag("/tone.mp4") is not None

    assert seen["sampling_rate"] == _SAMPLE_RATE
    assert seen["raw"].flags.writeable


async def test_a_classifier_that_fails_gives_no_tags_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
    tagger = _tagger_hearing(_audible(), monkeypatch)
    assert await tagger.ensure_ready() is True

    with caplog.at_level(logging.WARNING, logger="blink_downloader.vision.audio"):
        assert await tagger.tag("/tone.mp4") is None

    assert "Audio classification failed: inference blew up" in caplog.text


async def test_a_stage_that_outlasts_its_budget_is_abandoned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A hard ceiling on what this stage can cost one clip: the verdict goes
    ahead without the hint, and says so."""
    monkeypatch.setattr("blink_downloader.vision.audio._STAGE_BUDGET", 0.05)
    tagger = AudioTagger()

    async def never_returns(_path: str) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(tagger, "_tag", never_returns)

    with caplog.at_level(logging.WARNING, logger="blink_downloader.vision.audio"):
        assert await tagger.tag("/slow.mp4") is None

    assert "exceeded its" in caplog.text
    assert "/slow.mp4" in caplog.text


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
    [None, VisionHints(), VisionHints(audio_tags=AudioTags())],
    ids=["no-pipeline", "stage-off", "nothing-relevant-heard"],
)
def test_nothing_heard_stores_nothing(hints: VisionHints | None) -> None:
    """All three cases mean the same thing to the modal: no sound chips."""
    assert _audio_labels_json(hints) == ""
