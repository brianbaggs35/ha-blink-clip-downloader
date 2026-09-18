"""One entry point that runs the whole deterministic security assessment.

``analyzer.py`` orchestrates a lot already — frame extraction, provider
calls, escalation, prompt assembly — and the security layer is not another
thing it should have to sequence by hand. This module takes the
measurements the computer-vision stages produced and returns everything the
analyzer needs: the events, the score, the evidence quality, and the prompt
text describing them.

It is pure: no I/O, no models, no clock. That means the whole
"measurements in, verdict out" path is testable from literals, which is
exactly what a layer making safety-relevant judgements should be.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .assets import ProtectedAsset
from .detector import DetectionContext, DetectorThresholds, SecurityEventDetector
from .events import SecurityEvent, Severity
from .evidence import EvidenceQuality, assess_evidence
from .narrative import build_security_segment, build_vehicle_identity_segment
from .scoring import RiskAssessment, RiskScorer, ScoringContext
from .tracks import ObjectTrack, subject_tracks
from .vehicles import nearest_vehicle_for_subjects


@dataclass
class ClipMeasurements:
    """Everything the computer-vision stages measured for one clip.

    One object rather than nineteen positional arguments: these are all
    outputs of the same pass over the same frames, they are read together,
    and every one of them is optional in the sense that the stage producing
    it may not have run. Grouping them also makes the two different senses
    of "frames" impossible to mix up at a call site —
    ``frames_analyzed``/``target_frames`` are what the *model* sees (what
    evidence quality is about), ``frame_count``/``frame_interval`` describe
    the temporal scan the tracks came from (what event timing is about).
    """

    camera: str
    tracks: list[ObjectTrack]
    frame_interval: float
    frame_count: int
    frames_analyzed: int
    target_frames: int
    asset: ProtectedAsset | None = None
    contact_touching: bool | None = None
    contact_track_id: int | None = None
    depth_similar: bool | None = None
    scene_deviation: float | None = None
    appearance_change: float | None = None
    posture_reaching: bool | None = None
    posture_arm_raised: bool | None = None
    posture_crouching: bool | None = None
    unavailable_sources: list[str] | None = None
    not_applicable_sources: list[str] | None = None
    is_night: bool = False
    approved_person_recognized: bool = False


@dataclass
class SecurityOutcome:
    """Everything the deterministic layer concluded about one clip."""

    assessment: RiskAssessment = field(default_factory=RiskAssessment)
    evidence: EvidenceQuality = field(default_factory=EvidenceQuality)
    prompt_segments: list[str] = field(default_factory=list)
    #: Which vehicle the closest subject actually went to, when there was
    #: more than one and the answer is not a coin flip: ``"protected"``,
    #: ``"other"``, or ``None``.
    nearest_vehicle: str | None = None

    @property
    def events(self) -> list[SecurityEvent]:
        return self.assessment.events

    @property
    def severity(self) -> Severity:
        return self.assessment.severity

    @property
    def risk_score(self) -> float:
        return self.assessment.score


def assess_clip(
    measurements: ClipMeasurements,
    thresholds: DetectorThresholds | None = None,
) -> SecurityOutcome:
    """Detect events, score the risk, and render the prompt evidence.

    See :class:`ClipMeasurements` for what goes in — in particular for why
    it carries two separate ideas of "frames", which are deliberately not
    interchangeable.
    """
    evidence = assess_evidence(
        measurements.frames_analyzed,
        measurements.target_frames,
        measurements.tracks,
        measurements.frame_interval,
        measurements.unavailable_sources,
        measurements.not_applicable_sources,
    )
    events = SecurityEventDetector(thresholds).detect(
        DetectionContext(
            camera=measurements.camera,
            tracks=measurements.tracks,
            frame_interval=measurements.frame_interval,
            frame_count=measurements.frame_count,
            asset=measurements.asset,
            contact_touching=measurements.contact_touching,
            contact_track_id=measurements.contact_track_id,
            depth_similar=measurements.depth_similar,
            scene_deviation=measurements.scene_deviation,
            appearance_change=measurements.appearance_change,
            posture_reaching=measurements.posture_reaching,
            posture_arm_raised=measurements.posture_arm_raised,
            posture_crouching=measurements.posture_crouching,
        )
    )

    nearest: str | None = None
    if measurements.asset is not None and measurements.asset.identification is not None:
        nearest = nearest_vehicle_for_subjects(
            subject_tracks(measurements.tracks), measurements.asset.identification
        )

    assessment = RiskScorer().score(
        events,
        evidence.score,
        ScoringContext(
            is_night=measurements.is_night,
            approved_person_recognized=measurements.approved_person_recognized,
            subject_nearer_other_vehicle=nearest == "other",
        ),
    )

    segments: list[str] = []
    if measurements.asset is not None and measurements.asset.identification is not None:
        segments.append(
            build_vehicle_identity_segment(measurements.asset.identification, nearest)
        )
    security_segment = build_security_segment(assessment, evidence)
    if security_segment:
        segments.append(security_segment)

    return SecurityOutcome(
        assessment=assessment,
        evidence=evidence,
        prompt_segments=segments,
        nearest_vehicle=nearest,
    )
