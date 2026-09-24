"""Structured security analysis: tracks, events, risk, and evidence quality.

This package sits between the computer-vision stages (the ``vision``
package, which
produce boxes and pixels) and the AI providers (the ``analyzer``
package, which
produce judgement). Its job is to turn the former into structured, typed
facts the latter can verify rather than re-derive:

    detections ──▶ tracks ──▶ security events ──▶ risk score
                                      │
                                      ├──▶ prompt evidence (narrative)
                                      ├──▶ security_events table
                                      └──▶ Security tab timeline

Nothing here imports torch, ultralytics, opencv, or ``vision`` itself —
every stage's output is reduced to plain numbers before it arrives — so the
whole security layer loads, runs, and is tested on an install with none of
the optional computer-vision dependencies present.
"""

from .assets import (
    MARKABLE_ASSET_TYPES,
    AssetLocation,
    AssetType,
    ProtectedAsset,
    build_marked_asset,
    resolve_vehicle_asset,
)
from .detector import DetectionContext, DetectorThresholds, SecurityEventDetector
from .events import (
    AUDIO_EVENTS,
    BYPASS_BLOCKING_EVENTS,
    SecurityEvent,
    SecurityEventType,
    Severity,
    max_severity,
    severity_rank,
)
from .evidence import EvidenceQuality, assess_evidence
from .geometry import (
    Box,
    Zone,
    box_gap,
    box_iou,
    pixel_gap_to_feet,
    point_in_polygon,
)
from .narrative import (
    build_security_segment,
    build_vehicle_identity_segment,
    summarize_assessment,
)
from .pipeline import ClipMeasurements, SecurityOutcome, assess_clip
from .scoring import (
    RiskAssessment,
    RiskFactor,
    RiskScorer,
    ScoringContext,
    band_for_score,
)
from .sounds import detect_audio_events
from .tracks import (
    ANIMAL_LABELS,
    CARRYABLE_LABELS,
    PERSON_LABEL,
    SUBJECT_LABELS,
    VEHICLE_LABELS,
    Detection,
    ObjectTrack,
    TrackPoint,
    build_tracks,
    person_tracks,
    subject_tracks,
)
from .vehicles import (
    VehicleCandidate,
    VehicleIdentification,
    VehicleSignature,
    describe_region,
    identify_protected_vehicle,
    nearest_vehicle_for_subjects,
    relative_side,
)

__all__ = [
    "ANIMAL_LABELS",
    "AUDIO_EVENTS",
    "BYPASS_BLOCKING_EVENTS",
    "CARRYABLE_LABELS",
    "MARKABLE_ASSET_TYPES",
    "PERSON_LABEL",
    "SUBJECT_LABELS",
    "VEHICLE_LABELS",
    "AssetLocation",
    "AssetType",
    "Box",
    "ClipMeasurements",
    "Detection",
    "DetectionContext",
    "DetectorThresholds",
    "EvidenceQuality",
    "ObjectTrack",
    "ProtectedAsset",
    "RiskAssessment",
    "RiskFactor",
    "RiskScorer",
    "ScoringContext",
    "SecurityEvent",
    "SecurityEventDetector",
    "SecurityEventType",
    "SecurityOutcome",
    "Severity",
    "TrackPoint",
    "VehicleCandidate",
    "VehicleIdentification",
    "VehicleSignature",
    "Zone",
    "assess_clip",
    "assess_evidence",
    "band_for_score",
    "box_gap",
    "box_iou",
    "build_marked_asset",
    "build_security_segment",
    "build_tracks",
    "build_vehicle_identity_segment",
    "describe_region",
    "detect_audio_events",
    "identify_protected_vehicle",
    "max_severity",
    "nearest_vehicle_for_subjects",
    "person_tracks",
    "pixel_gap_to_feet",
    "point_in_polygon",
    "relative_side",
    "resolve_vehicle_asset",
    "severity_rank",
    "subject_tracks",
    "summarize_assessment",
]
