"""Stage 7: audio event tagging (Audio Spectrogram Transformer).

The only stage that looks at something other than pixels. A Blink camera
records sound as well as picture, and some of the most useful evidence a
clip carries is not visible: glass going, someone shouting, a car door at
three in the morning, a power tool on a lock. A camera pointed at a gate
cannot see any of that, and the frames the other stages sample are silent.

Two deliberate limits shape this stage, both about privacy rather than
capability:

* **It classifies, it does not transcribe.** The model answers "what kind
  of sound is this" — speech, shouting, breaking glass — and never what
  was said. Recognising that someone raised their voice is security
  evidence; a transcript of the neighbours' conversation drifting past the
  gate is surveillance, and it would leave this add-on's promises about
  local, minimal analysis worse than it found them.
* **It runs on-device.** The classifier is a local transformers model, the
  same way face recognition is local. No audio leaves the add-on, and
  nothing about the sound reaches the configured AI provider except the
  short hint below.

Like every other stage this one is off by default, lazily imports its
dependency, and reports itself unavailable rather than raising. It adds no
new dependency: transformers and torch are already the optional CV extra.

Why an audio stage lives in a package called ``vision``: this package is
the add-on's optional per-clip *evidence* pipeline, and every stage in it
turns raw clip data into a bounded hint the AI provider verifies rather
than re-derives. Audio is another such stage. Renaming the package to
match would churn ``VisionPipeline``/``VisionHints``/``VisionConfig``
across app.py, the analyzer, media_server and the tests for no user-
visible benefit, so the name stays and this note explains it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from ..security.sounds import matches_any
from . import runtime

_LOGGER = logging.getLogger(__name__)

# What the Audio Spectrogram Transformer expects: 16 kHz mono.
_SAMPLE_RATE = 16_000

# Below this RMS the clip is treated as carrying no sound and is skipped
# without troubling the model.
#
# A camera with audio recording switched off in the Blink app still writes
# an audio track; it just carries nothing worth classifying. Measured
# against real files, "nothing" is not reliably digital zero: AAC encodes
# true silence back to exactly 0.0, but a muted or gain-starved microphone
# produces an inaudible noise floor instead, and a -69 dBFS floor sailed
# past the 1e-4 (-80 dBFS) threshold this used to use, so every such clip
# paid for a model inference to be told it heard room tone.
#
# 1e-3 is -60 dBFS, which is below the level at which anything is
# audible at all; a sound worth classifying is an order of magnitude
# above it.
#
# Applied to the *window* that would actually be classified, not to the
# whole track. A two-second sound in a sixty-second clip measures 0.18x
# its own level once averaged over the track but 0.45x over the ten-second
# window — 2.4x higher, and the difference between landing above this
# threshold and below it. Gating on the track average would discard
# exactly the short quiet events worth hearing.
_SILENCE_RMS = 1e-3

# A label has to clear this to be mentioned at all. These are independent
# per-class probabilities (see _ACTIVATION), not shares of one budget, so
# a clip with several things audible at once can have several clear this;
# it is deliberately low, and the hint's wording carries the remaining
# uncertainty rather than the threshold hiding it.
_MIN_CONFIDENCE = 0.15

# At most this many labels reach the prompt. The tail is noise, and a long
# list reads as more certain than it is.
_MAX_LABELS = 3

# How many of the model's own top classes to look at before filtering for
# relevance. AudioSet's highest-scoring classes on a doorbell clip are
# usually ambience ("Inside, small room", "Silence", "Music"), so a short
# candidate list would throw away the one security-relevant label sitting
# below them. Ranking 527 already-computed probabilities is free; running
# the model again is not.
_CANDIDATE_LABELS = 25

# AudioSet is multi-label: a clip really can be Speech *and* Dog *and*
# Vehicle at once, and the checkpoint was trained with that in mind. The
# pipeline nevertheless defaults to softmax, which forces all 527 classes
# to share one probability budget -- co-occurring sounds then suppress
# each other, and every score comes out far below what the model actually
# asserts. Sigmoid is what this family of checkpoints is scored with.
_ACTIVATION = "sigmoid"

# Decoding audio alone is far quicker than the frame extraction that
# already runs per clip, but a corrupt file can still hang ffmpeg, and
# nothing else would ever time this out. Deliberately well under
# _STAGE_BUDGET so ffmpeg's own handler fires first and reaps the child,
# rather than the budget cancelling mid-communicate() and leaving it.
_FFMPEG_TIMEOUT = 20

# The most this stage may add to one clip's time-to-verdict. Nothing here
# is on the critical path by right: the audio hint is supporting evidence,
# and a verdict that arrives without it is worth far more than one that
# arrives late. Generous enough that a healthy decode-plus-inference never
# comes near it (both are seconds), tight enough that a stalled machine or
# a contended _cv_slot cannot turn one clip into a backlog.
_STAGE_BUDGET = 45

# How much audio to classify. AST expects ~10s and pads or truncates to
# its own window; handing it a whole 60s clip means everything after the
# first window is discarded silently, so the loudest window is chosen
# instead of the first (see _loudest_window).
_WINDOW_SECONDS = 10

# Keywords that make an AudioSet label worth reporting, matched on word
# boundaries and case-insensitively.
#
# Keyword matching rather than an exact label allow-list on purpose: the
# exact class strings differ between AudioSet checkpoints ("Gunshot,
# gunfire", "Walk, footsteps", "Smoke detector, smoke alarm"), and an
# allow-list pinned to one checkpoint's spelling would silently match
# nothing the day the model id changes. The cost is the occasional
# unhelpful label reaching the prompt, which the hedged wording covers;
# the alternative failure is the whole stage going quiet with no error.
_RELEVANT_KEYWORDS: frozenset[str] = frozenset(
    {
        # People
        "speech",
        "shout",
        "yell",
        "scream",
        "screaming",
        "crying",
        "conversation",
        "whispering",
        "footsteps",
        "walk",
        "run",
        # Forced entry / tampering
        "glass",
        "shatter",
        "break",
        "breaking",
        "smash",
        "door",
        "knock",
        "slam",
        "doorbell",
        "drill",
        "saw",
        "sawing",
        "hammer",
        "tools",
        "power tool",
        "chainsaw",
        "crowbar",
        "pry",
        # Vehicles
        "vehicle",
        "car",
        "truck",
        "motorcycle",
        "engine",
        "skid",
        "horn",
        # Alarms and emergencies
        "alarm",
        "siren",
        "smoke detector",
        "gunshot",
        "gunfire",
        "explosion",
        "fire",
        # Animals, which explain a lot of night-time motion
        "dog",
        "bark",
        "cat",
        "animal",
    }
)


@dataclass
class AudioTags:
    """What the audio of one clip was classified as.

    Only ever produced for a clip whose audio was actually classified. No
    track, nothing audible, or a classifier that could not run are all
    ``None`` from :meth:`AudioTagger.tag` instead — they differ in why,
    which the log says, but not in what any caller should then do.
    """

    #: (label, confidence) pairs, most confident first, already filtered.
    labels: list[tuple[str, float]] = field(default_factory=list)

    @property
    def any_tags(self) -> bool:
        return bool(self.labels)


def is_relevant_label(label: str) -> bool:
    """True when *label* names a sound worth putting in front of the AI.

    The matcher itself lives in ``security.sounds`` rather than here:
    that module has to do the same word-boundary matching to decide which
    sounds raise an event, and two copies of the same subtle regex is how
    a fix lands in one of them only. Same reasoning, and the same
    direction, as ``frame_motion`` taking ``point_in_polygon`` from
    ``security.geometry``.
    """
    return matches_any(label, _RELEVANT_KEYWORDS)


def _rms(samples: Any) -> float:
    """Root-mean-square level of a float32 sample array."""
    if samples.size == 0:
        return 0.0
    return float((samples.astype("float64") ** 2).mean() ** 0.5)


def _loudest_window(samples: Any, sample_rate: int = _SAMPLE_RATE) -> Any:
    """The _WINDOW_SECONDS stretch carrying the most energy.

    A 60-second clip is mostly the quiet before and after whatever
    triggered it. AST classifies one window and pads or truncates to fit,
    so handing it the *first* ten seconds usually classifies the silence
    that preceded the event. Stepping through in half-windows and keeping
    the loudest is a cheap way to land on the part worth looking at.
    """
    window = _WINDOW_SECONDS * sample_rate
    if samples.size <= window:
        return samples
    step = max(window // 2, 1)
    # The final window explicitly, because the stride only lands on the
    # very end when the clip's length happens to divide by it -- otherwise
    # the last few seconds, which is where a clip triggered by a sound
    # often has the sound, would be in no window at all.
    starts = {*range(0, samples.size - window + 1, step), samples.size - window}
    best_start, best_energy = 0, -1.0
    for start in sorted(starts):
        energy = _rms(samples[start : start + window])
        if energy > best_energy:
            best_start, best_energy = start, energy
    return samples[best_start : best_start + window]


def build_audio_hint(tags: AudioTags | None) -> str | None:
    """Render classified audio into an AUDIO prompt hint, or None."""
    if tags is None or not tags.any_tags:
        return None
    described = ", ".join(
        f"{label.lower()} ({score:.0%})" for label, score in tags.labels
    )
    return (
        f"\n\nAUDIO: The clip's sound was automatically classified as: {described}. "
        "This is a sound-classifier's guess from low-quality camera audio, not a "
        "transcript and not a description of what was said — nobody's words were "
        "read. Sounds carry past what the camera can see, so this may describe "
        "something out of frame entirely. Treat it as weak supporting evidence "
        "that has to agree with the frames before it counts for anything."
    )


async def _kill(proc: Any) -> None:
    """Kill and reap an ffmpeg child that outlived its welcome."""
    proc.kill()
    await proc.wait()


class AudioTagger:
    """Audio event classification (AudioSet) via a local transformers model.

    Loaded once per process and reused, like every other stage — the model
    download is the expensive part and a clip's worth of audio is not.
    """

    #: AudioSet-trained Audio Spectrogram Transformer.
    #:
    #: Chosen over the higher-scoring alternatives on purpose, checked
    #: against the Hugging Face hub on 2026-09-21. ``mispeech/ced-base``
    #: and ``saurabhati/DASS_medium_AudioSet_50.2`` both beat AST's 45.9
    #: mAP on AudioSet (about 50), and both ship an ``auto_map``, meaning
    #: they only load under ``trust_remote_code=True`` — arbitrary code
    #: fetched from the hub and executed inside a home-security add-on
    #: that can reach the user's clips and their Home Assistant. A few
    #: points of mAP does not buy that. AST is implemented in
    #: transformers itself, needs no remote code, and is the most
    #: downloaded AudioSet tagger by roughly forty times.
    #:
    #: Overridable via ``ai_audio_model`` so a user who wants one of those
    #: (or a smaller checkpoint) can have it; the keyword matching above
    #: is deliberately tolerant of a different label vocabulary.
    DEFAULT_MODEL_ID = "MIT/ast-finetuned-audioset-10-10-0.4593"

    # Empty default means no token configured; it is not a credential.
    def __init__(self, model_id: str = "", hf_token: str = "") -> None:  # nosec B107
        self._model_id = model_id or self.DEFAULT_MODEL_ID
        self._hf_token = hf_token
        self._pipe: Any = None
        self._lock: asyncio.Lock | None = None
        # The in-flight background load, if any -- see _model_ready.
        self._load_task: asyncio.Task[bool] | None = None
        # A permanently-missing dependency would otherwise log the same
        # warning once per clip forever; likewise a camera recording a
        # silent track, which is every clip it records.
        self._load_failure_logged = False
        self._no_sound_logged = False

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not runtime.torch_cpu_compatible():
            raise runtime.CPUIncompatibleError(runtime._CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, matching every other stage —
        # see runtime._native_import_lock for why the import itself is the
        # part that must not race.
        with runtime._native_import_lock:
            from transformers import pipeline  # type: ignore[import-not-found]

            _LOGGER.info("Loading audio-classification model '%s'", self._model_id)
            self._pipe = pipeline(
                task="audio-classification",
                model=self._model_id,
                device="cpu",
                token=self._hf_token or None,
            )
            _LOGGER.info("Audio-classification model ready")

    async def ensure_ready(self) -> bool:
        if self._pipe is not None:
            return True
        async with self._get_lock():
            if self._pipe is not None:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_sync)
                return True
            except ImportError as exc:
                self._log_load_failure(
                    "transformers package is not installed, audio analysis "
                    "unavailable: %s. Install it with: pip install transformers",
                    exc,
                )
                return False
            except runtime.CPUIncompatibleError as exc:
                self._log_load_failure("Audio analysis unavailable: %s", exc)
                return False
            # Suppressed below because this *is* handled:
            # _log_load_failure logs it, with the traceback on the branch
            # that has no more specific message to give.
            except Exception as exc:  # noqa: BLE001
                if runtime._is_huggingface_auth_error(exc):
                    self._log_load_failure(
                        runtime._HF_AUTH_FAILURE_MESSAGE, "audio-classification"
                    )
                else:
                    self._log_load_failure(
                        "Failed to load audio-classification model: %s",
                        exc,
                        exc_info=True,
                    )
                return False

    def _log_load_failure(
        self, message: str, *args: Any, exc_info: bool = False
    ) -> None:
        """Say why the model would not load, but only make noise once.

        Retrying per clip is right -- an interrupted download resumes --
        but the common cause is a dependency that will never appear, and
        one warning per clip forever would bury everything else in the log.
        """
        if self._load_failure_logged:
            _LOGGER.debug(message, *args, exc_info=exc_info)
            return
        self._load_failure_logged = True
        _LOGGER.warning(message, *args, exc_info=exc_info)

    def _note_no_sound(self, clip_path: str) -> None:
        """Say once that audio analysis is on but the clips carry no sound.

        The likeliest cause is a setting in a different application
        entirely, so the log has to name it: a user who enables audio
        analysis and never sees a sound chip has no other way to find out
        that their camera is recording a silent track.
        """
        if self._no_sound_logged:
            _LOGGER.debug("Audio track in %s carries no audible sound", clip_path)
            return
        self._no_sound_logged = True
        _LOGGER.info(
            "Audio analysis is enabled, but %s carries no audible sound. A "
            "camera with audio recording switched off in the Blink app still "
            "records a silent track, which is indistinguishable from a quiet "
            "scene and cannot be classified — check that setting if no clip "
            "ever reports a sound.",
            clip_path,
        )

    def _model_ready(self) -> bool:
        """Whether the model can classify *right now* -- never waits for it.

        The first load pulls a few hundred MB, and a clip's verdict must
        not sit behind that download: the load runs as a background task
        and the clips analyzed while it is in flight simply get no audio
        hint. Later clips pick it up once it is resident, which is the
        steady state for every clip after the first few.
        """
        if self._pipe is not None:
            return True
        if self._load_task is None or self._load_task.done():
            self._load_task = asyncio.create_task(self.ensure_ready())
        return False

    async def extract_audio(self, clip_path: str) -> Any:
        """Decode a clip's audio to 16 kHz mono float32, or None.

        One ffmpeg call does both jobs: a clip with no audio track at all
        makes ffmpeg exit non-zero with empty output ("Output file does not
        contain any stream"), which is exactly the signal needed and saves
        a separate ffprobe. Verified against real files with an audio
        track, with a silent track, and with none.
        """
        import numpy as np

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                clip_path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(_SAMPLE_RATE),
                "-f",
                "f32le",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            _LOGGER.warning("ffmpeg is not installed, audio analysis unavailable")
            return None
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not read audio from %s: %s", clip_path, exc)
            return None

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_FFMPEG_TIMEOUT
            )
        except TimeoutError:
            # Same handling as extract_frames: a timed-out communicate()
            # leaves the child running, so kill and reap it rather than
            # leaving an orphan behind for every clip.
            _LOGGER.warning("ffmpeg timed out reading audio from %s", clip_path)
            await _kill(proc)
            return None
        except asyncio.CancelledError:
            # _STAGE_BUDGET expired around us. Same reasoning as the
            # timeout above -- the child has to go with us.
            await _kill(proc)
            raise

        if proc.returncode != 0 or not stdout:
            # The common, uninteresting case: this camera records no audio,
            # or the user switched the microphone off in the Blink app.
            _LOGGER.debug("No audio track in %s", clip_path)
            return None
        # A clean f32le stream is a whole number of 4-byte samples.
        # ffmpeg killed mid-write, a disk filling up, or a container it
        # only partly understood can all leave a trailing partial sample,
        # and frombuffer raises on that rather than truncating -- which
        # would take the whole clip's analysis down over an optional hint.
        usable = len(stdout) - (len(stdout) % 4)
        if usable != len(stdout):
            _LOGGER.warning(
                "Audio from %s ended mid-sample (%d bytes); using the %d "
                "complete samples before it",
                clip_path,
                len(stdout),
                usable // 4,
            )
        if usable == 0:
            return None
        # frombuffer aliases the bytes object, so the result is read-only.
        # It is only ever read here, but the array is handed to torch
        # further down, which warns on (and can refuse) a non-writable
        # buffer -- so the one window that actually leaves this module is
        # copied in tag(), not the whole track.
        return np.frombuffer(stdout[:usable], dtype=np.float32)

    def _classify_sync(self, samples: Any) -> list[tuple[str, float]]:
        results = self._pipe(
            {"raw": samples, "sampling_rate": _SAMPLE_RATE},
            top_k=_CANDIDATE_LABELS,
            function_to_apply=_ACTIVATION,
        )
        scored = [
            (str(item["label"]), float(item["score"]))
            for item in results
            if float(item.get("score", 0.0)) >= _MIN_CONFIDENCE
            and is_relevant_label(str(item.get("label", "")))
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:_MAX_LABELS]

    async def tag(self, clip_path: str) -> AudioTags | None:
        """Classify one clip's audio, or None when there is nothing to say.

        Hard-bounded by :data:`_STAGE_BUDGET`. This stage is supporting
        evidence, so it is never allowed to be the reason a verdict is
        late: if it cannot finish in time the clip is analyzed without an
        audio hint, and the next one tries again.
        """
        try:
            return await asyncio.wait_for(self._tag(clip_path), timeout=_STAGE_BUDGET)
        except TimeoutError:
            _LOGGER.warning(
                "Audio analysis exceeded its %ds budget for %s; analyzing the "
                "clip without it rather than holding up the verdict",
                _STAGE_BUDGET,
                clip_path,
            )
            return None
        except Exception:
            # The stage boundary. vision/pipeline.py deliberately wraps
            # nothing -- each stage contains its own failures, the way
            # depth and contact do -- so anything that escapes here would
            # fail a perfectly analyzable clip over an optional hint.
            # Logged with a traceback because, unlike a missing optional
            # dependency, reaching this is a defect in our own code.
            _LOGGER.exception(
                "Audio analysis failed for %s; analyzing the clip without it",
                clip_path,
            )
            return None

    async def _tag(self, clip_path: str) -> AudioTags | None:
        samples = await self.extract_audio(clip_path)
        if samples is None:
            return None
        window = _loudest_window(samples)
        if _rms(window) < _SILENCE_RMS:
            self._note_no_sound(clip_path)
            return None
        if not self._model_ready():
            _LOGGER.debug("Audio model not loaded yet, skipping %s", clip_path)
            return None

        async with runtime._cv_slot():
            try:
                loop = asyncio.get_running_loop()
                labels = await loop.run_in_executor(
                    None, self._classify_sync, window.copy()
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Audio classification failed: %s", exc)
                return None
        return AudioTags(labels=labels)
