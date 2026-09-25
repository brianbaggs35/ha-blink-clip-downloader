"""The orchestrator: run the enabled stages and collect their hints.

:class:`VisionConfig` says which stages are on, :class:`VisionHints`
carries what they produced, and :class:`VisionPipeline` sequences them over
a clip's frames — including the parts that are not any single stage's job:
choosing which frames to scan, resolving *which* vehicle in frame is the
protected one, and learning that camera's vehicle signature over time.

Every stage is optional and every one fails soft. A stage that is disabled,
whose dependency is missing, or that simply finds nothing relevant adds its
name to ``unavailable_sources`` and analysis continues with whatever hints
did arrive — the AI provider still makes the final call either way.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..ffmpeg_output import ANALYSIS_FRAME_WIDTH, extract_jpeg_frames
from ..security import (
    PERSON_LABEL,
    VEHICLE_LABELS,
    Box,
    DetectorThresholds,
    ObjectTrack,
    ProtectedAsset,
    Zone,
    box_gap,
    build_marked_asset,
    build_tracks,
    resolve_vehicle_asset,
)
from ..security.geometry import GROUND_DEPTH_WEIGHT, box_foot_point
from ..security.tracks import _ZONE_BOX_OVERLAP
from ..security.vehicles import VehicleSignature
from . import imaging
from .audio import AudioTagger, AudioTags, build_audio_hint
from .contact import ContactSegmenter, _build_contact_hint
from .depth import DepthEstimator, _build_depth_hint
from .detection import (
    _SUBJECT_CLASSES,
    DetectedObject,
    ObjectDetector,
    _best_subject_vehicle_pair,
    _build_detection_hint,
    _build_tracking_hint,
    _car_zone_reference,
    _ZoneReference,
)
from .enhance import FrameEnhancer
from .faces import (
    FaceEmbedder,
    FaceRecognitionResult,
    FaceRecognizer,
    _build_recognition_hint,
)
from .pose import PoseEstimator, PostureResult, _build_posture_hint

if TYPE_CHECKING:
    from ..database import ClipDatabase

_LOGGER = logging.getLogger(__name__)


def _in_front_gap(subject: Box, asset: Box) -> float:
    """How far from the asset a subject stands, counting only feet *in front
    of* its ground line.

    :func:`~blink_downloader.security.geometry.ground_gap` with the one
    change that matters for choosing whom to examine: feet the asset hides
    (someone at its far side) or feet up on it (a dog on the bonnet) are
    consistent with being at it, where feet well in front of it — lower in
    the frame, nearer the camera — are what a passer-by has.
    """
    fx, fy = box_foot_point(subject)
    dx = max(asset[0] - fx, fx - asset[2], 0.0)
    dy = max(0.0, fy - asset[3]) * GROUND_DEPTH_WEIGHT
    return math.hypot(dx, dy)


#: How close, in feet, a subject has to stand to count as *at* the asset
#: when choosing whom the pair stages examine — the security layer's own
#: "near" distance, so both agree on who was at the car.
_AT_ASSET_FEET = DetectorThresholds().near_feet

#: Furthest, in feet, a subject may be from a marked asset for the pair
#: stages to be pointed at them when nobody is actually at one — the
#: security layer's own "approached the asset" distance. Beyond it the
#: stages would be measuring a passer-by, and have nothing to add.
_NEAR_MARKED_FEET = DetectorThresholds().approach_feet

#: One chosen pair for the depth/contact/pose stages: the subject's sighting,
#: the asset's box, which scan frame, the subject's track id, and which
#: marked asset it was (``None`` for the protected vehicle or the legacy
#: nearest-vehicle pair).
_Pair = tuple[DetectedObject, Box, int, int | None, str | None]


@dataclass
class VisionConfig:
    """Which optional computer-vision stages are enabled (see config.py).

    ``enhanced_detection_enabled`` gates frame preprocessing, object
    detection/tracking, depth estimation, and contact segmentation together
    — depth/segmentation have always required detection to run at all, so
    these four stages are one on/off setting rather than four independently
    toggleable ones. Face recognition remains separate since it's a
    privacy-sensitive, not just heavier-compute, feature.
    """

    enhanced_detection_enabled: bool = False
    object_detection_model: str = "yolo26n.pt"
    depth_estimation_model: str = "depth-anything/Depth-Anything-V2-Small-hf"
    face_recognition_enabled: bool = False
    #: Width of the frames faces are matched in — ``ai_face_recognition_resolution``
    #: mapped through :data:`.faces.FACE_RESOLUTION_WIDTHS`. At the analysis
    #: width the face stage reuses the frames already extracted; any other
    #: width re-extracts the same moments at that size.
    face_frame_width: int = ANALYSIS_FRAME_WIDTH
    #: Body-keypoint estimation for the subject nearest a protected asset.
    #: Its own toggle, and its own (separate, small) model checkpoint — a
    #: user who wants object detection should not pay for a download they
    #: will never use.
    pose_estimation_enabled: bool = False
    pose_model: str = "yolo26n-pose.pt"
    #: Sound-event classification of the clip's audio track. Its own toggle
    #: for the same reason pose has one — a separate model download — and
    #: because it is the one stage that looks at something other than
    #: pixels, so a user may want the picture analysed and the sound left
    #: alone. It classifies sounds and never transcribes speech; see
    #: :mod:`.audio`.
    audio_analysis_enabled: bool = False
    audio_model: str = ""
    hf_token: str = ""
    #: How many evenly-spaced frames the temporal scan runs detection over
    #: (see :func:`imaging._select_scan_frames`). This is the single knob that trades
    #: detection cost against how much of the clip's *behaviour* — dwell,
    #: approach, retreat — the security layer can see.
    temporal_scan_frames: int = 12
    #: Whether the structured security layer runs at all. Costs no extra
    #: model inference when detection is already on, so it defaults to
    #: enabled; turning it off falls back to the plain prompt hints.
    security_events_enabled: bool = True


#: Names of the optional evidence sources, as reported in
#: ``VisionHints.unavailable_sources`` and rendered verbatim into the
#: prompt's "Evidence sources unavailable for this clip" line. Named
#: constants rather than repeated literals because they are compared and
#: counted downstream — ``security.evidence.assess_evidence`` scores stage
#: coverage from the length of that list, so a typo here would silently
#: change an evidence-quality score rather than fail.
SOURCE_OBJECT_DETECTION = "object detection"


SOURCE_DEPTH_ESTIMATION = "depth estimation"


SOURCE_CONTACT_SEGMENTATION = "contact segmentation"


SOURCE_POSE_ESTIMATION = "pose estimation"


SOURCE_FACE_RECOGNITION = "face recognition"


@dataclass
class VisionHints:
    """Per-clip output of :meth:`VisionPipeline.process_clip`."""

    enhanced_frames: list[bytes] | None = None
    detection_hint: str | None = None
    tracking_hint: str | None = None
    depth_hint: str | None = None
    contact_hint: str | None = None
    recognized_resident_hint: str | None = None
    posture_hint: str | None = None
    audio_hint: str | None = None
    # The sounds behind audio_hint above, kept structured for the same
    # reason detections are: the clip modal shows a user what evidence the
    # analysis actually had, and a rendered prompt string cannot be
    # un-rendered back into chips.
    audio_tags: AudioTags | None = None
    face_recognition: FaceRecognitionResult | None = None
    # Raw per-object detections from ObjectDetector, kept alongside the
    # rendered detection_hint text above so callers (the analyzer) can
    # persist structured results — see database.py's detected_objects
    # table — rather than only ever having the flattened prompt string.
    detections: list[DetectedObject] | None = None

    # --- structured security evidence (see blink_downloader.security) ---
    # Everything below is raw material for the deterministic event
    # detector, which the analyzer runs: this module's job ends at producing
    # measurements, and the rules that interpret them live where they can
    # be tested without torch installed.
    tracks: list[ObjectTrack] | None = None
    asset: ProtectedAsset | None = None
    frame_size: tuple[float, float] | None = None
    #: Frames the temporal scan actually covered, and the real seconds
    #: between them — the security layer's whole sense of time.
    scan_frame_count: int = 0
    scan_interval: float = 0.0
    #: Which track the depth/contact stages examined, so their verdicts are
    #: attributed to the right subject rather than to whoever else was in
    #: frame.
    contact_track_id: int | None = None
    depth_similar: bool | None = None
    contact_touching: bool | None = None
    #: How much the protected asset's own image region changed between the
    #: start and end of the clip (see :func:`imaging._region_appearance_change`).
    asset_appearance_change: float | None = None
    #: What the subject's body was doing at the closest moment, when pose
    #: estimation is enabled and found them.
    posture: PostureResult | None = None
    #: Optional stages that produced nothing for this clip, named so the
    #: final result can say which evidence was missing instead of quietly
    #: concluding without it.
    unavailable_sources: list[str] = field(default_factory=list)
    #: Stages that had nothing in *this clip* to measure — as opposed to
    #: being switched off, missing a dependency, or failing. Kept apart from
    #: unavailable_sources because the two mean opposite things to anything
    #: reading them: a stage that could not run is missing evidence, while a
    #: stage with no question to answer is not. Conflating them told the AI
    #: that depth, contact and pose evidence was "unavailable" on every clip
    #: with no person-and-vehicle pair in it — most clips — and docked the
    #: evidence-quality score for it.
    not_applicable_sources: list[str] = field(default_factory=list)
    #: An updated learned vehicle signature to persist, set only when this
    #: clip identified the protected vehicle confidently enough to learn
    #: from (see :mod:`blink_downloader.security.vehicles`).
    vehicle_signature_update: VehicleSignature | None = None
    #: Assets marked on the Assets tab for this camera, located by their
    #: zones (see :func:`~blink_downloader.security.build_marked_asset`).
    marked_assets: list[ProtectedAsset] = field(default_factory=list)
    #: Which marked asset the depth/contact/pose stages examined, by key —
    #: ``None`` when they examined the protected vehicle, or nothing.
    examined_asset_key: str | None = None
    #: How much each marked asset's own region changed between the first and
    #: last scan frames, by key, for assets nobody stood in front of in either.
    marked_asset_changes: dict[str, float] = field(default_factory=dict)


class VisionPipeline:
    """Orchestrates the optional computer-vision stages for one clip.

    Each stage is independently toggleable via *config* and gracefully
    no-ops (leaving the corresponding hint unset) if its dependency isn't
    installed, fails to load, or simply finds nothing relevant — analysis
    always proceeds using whatever hints ended up available, exactly like
    the existing scene-baseline/zone-motion hints in ``analyzer/base.py``.
    """

    def __init__(self, config: VisionConfig, db: ClipDatabase | None = None) -> None:
        self._config = config
        self._db = db
        self._detector = ObjectDetector(config.object_detection_model)
        self._depth = DepthEstimator(config.hf_token, config.depth_estimation_model)
        self._segmenter = ContactSegmenter(config.hf_token)
        self._pose = PoseEstimator(config.pose_model)
        self._face_embedder = FaceEmbedder()
        self._audio = AudioTagger(config.audio_model, config.hf_token)

    @property
    def tracks_subjects(self) -> bool:
        """True when this pipeline turns clips into tracked subjects for the
        security layer — object detection on, and the layer itself not
        switched off. Without it, marked assets and the protected vehicle
        still shape the prompt, but no per-asset rule can run."""
        return (
            self._config.enhanced_detection_enabled
            and self._config.security_events_enabled
        )

    async def process_clip(
        self,
        frames: list[bytes],
        car_description: str = "",
        car_protection_applies: bool = False,
        raw_frames: list[bytes] | None = None,
        car_zone: dict[str, Any] | None = None,
        camera: str = "",
        frame_interval: float = 2.0,
        vehicle_signature: VehicleSignature | None = None,
        clip_path: str = "",
        marked_assets: list[dict[str, Any]] | None = None,
    ) -> VisionHints:
        """Run every enabled stage and return this clip's hints and evidence.

        *frames* are the handful already down-selected for the AI prompt.
        *raw_frames*, when given, is the full pre-down-selection extraction
        pool, and is what the temporal scan and face recognition both work
        from — for different reasons that happen to point the same way.
        Face recognition needs the widest possible pool because the clearest
        view of a face is often a *low*-motion moment the down-selection
        drops; the temporal scan needs it because down-selected frames are
        deliberately unevenly spaced, which makes every duration and speed
        derived from them wrong. See :func:`imaging._select_scan_frames`.

        *car_protection_applies* must reflect whether protected-vehicle
        rules apply to *this specific camera* (see
        ``BaseAnalyzer._car_protection_applies``) — not merely whether a
        protected vehicle is described somewhere on the property. Every
        vehicle-related stage is skipped entirely when False, so a camera
        that doesn't view the protected vehicle never generates vehicle
        evidence just because it happened to see a car.

        *car_zone*, *vehicle_signature* and the colour fingerprints computed
        below are the three pieces of evidence that decide *which* detected
        vehicle is the protected one — see
        :func:`~blink_downloader.security.vehicles.identify_protected_vehicle`.

        *marked_assets* are this camera's enabled entries from the Assets tab
        (see :mod:`blink_downloader.protected_assets`). They need no
        detection of their own — each is placed by its zone — but they give
        the pair stages somewhere else worth looking when someone is at the
        front door rather than the car.
        """
        hints = VisionHints()
        if not frames:
            return hints

        # Face recognition must always match against raw (pre-enhancement)
        # frames, since enrolled reference embeddings are computed from raw
        # reference photos — matching enhanced frames against raw references
        # is an embedding-space mismatch that can cause real matches (approved
        # household members) to be missed. Captured before `frames` is
        # potentially reassigned to CLAHE-enhanced frames below.
        raw_pool = raw_frames if raw_frames else frames

        if self._config.enhanced_detection_enabled:
            await self._run_detection_stages(
                hints,
                frames,
                raw_pool,
                camera=camera,
                car_description=car_description if car_protection_applies else "",
                car_zone=car_zone if car_protection_applies else None,
                frame_interval=frame_interval,
                vehicle_signature=vehicle_signature,
                marked_assets=marked_assets or [],
            )
        else:
            hints.unavailable_sources.append(SOURCE_OBJECT_DETECTION)
            hints.unavailable_sources.append(SOURCE_DEPTH_ESTIMATION)
            hints.unavailable_sources.append(SOURCE_CONTACT_SEGMENTATION)
            hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)

        await self._run_audio_stage(hints, clip_path)
        await self._run_face_stage(hints, raw_pool, clip_path, frame_interval)
        self._log_result(hints)
        return hints

    async def _run_audio_stage(self, hints: VisionHints, clip_path: str) -> None:
        """Classify the clip's sound, when the stage is on and there is a file.

        The only stage that reads the file rather than the frames, so the
        only one that needs a path. A clip with no audio track -- the
        camera's microphone switched off, or a model without one -- simply
        produces no hint, which is why this cannot be inferred from the
        frames the other stages already hold.

        Deliberately *not* recorded in ``unavailable_sources`` when it
        finds nothing. That list feeds evidence.py's stage-coverage score,
        whose denominator (``TOTAL_OPTIONAL_SOURCES``) counts the four
        *visual* stages it was calibrated against. Appending a fifth,
        non-visual source would knock 25 points off the coverage of every
        clip that simply had nothing audible in it -- which is most of
        them -- and would do it only for users who turned audio on.
        Silence is not missing evidence about what the camera saw.
        """
        if not (self._config.audio_analysis_enabled and clip_path):
            return
        hints.audio_tags = await self._audio.tag(clip_path)
        hints.audio_hint = build_audio_hint(hints.audio_tags)

    async def _run_face_stage(
        self,
        hints: VisionHints,
        raw_pool: list[bytes],
        clip_path: str,
        frame_interval: float,
    ) -> None:
        """Recognize enrolled household members, or record the stage as absent."""
        if not (self._config.face_recognition_enabled and self._db is not None):
            hints.unavailable_sources.append(SOURCE_FACE_RECOGNITION)
            return
        frames = await self._face_frames(raw_pool, clip_path, frame_interval)
        recognizer = FaceRecognizer(self._face_embedder, self._db)
        face_result = await recognizer.recognize(frames)
        hints.face_recognition = face_result
        hints.recognized_resident_hint = _build_recognition_hint(face_result)

    async def _face_frames(
        self, raw_pool: list[bytes], clip_path: str, frame_interval: float
    ) -> list[bytes]:
        """The frames faces are matched in.

        *raw_pool* itself at the analysis width. At a wider configured
        width, the same moments of the clip — same interval, same count, so
        the same ``fps`` sampling lands on the same timestamps — extracted
        again at that size. If that extraction fails, *raw_pool* again: a
        smaller face than the enrolled photos were captured at can only
        match less often, never match the wrong person, so falling back
        errs the safe way.
        """
        width = self._config.face_frame_width
        if width == ANALYSIS_FRAME_WIDTH or not clip_path or not raw_pool:
            return raw_pool
        frames = await extract_jpeg_frames(
            clip_path,
            width=width,
            interval=frame_interval,
            count=len(raw_pool),
            label=clip_path,
        )
        return frames or raw_pool

    def _log_result(self, hints: VisionHints) -> None:
        """One debug line per clip saying which stages produced anything.

        The only other evidence any of this ran is the one-time "model
        ready" INFO each stage prints on its first load — after that every
        stage runs, and produces real hints that do reach the AI prompt,
        completely silently. Names are deliberately never logged, matching
        ``_build_recognition_hint``'s own name-free prompt guarantee: only
        counts, mirroring what the prompt itself is allowed to say.
        """
        faces = hints.face_recognition
        _LOGGER.debug(
            "Vision pipeline result: enhanced_detection=%s "
            "(detection=%r, tracking=%r, depth=%r, contact=%r, tracks=%d, "
            "scan_frames=%d, marked_assets=%d, examined_asset=%s), audio=%s, "
            "face_recognition=%s (approved=%d, other=%d, unrecognized_present=%s)",
            self._config.enhanced_detection_enabled,
            hints.detection_hint,
            hints.tracking_hint,
            hints.depth_hint,
            hints.contact_hint,
            len(hints.tracks or []),
            hints.scan_frame_count,
            len(hints.marked_assets),
            hints.examined_asset_key or "-",
            len(hints.audio_tags.labels) if hints.audio_tags else 0,
            self._config.face_recognition_enabled,
            len(faces.approved_names) if faces else 0,
            len(faces.other_names) if faces else 0,
            faces.unrecognized_present if faces else False,
        )

    async def _run_detection_stages(
        self,
        hints: VisionHints,
        frames: list[bytes],
        raw_pool: list[bytes],
        camera: str,
        car_description: str,
        car_zone: dict[str, Any] | None,
        frame_interval: float,
        vehicle_signature: VehicleSignature | None,
        marked_assets: list[dict[str, Any]],
    ) -> None:
        """Frame preprocessing, detection, tracking, vehicle identification,
        and locating the camera's marked assets."""
        # Enhancement exists for the *prompt* images: CLAHE lifts a dark
        # night frame into something a vision-language model can read.
        hints.enhanced_frames = FrameEnhancer.enhance(frames)

        # `0` means "no wider scan" (see DOCS.md's ai_temporal_scan_frames):
        # detect over the frames the prompt already uses, not over the whole
        # raw extraction, which would be the most expensive setting there is
        # rather than the cheapest one the option promises.
        cap = self._config.temporal_scan_frames
        selected, scan_interval = imaging._select_scan_frames(
            raw_pool if cap > 0 else frames, cap, frame_interval
        )
        # Every stage below is a *model*, and every one of them gets the raw
        # frames — the same rule process_clip already applies to face
        # recognition, and for the same underlying reason: these models were
        # trained on ordinary photographs, and CLAHE plus non-local-means
        # denoising is not one. Scan frames used to be enhanced, and it cost
        # real accuracy rather than buying any: measured over COCO128 at the
        # thresholds this pipeline actually uses, enhancement lost real
        # detections on every model tried and came out behind on F1 every
        # time, whichever way its false-positive count moved. On a dark
        # driveway it is worse than the averages suggest, because local
        # contrast normalisation invents structure exactly where there is
        # least real signal — a house reported as a "truck", a second
        # "person" beside the only one present, a "cat" in a clip with no
        # animal in it, all of which disappear on the unenhanced frame.
        # Dropping it here also stops the denoiser, by far the most
        # expensive thing this module does without a model behind it, from
        # running over the whole scan set at all.
        scan_frames = selected
        hints.scan_frame_count = len(scan_frames)
        hints.scan_interval = scan_interval

        detections = await self._detector.detect(scan_frames)
        hints.detections = detections
        if not detections:
            if detections is None:
                hints.unavailable_sources.append(SOURCE_OBJECT_DETECTION)
                # The detector itself could not run, so the stages that
                # work from its boxes could not either.
                hints.unavailable_sources.append(SOURCE_DEPTH_ESTIMATION)
                hints.unavailable_sources.append(SOURCE_CONTACT_SEGMENTATION)
                hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)
            else:
                # Ran and found nothing, which is not the same as not having
                # run — only the former is evidence, and conflating the two
                # would tell the model "nobody was here" on a clip the
                # detector never actually looked at.
                hints.detection_hint = _build_detection_hint([], car_description)
                # Same distinction one level down: with no boxes at all
                # there is nothing for depth, contact or pose to measure.
                # That is an empty clip, not missing evidence.
                hints.not_applicable_sources.append(SOURCE_DEPTH_ESTIMATION)
                hints.not_applicable_sources.append(SOURCE_CONTACT_SEGMENTATION)
                hints.not_applicable_sources.append(SOURCE_POSE_ESTIMATION)
            return

        # A frame that won't decode costs the security layer its geometry,
        # but the object-detection and tracking hints below are built from
        # the detections alone and stay just as valid — returning early here
        # would drop them for no reason.
        frame_size = imaging._frame_dimensions(scan_frames[0])
        hints.frame_size = (
            (float(frame_size[0]), float(frame_size[1])) if frame_size else None
        )

        if self._config.security_events_enabled and hints.frame_size is not None:
            hints.tracks = build_tracks(
                [
                    (d.label, d.confidence, d.box, d.track_id, d.frame_index)
                    for d in detections
                ],
                scan_interval,
                hints.frame_size,
            )
            hints.asset = self._resolve_asset(
                hints, scan_frames, camera, car_description, car_zone, vehicle_signature
            )
            hints.marked_assets = self._locate_marked_assets(
                camera, marked_assets, hints.frame_size
            )

        zone_ref = (
            _car_zone_reference(car_zone, scan_frames[0])
            if car_zone and car_description
            else None
        )
        asset_box = hints.asset.box if hints.asset and hints.asset.present else None
        hints.detection_hint = _build_detection_hint(
            detections, car_description, zone_ref, asset_box
        )
        hints.tracking_hint = _build_tracking_hint(detections, len(scan_frames))

        await self._run_pair_stages(hints, scan_frames, detections, zone_ref)
        hints.marked_asset_changes = self._marked_asset_changes(
            scan_frames, hints.marked_assets, detections
        )

    @staticmethod
    def _locate_marked_assets(
        camera: str,
        marked_assets: list[dict[str, Any]],
        frame_size: tuple[float, float],
    ) -> list[ProtectedAsset]:
        """Place each of this camera's marked assets on the scan frames."""
        located: list[ProtectedAsset] = []
        for entry in marked_assets:
            asset = build_marked_asset(
                camera,
                str(entry.get("id", "")),
                str(entry.get("name", "")),
                str(entry.get("asset_type", "")),
                Zone.from_config(entry.get("zone")),
                frame_size,
            )
            if asset is not None:
                located.append(asset)
        return located

    def _marked_asset_changes(
        self,
        scan_frames: list[bytes],
        marked_assets: list[ProtectedAsset],
        detections: list[DetectedObject],
    ) -> dict[str, float]:
        """Before/after appearance change of each marked asset's region.

        Only worth computing when a subject was in the clip at all — the
        event built on it needs somebody to have been at the asset, and a
        change nobody could have caused is the weather. Each comparison
        carries the same clean-view precondition as the vehicle's own (see
        :meth:`_asset_change`), and one that cannot be made — an undecodable
        frame, a missing image library — is simply left out: one fewer
        input for one asset, never a failed clip.
        """
        if len(scan_frames) < 2 or not marked_assets:
            return {}
        if not any(d.label in _SUBJECT_CLASSES for d in detections):
            return {}
        changes: dict[str, float] = {}
        for asset in marked_assets:
            # build_marked_asset only returns assets with a box.
            assert asset.box is not None
            try:
                change = self._asset_change(scan_frames, asset.box, detections)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Appearance change failed for asset %r", asset.key)
                continue
            if change is not None:
                changes[asset.key] = change
        return changes

    def _resolve_asset(
        self,
        hints: VisionHints,
        scan_frames: list[bytes],
        camera: str,
        car_description: str,
        car_zone: dict[str, Any] | None,
        vehicle_signature: VehicleSignature | None,
    ) -> ProtectedAsset | None:
        """Work out which detected vehicle, if any, is the protected one."""
        if not car_description or hints.tracks is None or hints.frame_size is None:
            return None

        # A colour fingerprint per candidate vehicle, taken from the frame
        # where the detector saw it most confidently. Only computed when
        # there is a learned signature to compare against — otherwise it is
        # decode work whose result nothing would read.
        histograms: dict[int | None, tuple[float, ...]] = {}
        if vehicle_signature is not None and vehicle_signature.histogram:
            for track in hints.tracks:
                # Untracked vehicles share a track id of None, so a
                # fingerprint stored for one would be handed to all of them
                # (and only the last decode would survive). Skipping them
                # here also saves the decode.
                if track.label not in VEHICLE_LABELS or track.track_id is None:
                    continue
                best = max(track.points, key=lambda pt: pt.confidence)
                if 0 <= best.frame_index < len(scan_frames):
                    histograms[track.track_id] = imaging._vehicle_histogram(
                        scan_frames[best.frame_index], best.box
                    )

        asset = resolve_vehicle_asset(
            camera,
            car_description,
            hints.tracks,
            hints.frame_size,
            zone=Zone.from_config(car_zone),
            signature=vehicle_signature,
            histograms=histograms,
        )
        # resolve_vehicle_asset only declines when it is given no
        # description, and this method returned above if that were the case.
        assert asset is not None
        hints.vehicle_signature_update = self._learn_signature(
            asset, hints, scan_frames, vehicle_signature
        )
        return asset

    @staticmethod
    def _learn_signature(
        asset: ProtectedAsset,
        hints: VisionHints,
        scan_frames: list[bytes],
        current: VehicleSignature | None,
    ) -> VehicleSignature | None:
        """Fold a confident sighting into this camera's learned signature.

        Only confident identifications teach: learning from a guess is how
        a signature drifts onto the neighbour's car and stays there, which
        would make the whole mechanism worse than not having it. The blend
        itself is deliberately slow — see
        :meth:`~blink_downloader.security.vehicles.VehicleSignature.blend`.
        """
        identification = asset.identification
        if (
            identification is None
            or not identification.confident
            or identification.protected is None
            or hints.frame_size is None
            or not scan_frames
        ):
            return None
        protected = identification.protected
        normalized = protected.normalized_box
        # `protected.box` is a median across sightings and belongs to no
        # single frame; cropping it out of frame 0 samples whatever was
        # there then, which for a car that pulls in mid-clip is empty
        # driveway. Use the sighting the detector was surest of — the same
        # choice `_resolve_asset` makes for the fingerprints this one is
        # later compared against, so both are measured the same way.
        # The range check mirrors `_resolve_asset`'s own: the sighting index
        # addresses the scan frames the detector ran over, so a caller
        # holding a shorter list falls back to the median box rather than
        # indexing off the end.
        sample = protected.sample
        if sample is not None and 0 <= sample.frame_index < len(scan_frames):
            crop_frame, crop_box = scan_frames[sample.frame_index], sample.box
        else:
            crop_frame, crop_box = scan_frames[0], protected.box
        histogram = imaging._vehicle_histogram(crop_frame, crop_box)
        if current is None:
            return VehicleSignature.from_observation(normalized, histogram)
        return current.blend(normalized, histogram)

    async def _run_pair_stages(
        self,
        hints: VisionHints,
        scan_frames: list[bytes],
        detections: list[DetectedObject],
        zone_ref: _ZoneReference | None,
    ) -> None:
        """Depth and contact analysis for the subject nearest the asset.

        These are the two stages that can tell "walked past the car" from
        "stood right at it" — a distinction a 2D frame simply does not
        contain — so they are pointed at whichever subject actually came
        closest, and their verdict is tagged with that subject's track id so
        nothing downstream misapplies it to somebody else in frame.
        """
        pair = self._select_pair(hints, detections, zone_ref)
        if pair is None:
            # All three of these answer "how close did this subject get to
            # that vehicle". With no subject, or no vehicle, the question
            # does not arise — so they are not applicable rather than
            # unavailable. This is the common case (a person and no car in
            # frame, or cars and nobody around), and reporting it as missing
            # evidence both misinformed the prompt and cost the clip
            # evidence-quality points it had done nothing to lose.
            hints.not_applicable_sources.append(SOURCE_DEPTH_ESTIMATION)
            hints.not_applicable_sources.append(SOURCE_CONTACT_SEGMENTATION)
            hints.not_applicable_sources.append(SOURCE_POSE_ESTIMATION)
            return
        subject, asset_box, frame_idx, track_id, asset_key = pair
        hints.contact_track_id = track_id
        hints.examined_asset_key = asset_key
        target = self._pair_target(hints, asset_key)

        depth_result = await self._depth.compare(
            scan_frames[frame_idx], subject.box, asset_box
        )
        if depth_result is None:
            hints.unavailable_sources.append(SOURCE_DEPTH_ESTIMATION)
        else:
            hints.depth_similar = depth_result.similar_depth
            hints.depth_hint = _build_depth_hint(depth_result, subject.label, target)

        contact_result = await self._segmenter.check_contact(
            scan_frames[frame_idx], subject.box, asset_box
        )
        if contact_result is None:
            hints.unavailable_sources.append(SOURCE_CONTACT_SEGMENTATION)
        else:
            hints.contact_touching = contact_result.touching
            hints.contact_hint = _build_contact_hint(
                contact_result, subject.label, target
            )

        if self._config.pose_estimation_enabled:
            posture = await self._pose.analyze(
                scan_frames[frame_idx], subject.box, asset_box
            )
            if posture is None:
                hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)
            else:
                hints.posture = posture
                hints.posture_hint = _build_posture_hint(posture, subject.label)
        else:
            hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)

        # Cheap, model-free "did the vehicle itself change" evidence, worth
        # computing exactly when somebody was close enough to change it. Only
        # the vehicle's: it feeds the impact rule, which is the vehicle's
        # alone, and every marked asset gets its own comparison separately
        # (see _marked_asset_changes).
        if asset_key is None:
            hints.asset_appearance_change = self._asset_change(
                scan_frames, asset_box, detections
            )

    @staticmethod
    def _pair_target(hints: VisionHints, asset_key: str | None) -> str:
        """What the pair stages' prompt hints call the thing they measured."""
        if asset_key is not None:
            for asset in hints.marked_assets:
                if asset.key == asset_key:
                    return f'asset marked "{asset.name}"'
        return "vehicle"

    @staticmethod
    def _asset_change(
        scan_frames: list[bytes],
        asset_box: Box,
        detections: list[DetectedObject],
    ) -> float | None:
        """Compare the asset's own region between the clip's ends.

        Skipped entirely when a subject overlaps the asset in either of the
        two frames being compared: the region would then be showing a person
        rather than the vehicle, and "it looks different" would be measuring
        where they happened to be standing. Since this feeds the one
        CRITICAL event the detector can emit, a clean view on both ends is
        the right precondition — and its absence means one fewer input, not
        a wrong one.
        """
        last_index = len(scan_frames) - 1
        for detection in detections:
            if (
                detection.frame_index in (0, last_index)
                and detection.label in _SUBJECT_CLASSES
                and box_gap(detection.box, asset_box) <= 0
            ):
                return None
        return imaging._region_appearance_change(
            scan_frames[0], scan_frames[last_index], asset_box
        )

    @staticmethod
    def _select_pair(
        hints: VisionHints,
        detections: list[DetectedObject],
        zone_ref: _ZoneReference | None,
    ) -> _Pair | None:
        """Pick the subject/asset box pair the heavy stages should examine.

        Prefers the identified protected vehicle, so depth and contact are
        measured against the right car rather than against whichever one a
        subject happened to stand nearest. Falls back to the legacy
        nearest-pair search when the security layer is switched off or found
        no asset.

        A camera with marked assets adds one more place worth looking. The
        stages examine one pair per clip, so the order is what someone is
        *at*: a person at the car, then a person at a marked asset, then any
        subject at either, and only then whoever is nearest the car — so a
        visitor trying the front door is not left unexamined because a
        parked car across the frame was nearer the camera's idea of "the
        asset". On a camera with nothing marked this is exactly the old
        choice.
        """
        asset = hints.asset
        vehicle = asset if asset is not None and asset.present and asset.box else None
        marked = hints.marked_assets
        if vehicle is None and not marked:
            legacy = _best_subject_vehicle_pair(detections, zone_ref)
            if legacy is None:
                return None
            subject, legacy_vehicle, frame_idx = legacy
            return (subject, legacy_vehicle.box, frame_idx, subject.track_id, None)

        subjects = [d for d in detections if d.label in _SUBJECT_CLASSES]
        if not subjects:
            return None
        people = [d for d in subjects if d.label == PERSON_LABEL]
        frame_size = hints.frame_size

        for candidates in (people, subjects):
            if vehicle is not None and any(
                _is_at(d, vehicle, frame_size) for d in candidates
            ):
                return _vehicle_pair(vehicle, subjects)
            chosen = _marked_pair_at(marked, candidates, frame_size)
            if chosen is not None:
                return _examine(chosen[0], chosen[1], subjects)
        if vehicle is not None:
            return _vehicle_pair(vehicle, subjects)
        near = _marked_pair_near(marked, subjects)
        if near is None:
            return None
        return _examine(near[0], near[1], subjects)


