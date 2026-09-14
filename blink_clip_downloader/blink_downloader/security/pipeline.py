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
    camera: str,
    tracks: list[ObjectTrack],
    frame_interval: float,
    frame_count: int,
    frames_analyzed: int,
    target_frames: int,
    asset: ProtectedAsset | None = None,
    contact_touching: bool | None = None,
    contact_track_id: int | None = None,
    depth_similar: bool | None = None,
    scene_deviation: float | None = None,
    appearance_change: float | None = None,
    unavailable_sources: list[str] | None = None,
    is_night: bool = False,
    approved_person_recognized: bool = False,
    thresholds: DetectorThresholds | None = None,
) -> SecurityOutcome:
    """Detect events, score the risk, and render the prompt evidence.

    *frames_analyzed* and *target_frames* describe the frames the AI model
    is seeing (how many were available versus how many the configuration
    asked for), which is what evidence quality is about; *frame_count* and
    *frame_interval* describe the temporal scan the tracks came from, which
    is what event timing is about. They are deliberately separate numbers —
    conflating them is how durations end up wrong.
    """
    evidence = assess_evidence(
        frames_analyzed, target_frames, tracks, frame_interval, unavailable_sources
    )
    events = SecurityEventDetector(thresholds).detect(
        DetectionContext(
            camera=camera,
            tracks=tracks,
            frame_interval=frame_interval,
            frame_count=frame_count,
            asset=asset,
            contact_touching=contact_touching,
            contact_track_id=contact_track_id,
            depth_similar=depth_similar,
            scene_deviation=scene_deviation,
            appearance_change=appearance_change,
        )
    )

    nearest: str | None = None
    if asset is not None and asset.identification is not None:
        nearest = nearest_vehicle_for_subjects(
            subject_tracks(tracks), asset.identification
        )

    assessment = RiskScorer().score(
        events,
        evidence.score,
        ScoringContext(
            is_night=is_night,
            approved_person_recognized=approved_person_recognized,
            subject_nearer_other_vehicle=nearest == "other",
        ),
    )

    segments: list[str] = []
    if asset is not None and asset.identification is not None:
        segments.append(build_vehicle_identity_segment(asset.identification, nearest))
    security_segment = build_security_segment(assessment, evidence)
    if security_segment:
        segments.append(security_segment)

    return SecurityOutcome(
        assessment=assessment,
        evidence=evidence,
        prompt_segments=segments,
        nearest_vehicle=nearest,
    )