def _feet_from(subject: DetectedObject, asset: ProtectedAsset) -> float | None:
    """How far *subject* stands in front of *asset*, in feet — measured to
    where someone at it would stand, which for a window is the ground below
    it (see ``ProtectedAsset.standing_box``), the same as the detector."""
    standing = asset.standing_box
    # Only ever called for assets with a box: the vehicle is filtered on it,
    # and build_marked_asset never returns one without.
    assert standing is not None
    return asset.gap_feet(_in_front_gap(subject.box, standing))


def _is_at(
    subject: DetectedObject,
    asset: ProtectedAsset,
    frame_size: tuple[float, float] | None,
) -> bool:
    """Whether *subject* is at *asset* rather than merely in frame with it.

    Within the security layer's "near" distance, or — for a marked asset,
    whose zone is the asset itself — covering it the way tracks.py's own
    zone test counts as being in it. That second test is what finds someone
    at a window, whose feet are well below the sill a ground-line distance
    is measured to.
    """
    feet = _feet_from(subject, asset)
    if feet is not None and feet <= _AT_ASSET_FEET:
        return True
    if not asset.marked or asset.zone is None or frame_size is None:
        return False
    return asset.zone.covered_share_of(subject.box, *frame_size) >= _ZONE_BOX_OVERLAP


def _vehicle_pair(vehicle: ProtectedAsset, subjects: list[DetectedObject]) -> _Pair:
    """The protected vehicle's pair: whoever stands nearest the car.

    Then — only among that one subject's sightings, exactly as before — the
    moment their outline overlaps it most deeply. Choosing the subject by
    overlap too sent these stages to a passer-by near the camera, who covers
    far more of the car in the image than someone standing at its door,
    leaving the person actually at the car with a contact nothing could
    confirm.

    A person at the car outranks an animal at it. Theirs is the contact
    these stages can raise to suspicious or critical — an animal's stays
    noteworthy whatever confirms it (see security/detector.py's
    _contact_severity) — and pose only reads people. Examining the dog at a
    stranger's feet left the stranger's touch unconfirmed and the clip held
    below the alert band. Only *at* the car, though: a dog on the bonnet is
    still examined ahead of its owner across the lawn.
    """
    car = vehicle.box
    assert car is not None

    def rank(d: DetectedObject) -> tuple[bool, float, float]:
        gap = _in_front_gap(d.box, car)
        feet = vehicle.gap_feet(gap)
        person_at_car = (
            d.label == PERSON_LABEL and feet is not None and feet <= _AT_ASSET_FEET
        )
        return (not person_at_car, gap, box_gap(d.box, car))

    chosen = min(subjects, key=rank)
    nearest = min(_sightings_of(chosen, subjects), key=lambda d: box_gap(d.box, car))
    return (nearest, car, nearest.frame_index, nearest.track_id, None)


def _sightings_of(
    chosen: DetectedObject, subjects: list[DetectedObject]
) -> list[DetectedObject]:
    """Every sighting of *chosen*'s own track, or just *chosen* when it has
    no track id. Untracked boxes all share ``None``, so grouping on it would
    reopen the choice of *whom* to every untracked subject in the clip —
    handing the stages back to the passer-by overlapping the car most, or
    to the dog at a stranger's feet, whenever tracking lost the thread."""
    if chosen.track_id is None:
        return [chosen]
    return [d for d in subjects if d.track_id == chosen.track_id]


def _marked_pair_at(
    marked: list[ProtectedAsset],
    candidates: list[DetectedObject],
    frame_size: tuple[float, float] | None,
) -> tuple[ProtectedAsset, DetectedObject] | None:
    """The marked asset and subject to examine among those *at* one.

    An asset whose handling means something (a bike, a window) before one
    people are meant to touch (a door): the contact these stages can confirm
    is evidence only at the former. Then the nearest.
    """
    best: tuple[tuple[bool, float], ProtectedAsset, DetectedObject] | None = None
    for asset in marked:
        for d in candidates:
            if not _is_at(d, asset, frame_size):
                continue
            feet = _feet_from(d, asset)
            key = (asset.handled_routinely, feet if feet is not None else 0.0)
            if best is None or key < best[0]:
                best = (key, asset, d)
    return (best[1], best[2]) if best is not None else None


def _marked_pair_near(
    marked: list[ProtectedAsset], subjects: list[DetectedObject]
) -> tuple[ProtectedAsset, DetectedObject] | None:
    """The nearest subject to any marked asset, if near enough to matter."""
    best: tuple[float, ProtectedAsset, DetectedObject] | None = None
    for asset in marked:
        for d in subjects:
            feet = _feet_from(d, asset)
            if feet is None or feet > _NEAR_MARKED_FEET:
                continue
            if best is None or feet < best[0]:
                best = (feet, asset, d)
    return (best[1], best[2]) if best is not None else None


def _examine(
    asset: ProtectedAsset, chosen: DetectedObject, subjects: list[DetectedObject]
) -> _Pair:
    """The pair for *chosen* at a marked *asset*: their sighting overlapping
    it most deeply, the same moment the vehicle's pair examines."""
    box = asset.box
    assert box is not None
    nearest = min(_sightings_of(chosen, subjects), key=lambda d: box_gap(d.box, box))
    return (nearest, box, nearest.frame_index, nearest.track_id, asset.key)
