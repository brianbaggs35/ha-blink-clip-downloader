"""Deterministic security-event detection from tracks and protected assets.

This is the layer that takes work away from the AI model. Geometry and
timing — did someone enter the zone, did they get closer, how long did they
stay, did the boxes overlap at the same depth — are things code can compute
exactly and a vision-language model can only guess at from stills. So code
computes them, and the model is left to do what it is actually good at:
looking at the frames and saying whether the story those facts tell is
really what happened.

Every rule here is written to under-claim. Sampled frames are seconds
apart, boxes are approximations, and a 2D overlap is not contact. Where the
evidence only supports "possible", the event says possible — see each
rule's own comment for exactly what it does and does not establish.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .assets import ProtectedAsset
from .events import SecurityEvent, SecurityEventType, Severity, severity_rank
from .geometry import box_foot_point
from .tracks import (
    ANIMAL_LABELS,
    CARRYABLE_LABELS,
    PERSON_LABEL,
    ApproachProfile,
    ObjectTrack,
    subject_tracks,
)

#: How much an event's confidence is cut when the protected asset itself was
#: only tentatively identified. Not zero — the geometry still happened — but
#: enough that an ambiguous identification cannot drive a critical alert on
#: its own.
_UNCONFIDENT_ASSET_SCALE = 0.6

#: Contact confidence required before a movement spike alone can escalate to
#: an impact candidate. Met by depth- or segmentation-backed contact, not by
#: a bare bounding-box overlap.
_IMPACT_CONTACT_CONFIDENCE = 0.6

#: Confidence assigned when the *only* evidence of contact is that two boxes
#: overlapped deeply in a 2D projection. Deliberately below
#: :data:`_IMPACT_CONTACT_CONFIDENCE`, and below the bar
#: :meth:`SecurityEventDetector._contact_severity` uses to call a contact
#: suspicious rather than merely noteworthy.
_BARE_OVERLAP_CONFIDENCE = 0.45

#: Contact confidence at or above which the event is treated as suspicious
#: rather than noteworthy. A camera cannot see depth, so a person walking in
#: front of or behind a parked car overlaps it in every single frame — and
#: on a device where the depth and segmentation stages are unavailable, that
#: projection is *all* the evidence there is. Calling it suspicious anyway
#: is what turns an ordinary pavement into a stream of false alerts. The
#: event is still raised, still reaches the prompt and the Security tab, and
#: still says plainly what it rests on; only its weight waits for evidence
#: that a second stage actually confirmed it.
_CONFIRMED_CONTACT_CONFIDENCE = 0.6


@dataclass(frozen=True)
class DetectorThresholds:
    """Tunable cut-offs for every rule below.

    Defaults are aligned with the behavioural thresholds the analysis
    prompt already states in words (1 ft / 3 ft from a protected vehicle),
    so the deterministic layer and the model's own written rules agree
    instead of each having invented their own idea of "close".
    """

    #: Distance (feet) inside which a subject counts as near the asset.
    near_feet: float = 3.0
    #: Distance (feet) inside which proximity becomes concerning on its own.
    close_feet: float = 1.0
    #: Distance (feet) a subject must end up within for closing distance to
    #: read as "approached the asset" rather than merely "walked nearer".
    approach_feet: float = 8.0
    #: Observed presence (seconds) near the asset or inside its zone that
    #: counts as lingering rather than passing through. Comfortably longer
    #: than unlocking a car and getting in, which is what the threshold has
    #: to clear to avoid firing on every household departure.
    loiter_seconds: float = 12.0
    #: Observed presence (seconds) anywhere in frame that counts as
    #: loitering even with no protected asset involved.
    open_loiter_seconds: float = 15.0
    #: Total travel (frame widths) below which a track counts as having
    #: stayed put rather than crossed the scene.
    stationary_path: float = 0.35
    #: Speed-up between consecutive legs, in the subject's own heights per
    #: second, that reads as rushing at the asset or bolting from it — a
    #: jog or faster. An ordinary walk is about 0.8 heights a second (1.4 m/s
    #: for someone 1.7 m tall) and a brisk one about 1.0, and standing at
    #: the car and then walking off is a speed-up of exactly that much: the
    #: shape of getting out of a parked car and going indoors, which must
    #: not read as an impact.
    abrupt_speed_change: float = 1.2
    #: Change (0.0-1.0) in the asset's own image region between before and
    #: after the interaction that reads as the asset itself being altered.
    appearance_change: float = 0.25
    #: Scene-baseline deviation (0.0-1.0) above which an empty frame reads
    #: as the camera itself having been interfered with.
    obstruction_deviation: float = 0.45
    #: Share of the subject's own box height that must overlap the asset
    #: before an overlap counts as possible contact. In a 2D projection
    #: everyone who walks in front of or behind a parked car overlaps it;
    #: only a substantial overlap distinguishes touching from passing.
    contact_overlap_fraction: float = 0.15


@dataclass
class DetectionContext:
    """Everything the detector needs about one clip.

    Deliberately plain values rather than objects from
    :mod:`blink_downloader.vision`: the optional depth and contact stages
    are summarized down to ``depth_similar`` / ``contact_touching`` tri-state
    booleans before they get here, which keeps this package independent of
    torch and makes every rule below trivially testable from literals.

    ``contact_track_id`` names which track the depth/contact stages actually
    examined — they run on a single chosen subject/asset pair, so applying
    their verdict to a *different* subject in the same clip would attribute
    evidence to the wrong person. When it is ``None``, the verdict is
    applied only to whichever subject came closest to the asset.
    """

    camera: str
    tracks: list[ObjectTrack]
    frame_interval: float
    frame_count: int
    asset: ProtectedAsset | None = None
    contact_touching: bool | None = None
    contact_track_id: int | None = None
    depth_similar: bool | None = None
    scene_deviation: float | None = None
    appearance_change: float | None = None
    #: What pose estimation found the examined subject's body doing at the
    #: closest moment (see ``vision.PostureResult``). All ``None`` when the
    #: stage is off or found nothing, which every rule treats as "no
    #: posture evidence" rather than as a negative finding.
    posture_reaching: bool | None = None
    posture_arm_raised: bool | None = None
    posture_crouching: bool | None = None
    #: Assets marked on the Assets tab for this camera, each located by the
    #: zone its owner drew (see :func:`~.assets.build_marked_asset`). Held to
    #: the same rules as the protected vehicle, with the differences each
    #: asset's own properties decide — see ``ProtectedAsset.handled_routinely``
    #: and ``impact_applies``. Empty on every camera nobody marked anything
    #: on, which leaves this detector exactly as it was.
    marked_assets: list[ProtectedAsset] = field(default_factory=list)
    #: Which marked asset the depth/contact/pose stages examined, by key.
    #: ``None`` means the protected vehicle, as it always has. Those stages
    #: look at one subject and one asset per clip, so a verdict about the car
    #: must never be read as one about the door beside it, or the reverse.
    examined_asset_key: str | None = None
    #: How much each marked asset's own region changed between the clip's
    #: first and last scan frames, by key — only for assets nobody was
    #: standing in front of in either frame (see vision/pipeline.py).
    marked_asset_changes: dict[str, float] = field(default_factory=dict)

    @property
    def timeline_end(self) -> float:
        """Clip-relative offset of the last sampled frame."""
        return max(0, self.frame_count - 1) * self.frame_interval


def _round_evidence(values: dict[str, object]) -> dict[str, object]:
    """Round float evidence values so stored JSON stays readable."""
    return {
        key: round(value, 3) if isinstance(value, float) else value
        for key, value in values.items()
    }


class SecurityEventDetector:
    """Turns tracks plus optional CV evidence into structured events."""

    def __init__(self, thresholds: DetectorThresholds | None = None) -> None:
        self._t = thresholds or DetectorThresholds()

    def detect(self, ctx: DetectionContext) -> list[SecurityEvent]:
        """Return every event supported by *ctx*, earliest first.

        Ties at the same offset are ordered most-severe first so a
        timeline's first line for a moment is its most important one.
        """
        asset_events = self._asset_events(ctx)
        # Standing at the protected vehicle for a while is *one* behaviour,
        # and the scorer adds up every event it is handed — so a track
        # already credited with lingering at the asset must not also be
        # scored for lingering on open ground, or a single loiterer is
        # counted twice at full weight.
        already_loitering = {
            _track_key(e)
            for e in asset_events
            if e.event_type is SecurityEventType.LOITERING
        }
        events: list[SecurityEvent] = []
        events.extend(self._subject_events(ctx, already_loitering))
        events.extend(asset_events)
        events.extend(self._carryable_events(ctx))
        events.extend(self._scene_events(ctx))
        events.sort(key=lambda e: (e.start_offset, -severity_rank(e.severity)))
        return events

    # -- subjects ------------------------------------------------------

    def _subject_events(
        self, ctx: DetectionContext, already_loitering: set[tuple[object, ...]]
    ) -> list[SecurityEvent]:
        """Presence, open-ground loitering, and more than one person."""
        events: list[SecurityEvent] = []
        subjects = subject_tracks(ctx.tracks)
        for track in subjects:
            presence = self._presence_event(track, ctx)
            events.append(presence)
            loiter = self._open_loiter_event(track)
            if loiter is not None and _track_key(loiter) not in already_loitering:
                events.append(loiter)

        people = [t for t in subjects if t.label == PERSON_LABEL and t.tracked]
        if len(people) > 1:
            events.append(
                SecurityEvent(
                    event_type=SecurityEventType.MULTIPLE_SUBJECTS,
                    severity=Severity.NOTEWORTHY,
                    confidence=min(
                        0.9, sum(t.mean_confidence for t in people) / len(people)
                    ),
                    detail=f"{len(people)} separate people were tracked in this clip.",
                    subject_label=PERSON_LABEL,
                    start_offset=min(t.first_offset for t in people),
                    end_offset=max(t.last_offset for t in people),
                    evidence=_round_evidence({"person_count": len(people)}),
                )
            )
        return events

    def _presence_event(
        self, track: ObjectTrack, ctx: DetectionContext
    ) -> SecurityEvent:
        """The baseline "something was here" event, always routine."""
        # A track seen in a single frame has no measurable dwell, and
        # "visible for at least 0s" is not a sentence anyone should be shown
        # — say "briefly" rather than round a sub-second sighting to zero.
        dwell = (
            f"was visible for at least {track.dwell_seconds:.0f}s"
            if track.dwell_seconds >= 1
            else "was visible only briefly"
        )
        return SecurityEvent(
            event_type=SecurityEventType.SUBJECT_PRESENT,
            severity=Severity.ROUTINE,
            confidence=track.mean_confidence,
            detail=f"{_article(track.label)} {track.label} {dwell}, {track.direction}.",
            subject_label=track.label,
            track_id=track.track_id,
            start_offset=track.first_offset,
            end_offset=track.last_offset,
            evidence=_round_evidence(
                {
                    "dwell_seconds": track.dwell_seconds,
                    "frames_seen": track.frame_count,
                    "direction": track.direction,
                    "average_speed": track.average_speed,
                    "tracking_continuity": track.continuity(ctx.frame_interval),
                    "tracked": track.tracked,
                }
            ),
        )

    def _open_loiter_event(self, track: ObjectTrack) -> SecurityEvent | None:
        """Loitering with no protected asset involved — long and stationary.

        Requires both a long observed presence *and* very little travel:
        someone walking steadily across a large field of view can be in
        frame for a long time without loitering in any meaningful sense.
        """
        if (
            track.dwell_seconds < self._t.open_loiter_seconds
            or track.path_length > self._t.stationary_path
        ):
            return None
        return SecurityEvent(
            event_type=SecurityEventType.LOITERING,
            severity=Severity.NOTEWORTHY,
            confidence=0.55,
            detail=(
                f"The {track.label} stayed in roughly one place for at least "
                f"{track.dwell_seconds:.0f}s."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            start_offset=track.first_offset,
            end_offset=track.last_offset,
            evidence=_round_evidence(
                {
                    "dwell_seconds": track.dwell_seconds,
                    "path_length_widths": track.path_length,
                }
            ),
        )

    # -- protected asset ----------------------------------------------

    def _asset_events(self, ctx: DetectionContext) -> list[SecurityEvent]:
        """Every rule that needs a located protected asset to mean anything,
        for the protected vehicle and for each marked asset in turn."""
        events = self._events_for_asset(ctx.asset, ctx) if ctx.asset else []
        for asset in ctx.marked_assets:
            events.extend(self._events_for_asset(asset, ctx))
        return events

    def _events_for_asset(
        self, asset: ProtectedAsset, ctx: DetectionContext
    ) -> list[SecurityEvent]:
        """The asset rules for one asset, over every subject in the clip."""
        if asset.box is None:
            return []
        subjects = subject_tracks(ctx.tracks)
        if not subjects:
            return []

        profiles = [t.approach_to(asset.box) for t in subjects]
        # How close each came is measured to where someone at the asset
        # would stand, which for a window is the ground below it rather than
        # its sill; whether they touched it is measured against the asset.
        standing_box = asset.standing_box
        assert standing_box is not None
        standing = (
            [t.approach_to(standing_box) for t in subjects]
            if asset.elevated
            else profiles
        )
        primary = min(range(len(subjects)), key=lambda i: profiles[i].min_box_gap)
        examined = self._examined(asset, ctx)

        events: list[SecurityEvent] = []
        for index, track in enumerate(subjects):
            events.extend(
                self._asset_track_events(
                    track,
                    profiles[index],
                    asset,
                    ctx,
                    cv_applies=examined
                    and self._cv_evidence_applies(track, index == primary, ctx),
                    standing=standing[index],
                )
            )
        disturbed = self._disturbed_event(asset, ctx, events)
        if disturbed is not None:
            events.append(disturbed)
        if not asset.confident:
            # Every one of these events is a claim about the protected
            # vehicle specifically. If which car that is was a guess, the
            # claim inherits the guess's uncertainty rather than presenting
            # at full strength — see vehicles.identify_protected_vehicle.
            events = [
                replace(e, confidence=e.confidence * _UNCONFIDENT_ASSET_SCALE)
                for e in events
            ]
        return events

    @staticmethod
    def _examined(asset: ProtectedAsset, ctx: DetectionContext) -> bool:
        """Whether this clip's depth/contact/pose verdict is about *asset*."""
        if ctx.examined_asset_key is None:
            return asset is ctx.asset
        return asset.marked and asset.key == ctx.examined_asset_key

    @staticmethod
    def _cv_evidence_applies(
        track: ObjectTrack, is_primary: bool, ctx: DetectionContext
    ) -> bool:
        """Whether this clip's depth/contact verdict describes *track*.

        Those stages examine one subject/asset pair per clip. Matching on
        the track id they reported is exact; without one, only the subject
        that came closest to the asset can plausibly be the pair they
        looked at.
        """
        if ctx.contact_track_id is not None:
            return track.track_id == ctx.contact_track_id
        return is_primary

    def _asset_track_events(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        asset: ProtectedAsset,
        ctx: DetectionContext,
        cv_applies: bool,
        standing: ApproachProfile | None = None,
    ) -> list[SecurityEvent]:
        """*profile* is measured against the asset itself (overlap, contact);
        *standing* against where someone at it stands (distance, approach,
        retreat), which differ only for an elevated asset."""
        standing = standing or profile
        events: list[SecurityEvent] = []
        depth_verdict = ctx.depth_similar if cv_applies else None
        min_feet, near = self._nearness(track, profile, standing, asset, depth_verdict)

        zone_event = self._zone_event(track, asset, ctx, depth_verdict)
        if zone_event is not None:
            events.append(zone_event)

        loiter = self._asset_loiter_event(track, asset, near and asset.present)
        if loiter is not None:
            events.append(loiter)

        # Everything below is about the physical object. When the protected
        # vehicle has evidently been driven away and only its empty space
        # remains (see AssetLocation.ZONE_ABSENT), there is nothing to be
        # near, approach, or touch — and reporting otherwise is exactly how
        # a neighbour parking in the vacated spot became a critical alert.
        if not asset.present:
            return events

        if near:
            events.append(
                self._proximity_event(track, standing, asset, min_feet, depth_verdict)
            )

        # Gated on the same depth verdict as proximity: a passer-by on the
        # pavement closes a lot of 2D distance to a car they are nowhere
        # near, and "approached the vehicle" is exactly as wrong for them as
        # "stood next to it" would be.
        approach = (
            self._approach_event(track, standing, asset, min_feet)
            if depth_verdict is not False
            else None
        )
        if approach is not None:
            events.append(approach)

        if asset.handled_routinely:
            # Reaching for a door and touching it is how a door is used, so
            # neither is evidence here — see ProtectedAsset.handled_routinely.
            # Coming close and leaving again still is the plain record of a
            # visit, and costs next to nothing in the score.
            if near and profile.retreated:
                events.append(self._retreat_event(track, standing, asset))
            return events

        if near and cv_applies and ctx.posture_reaching:
            events.append(self._reach_event(track, standing, asset, ctx))

        events.extend(
            self._contact_events(track, profile, standing, asset, ctx, cv_applies, near)
        )
        return events

    def _nearness(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        standing: ApproachProfile,
        asset: ProtectedAsset,
        depth_verdict: bool | None,
    ) -> tuple[float, bool]:
        """How close *track* came to *asset* in feet, and whether that was near.

        Depth estimation is what separates "walked past the car" from
        "stood right at the car": in a 2D frame those look identical, and
        the depth map is the only evidence that says which one happened. A
        negative verdict is as useful as a positive one, so when depth
        places this subject at a clearly different distance from the camera
        than the asset, the proximity and zone rules stand down entirely
        rather than reporting a closeness that only exists in the
        projection.

        A positive one decides the far side of the car — the driver's door,
        from a camera facing its passenger side — where the car hides the
        subject's feet, so their box ends mid-car and the foot point reads
        as metres behind it. Depth placing them at the car's own distance
        with their outlines overlapping is being at the car, wherever the
        feet appear to be. Without it the same confirmed touch scored 69 on
        the far side against 86 on the near one, below the alert band. Only
        feet the car could be hiding, though: feet in plain view in front of
        it are a measured distance, and depth calls a passer-by who overlaps
        the car in the image "similar" more often than not (10 of 13 real
        photos) — letting that override a visible gap put someone walking
        past five feet in front of the car "within 1 ft" of it and forced a
        critical alert.
        """
        min_feet = asset.gap_feet(standing.min_gap)
        # _asset_events only calls this for an asset with a box, which is
        # the one thing gap_feet needs to produce a number.
        assert min_feet is not None
        if depth_verdict is False:
            return min_feet, False
        near = min_feet <= self._t.near_feet
        if (
            depth_verdict is True
            and profile.min_box_gap <= 0
            and not near
            and self._feet_could_be_hidden(track, profile, asset)
        ):
            return min(min_feet, self._t.close_feet), True
        return min_feet, near

    def _contact_events(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        standing: ApproachProfile,
        asset: ProtectedAsset,
        ctx: DetectionContext,
        cv_applies: bool,
        near: bool,
    ) -> list[SecurityEvent]:
        """Possible contact, and the two events defined in terms of it.

        Impact and retreat-after-contact both describe the *same* contact,
        so neither can exist without it — and a subject who came close and
        then left without one gets the plain retreat instead.
        """
        contact = self._contact_event(track, profile, asset, ctx, cv_applies, near)
        if contact is None:
            if near and standing.retreated:
                return [self._retreat_event(track, standing, asset)]
            return []

        events = [contact]
        impact = (
            self._impact_event(track, profile, asset, ctx, contact)
            if cv_applies and asset.impact_applies
            else None
        )
        if impact is not None:
            events.append(impact)
        retreat = self._retreat_after_contact_event(track, standing, asset, contact)
        if retreat is not None:
            events.append(retreat)
        return events

    @staticmethod
    def _feet_could_be_hidden(
        track: ObjectTrack, profile: ApproachProfile, asset: ProtectedAsset
    ) -> bool:
        """Whether the subject's feet, where they overlapped the asset most,
        were no nearer the camera than its ground line — where the asset
        itself could be hiding them."""
        # Only reached for an overlapping outline, so the asset has a box.
        assert asset.box is not None
        _, foot_y = box_foot_point(track.points[profile.min_box_gap_index].box)
        return foot_y <= asset.box[3]

    def _zone_event(
        self,
        track: ObjectTrack,
        asset: ProtectedAsset,
        ctx: DetectionContext,
        depth_similar: bool | None = None,
    ) -> SecurityEvent | None:
        """Presence in the user-drawn zone, judged from the subject's feet.

        Deliberately not restricted to an observed *crossing*. A clip starts
        when motion is detected, so a subject can perfectly well already be
        standing at the vehicle in the very first sampled frame — and on a
        camera pointed straight at a parking space that is the common case,
        not the exotic one. Requiring an outside-then-inside transition
        silently produced no zone event at all for exactly those clips. The
        wording below distinguishes the two so the claim stays honest about
        what was actually observed.

        Suppressed when depth estimation places the subject at a clearly
        different distance from the camera than the vehicle the zone was
        drawn around — they overlap the zone in the image while standing
        somewhere else entirely. Only applied when the vehicle itself was
        detected, since that is what the depth comparison actually measured
        against.
        """
        if asset.zone is None or not track.in_zone(asset.zone):
            return None
        # A marked asset's zone *is* the asset, so a depth comparison against
        # it measured the asset itself, detected or not.
        if depth_similar is False and (asset.detected or asset.marked):
            return None
        dwell = track.zone_dwell(asset.zone)
        # A single in-zone sighting gives a zero-length span, which is a
        # real limit of sampling every couple of seconds rather than a
        # measurement of an instant — say "entered" instead of claiming a
        # duration the frames cannot support.
        stayed = f" and stayed at least {dwell:.0f}s" if dwell >= 1.0 else ""
        crossed = track.entered_zone(asset.zone)
        # Named after the asset rather than via _asset_place: the zone is a
        # fixed region the user drew, and it keeps that name whether or not
        # the car is currently parked in it.
        place = asset.reference
        movement = (
            f"crossed into the area marked around {place}"
            if crossed
            else f"was already inside the area marked around {place} when the clip began"
        )
        return SecurityEvent(
            event_type=SecurityEventType.ZONE_ENTERED,
            severity=Severity.NOTEWORTHY,
            confidence=0.8 if track.tracked else 0.5,
            detail=f"The {track.label} {movement}{stayed}.",
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=track.first_offset,
            end_offset=track.last_offset,
            evidence=_round_evidence(
                {
                    "zone_dwell_seconds": dwell,
                    "frames_in_zone": sum(track.zone_membership(asset.zone)),
                    "crossed_in": crossed,
                    "frame_interval": ctx.frame_interval,
                }
            ),
        )

    def _proximity_event(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        asset: ProtectedAsset,
        min_feet: float,
        depth_similar: bool | None = None,
    ) -> SecurityEvent:
        close = min_feet <= self._t.close_feet
        confidence = 0.75 if asset.located_exactly else 0.5
        if depth_similar is True:
            # Independent confirmation that the subject really is at the
            # vehicle's distance, not merely overlapping it in projection.
            confidence = min(0.95, confidence + 0.15)
        return SecurityEvent(
            event_type=SecurityEventType.ASSET_PROXIMITY,
            # Standing within a foot of a door is ringing its bell. Closeness
            # to an asset people are meant to walk up to stays a fact on the
            # record rather than a concern; lingering there is what escalates.
            severity=(
                Severity.SUSPICIOUS
                if close and not asset.handled_routinely
                else Severity.NOTEWORTHY
            ),
            confidence=confidence,
            detail=(
                f"The {track.label} came within {_feet_phrase(min_feet)} of "
                f"{asset.reference}"
                + (
                    ""
                    if asset.located_exactly
                    else ", measured against where it normally sits"
                )
                + "."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=profile.min_gap_offset,
            end_offset=profile.min_gap_offset,
            evidence=_round_evidence(
                {
                    "min_gap_pixels": profile.min_gap,
                    "min_gap_feet": min_feet,
                    "asset_detected": asset.detected,
                    "asset_marked": asset.marked,
                    "similar_depth": depth_similar,
                }
            ),
        )

    def _approach_event(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        asset: ProtectedAsset,
        min_feet: float,
    ) -> SecurityEvent | None:
        """Closing distance, but only when it ends up somewhere that matters.

        Walking from the far edge of frame to the near edge closes a lot of
        distance without ever coming near the asset — that is a pedestrian,
        not an approach.
        """
        if not profile.approached or min_feet > self._t.approach_feet:
            return None
        return SecurityEvent(
            event_type=SecurityEventType.ASSET_APPROACHED,
            severity=Severity.NOTEWORTHY,
            confidence=0.7 if track.tracked else 0.45,
            detail=(
                f"The {track.label} closed {profile.approach_fraction * 100:.0f}% of "
                f"the distance to {asset.reference}, "
                f"ending within {_feet_phrase(min_feet)}."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=track.first_offset,
            end_offset=profile.min_gap_offset,
            evidence=_round_evidence(
                {
                    "approach_fraction": profile.approach_fraction,
                    "first_gap_pixels": profile.first_gap,
                    "min_gap_pixels": profile.min_gap,
                    "min_gap_feet": min_feet,
                }
            ),
        )

    def _asset_loiter_event(
        self, track: ObjectTrack, asset: ProtectedAsset, near: bool
    ) -> SecurityEvent | None:
        """Lingering specifically at the asset, which needs far less time
        than loitering on open ground before it is worth noticing."""
        in_zone = asset.zone is not None and track.in_zone(asset.zone)
        if not (near or in_zone):
            return None
        dwell = (
            track.zone_dwell(asset.zone)
            if in_zone and asset.zone is not None
            else track.dwell_seconds
        )
        if dwell < self._t.loiter_seconds:
            return None
        return SecurityEvent(
            event_type=SecurityEventType.LOITERING,
            severity=Severity.SUSPICIOUS,
            confidence=0.7 if track.tracked else 0.45,
            detail=(
                f"The {track.label} remained at {_asset_place(asset)} for at "
                f"least {dwell:.0f}s."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=track.first_offset,
            end_offset=track.last_offset,
            evidence=_round_evidence(
                {"dwell_seconds": dwell, "in_zone": in_zone, "near_asset": near}
            ),
        )

    def _reach_event(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        asset: ProtectedAsset,
        ctx: DetectionContext,
    ) -> SecurityEvent:
        """An arm extended toward the asset from close range.

        Only emitted alongside proximity: a reaching gesture ten feet from a
        car is somebody stretching. Close range plus a reach is the shape of
        trying a door handle, and it is the one thing a bounding box is
        completely blind to.
        """
        crouched = " while crouched or bent over" if ctx.posture_crouching else ""
        return SecurityEvent(
            event_type=SecurityEventType.ASSET_REACH,
            severity=Severity.SUSPICIOUS,
            confidence=0.65,
            detail=(
                f"The {track.label} had an arm extended toward "
                f"{asset.reference} from close "
                f"range{crouched}."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=profile.min_gap_offset,
            end_offset=profile.min_gap_offset,
            evidence=_round_evidence(
                {
                    "min_gap_pixels": profile.min_gap,
                    "crouching": bool(ctx.posture_crouching),
                    "arm_raised": bool(ctx.posture_arm_raised),
                }
            ),
        )

    def _contact_basis(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        asset: ProtectedAsset,
        ctx: DetectionContext,
        cv_applies: bool,
        near: bool,
    ) -> tuple[float, str] | None:
        """Grade the evidence that a subject actually touched the asset.

        A 2D box overlap is the weakest possible signal — a person walking
        ten feet in front of a car overlaps it in the image every time,
        which is exactly the false positive that makes naive box-overlap
        "contact" detection useless on a driveway. Two things guard against
        it here: the overlap must be *deep* relative to the subject's own
        size, and depth estimation or pixel-level segmentation, when
        available, dominate — including their negative verdicts. If either
        says the subject was at a different distance or not touching, no
        contact event is emitted at all.
        """
        if cv_applies and ctx.depth_similar is False:
            # Segmentation and depth answer different questions, and this is
            # the case where they disagree: a person walking *in front of*
            # the car has silhouettes that genuinely abut in the image while
            # the two are metres apart on the ground. Depth is the only
            # stage that can tell those apart, so its negative verdict wins
            # over segmentation's positive one — the same precedence every
            # other rule here gives it. The contact *hint* still reaches the
            # prompt either way (see vision/pipeline.py's _run_pair_stages), so the
            # model can still see and judge it; what is withheld is this
            # layer asserting a contact its own evidence contradicts.
            return None
        if cv_applies and ctx.contact_touching is True:
            return 0.8, "pixel-level segmentation found the outlines touching"
        if profile.min_box_gap > 0 or not self._overlap_is_deep(track, profile):
            return None
        if not near and not self._feet_could_be_hidden(track, profile, asset):
            # The outlines overlap only in the picture: the subject's feet
            # were in plain view, further in front of the asset than anyone
            # can reach. That is a passer-by between the camera and the car,
            # and "possible contact" is a claim their own position rules
            # out — one that still reached the prompt and the Security tab
            # for everyone walking along the pavement past a parked car. The
            # far side of the asset, where it hides the feet, keeps the
            # overlap as its evidence.
            return None
        if cv_applies and ctx.contact_touching is False:
            return None
        if cv_applies and ctx.depth_similar is True:
            return 0.7, "the outlines overlapped at a similar distance from the camera"
        return (
            _BARE_OVERLAP_CONFIDENCE,
            "the outlines overlapped, with no depth or segmentation evidence",
        )

    @staticmethod
    def points_offset(track: ObjectTrack, profile: ApproachProfile) -> float:
        """Clip offset of the moment the outlines overlapped most deeply."""
        return track.points[profile.min_box_gap_index].offset

    def _overlap_is_deep(self, track: ObjectTrack, profile: ApproachProfile) -> bool:
        """Is the overlap more than a passer-by clipping the asset's outline?"""
        box = track.points[profile.min_box_gap_index].box
        height = box[3] - box[1]
        if height <= 0:
            return False
        return -profile.min_box_gap >= self._t.contact_overlap_fraction * height

    @staticmethod
    def _contact_severity(confidence: float, animal: bool) -> Severity:
        """How much weight a contact claim has earned.

        An animal against the vehicle stays noteworthy whatever confirmed
        it: a dog putting its paws on a bonnet is worth telling the owner
        about, and is why animals are subjects at all, but it is not the
        intruder the suspicious band exists for. For a person, the
        distinction that matters is whether anything beyond a 2D overlap
        actually backs the claim — see :data:`_CONFIRMED_CONTACT_CONFIDENCE`.
        """
        if animal:
            return Severity.NOTEWORTHY
        return (
            Severity.SUSPICIOUS
            if confidence >= _CONFIRMED_CONTACT_CONFIDENCE
            else Severity.NOTEWORTHY
        )

    def _contact_event(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        asset: ProtectedAsset,
        ctx: DetectionContext,
        cv_applies: bool,
        near: bool,
    ) -> SecurityEvent | None:
        basis = self._contact_basis(track, profile, asset, ctx, cv_applies, near)
        if basis is None:
            return None
        confidence, reason = basis
        animal = track.label in ANIMAL_LABELS
        event_type = (
            SecurityEventType.ANIMAL_ASSET_INTERACTION
            if animal
            else SecurityEventType.CONTACT_CANDIDATE
        )
        return SecurityEvent(
            event_type=event_type,
            severity=self._contact_severity(confidence, animal),
            confidence=confidence,
            detail=(
                f"Possible contact between the {track.label} and "
                f"{asset.reference} — {reason}."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=self.points_offset(track, profile),
            end_offset=self.points_offset(track, profile),
            evidence=_round_evidence(
                {
                    "min_overlap_pixels": profile.min_box_gap,
                    "basis": reason,
                    # Recorded here rather than read back off `confidence`:
                    # an uncertain identification of the car scales every
                    # event's confidence down afterwards (see
                    # _asset_events), which says nothing about whether a
                    # second stage backed this contact.
                    "confirmed": confidence >= _CONFIRMED_CONTACT_CONFIDENCE,
                    "segmentation_contact": ctx.contact_touching,
                    "similar_depth": ctx.depth_similar,
                }
            ),
        )

    def _impact_event(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        asset: ProtectedAsset,
        ctx: DetectionContext,
        contact: SecurityEvent,
    ) -> SecurityEvent | None:
        """Contact plus something abrupt — the signature of a strike or bump.

        Restricted to people: an animal brushing a car produces the same
        contact evidence, but "impact" carries an intent this pipeline has
        no business inferring from a dog. Callers restrict it further, to
        the one subject the pose and appearance stages actually examined —
        both pieces of evidence below belong to that subject and that
        moment, and attributing either to a second person who merely
        overlapped the vehicle would double-count the only CRITICAL event
        this detector can emit.
        """
        if track.label != PERSON_LABEL:
            return None
        # _asset_events only reaches here for an asset with a box: contact
        # was measured against it.
        assert asset.box is not None
        speed_increase = track.max_speed_increase_at(asset.box)
        change = ctx.appearance_change
        # None of the three signals below is worth anything unless the
        # contact under it is itself well evidenced. Bare box overlap plus a
        # brisk walk is a person arriving at their car, not a collision;
        # bare box overlap plus "the car's image region looks different" is
        # a passer-by crossing in front of a car whose door someone opened,
        # or on which snow settled, or which the camera re-exposed; and bare
        # box overlap plus a raised wrist is somebody walking past their own
        # car with a phone to their ear (_ARM_RAISED_FRACTION is 6% of body
        # height, so an everyday gesture clears it). Requiring depth or
        # segmentation backing is what keeps the one CRITICAL event type
        # this detector can emit — the one that forces an alert past the AI
        # model's own verdict and withholds the face-recognition bypass —
        # out of everyday footage on a device where those stages are
        # unavailable, exactly as DOCS' "Running it on a low-powered device"
        # promises.
        confirmed = contact.confidence >= _IMPACT_CONTACT_CONFIDENCE
        abrupt = speed_increase >= self._t.abrupt_speed_change and confirmed
        altered = (
            change is not None and change >= self._t.appearance_change and confirmed
        )
        # A raised arm at the moment of contact is the one posture that
        # separates a strike from a touch, and unlike the speed spike it is
        # visible in the single frame the pose stage examines — so it needs
        # no corroborating *motion* signal. It still needs the same
        # confirmed contact underneath it as the other two: what a single
        # frame cannot establish is that the subject was touching the
        # vehicle at all rather than passing in front of it.
        raised = bool(ctx.posture_arm_raised) and confirmed
        if not (abrupt or altered or raised):
            return None

        reasons: list[str] = []
        if raised:
            reasons.append("the subject's arm was raised above shoulder height")
        if abrupt:
            reasons.append("the subject accelerated sharply around that moment")
        if altered:
            reasons.append("the asset's own image region looked different afterwards")
        return SecurityEvent(
            event_type=SecurityEventType.IMPACT_CANDIDATE,
            severity=Severity.CRITICAL,
            confidence=min(0.85, contact.confidence + 0.1),
            detail=(
                f"Possible impact with {asset.reference}: "
                + " and ".join(reasons)
                + ". This is a candidate for review, not a confirmed impact."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=profile.min_gap_offset,
            end_offset=track.last_offset,
            evidence=_round_evidence(
                {
                    "max_speed_increase": speed_increase,
                    "appearance_change": change,
                    "arm_raised": raised,
                    "contact_confidence": contact.confidence,
                }
            ),
        )

    def _retreat_after_contact_event(
        self,
        track: ObjectTrack,
        profile: ApproachProfile,
        asset: ProtectedAsset,
        contact: SecurityEvent,
    ) -> SecurityEvent | None:
        """Moving away immediately after touching the asset.

        This event is defined entirely in terms of the contact it follows,
        so it cannot be more certain than that contact was. Inheriting the
        weaker severity matters most on a device where the depth and
        segmentation stages are unavailable: a passer-by whose box merely
        overlapped the car in projection would otherwise walk on and collect
        a second suspicious event for doing so.
        """
        if not profile.retreated:
            return None
        return SecurityEvent(
            event_type=SecurityEventType.RETREAT_AFTER_CONTACT,
            severity=min(Severity.SUSPICIOUS, contact.severity, key=severity_rank),
            confidence=min(0.6, contact.confidence),
            detail=(
                f"The {track.label} moved away from "
                f"{asset.reference} directly after the "
                "possible contact."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=profile.min_gap_offset,
            end_offset=track.last_offset,
            evidence=_round_evidence(
                {
                    "retreat_fraction": profile.retreat_fraction,
                    "min_gap_pixels": profile.min_gap,
                    "last_gap_pixels": profile.last_gap,
                    "confirmed": contact.evidence["confirmed"],
                }
            ),
        )

    def _retreat_event(
        self, track: ObjectTrack, profile: ApproachProfile, asset: ProtectedAsset
    ) -> SecurityEvent:
        return SecurityEvent(
            event_type=SecurityEventType.RETREAT,
            severity=Severity.NOTEWORTHY,
            confidence=0.6,
            detail=(
                f"The {track.label} came close to "
                f"{asset.reference} and then moved away "
                "again."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=profile.min_gap_offset,
            end_offset=track.last_offset,
            evidence=_round_evidence(
                {
                    "retreat_fraction": profile.retreat_fraction,
                    "min_gap_pixels": profile.min_gap,
                }
            ),
        )

    def _disturbed_event(
        self,
        asset: ProtectedAsset,
        ctx: DetectionContext,
        events: list[SecurityEvent],
    ) -> SecurityEvent | None:
        """A marked asset that looked different after someone was at it.

        The detectable half of a parcel or a bike going missing: COCO has no
        class for either, so the only evidence a camera holds is that the
        spot looks different at the end of the clip than at the start. Two
        things keep a lighting change from reading as a theft. The
        comparison is contrast-normalized and skipped whenever anybody stood
        in front of the asset in either frame compared (see
        vision/pipeline.py), and the event needs a subject to have actually
        been at the asset in between — a region that changed while nobody
        went near it is weather, not a visitor. Noteworthy whatever the
        type: the same evidence is a courier leaving a parcel and a resident
        collecting one, and telling those apart is the model's job.
        """
        if not asset.marked:
            return None
        change = ctx.marked_asset_changes.get(asset.key)
        if change is None or change < self._t.appearance_change:
            return None
        visits = [e for e in events if e.event_type in _VISIT_EVENTS]
        if not visits:
            return None
        visit = min(visits, key=lambda e: (-severity_rank(e.severity), e.start_offset))
        what = (
            "it may have been opened, closed or left ajar, or something may have "
            "been left at it"
            if asset.fixed
            else "something may have been taken, moved or left there"
        )
        return SecurityEvent(
            event_type=SecurityEventType.ASSET_DISTURBED,
            severity=Severity.NOTEWORTHY,
            confidence=0.5,
            detail=(
                f"The area marked around {asset.reference} looked different after "
                f"the {visit.subject_label} was at it — {what}."
            ),
            subject_label=visit.subject_label,
            track_id=visit.track_id,
            asset_name=asset.name,
            asset_type=str(asset.asset_type),
            start_offset=visit.start_offset,
            end_offset=ctx.timeline_end,
            evidence=_round_evidence({"appearance_change": change}),
        )

    # -- carryable objects ---------------------------------------------

    def _carryable_events(self, ctx: DetectionContext) -> list[SecurityEvent]:
        """Bags and cases appearing or disappearing while a person is around.

        COCO has no "parcel" class, so a delivered package most often lands
        in one of the bag classes — which makes this the closest thing to
        package-theft detection the detector honestly has. It requires a
        person to have been present, since an object the detector simply
        lost track of is otherwise indistinguishable from one that was
        taken. Confidence stays low on purpose.
        """
        if ctx.frame_count < 3:
            return []
        people = [t for t in ctx.tracks if t.label == PERSON_LABEL]
        if not people:
            return []
        midpoint = ctx.timeline_end / 2.0
        events: list[SecurityEvent] = []
        for track in ctx.tracks:
            if track.label not in CARRYABLE_LABELS:
                continue
            if track.first_offset <= 0.0 and track.last_offset < midpoint:
                events.append(self._carryable_event(track, removed=True))
            elif (
                track.first_offset > midpoint and track.last_offset >= ctx.timeline_end
            ):
                events.append(self._carryable_event(track, removed=False))
        return events

    @staticmethod
    def _carryable_event(track: ObjectTrack, removed: bool) -> SecurityEvent:
        if removed:
            return SecurityEvent(
                event_type=SecurityEventType.OBJECT_REMOVED,
                severity=Severity.SUSPICIOUS,
                confidence=0.45,
                detail=(
                    f"A {track.label} visible at the start of the clip was gone by "
                    "the end while a person was present — possible removal, though "
                    "the detector may simply have lost sight of it."
                ),
                subject_label=track.label,
                track_id=track.track_id,
                start_offset=track.first_offset,
                end_offset=track.last_offset,
                evidence=_round_evidence({"last_seen_offset": track.last_offset}),
            )
        return SecurityEvent(
            event_type=SecurityEventType.OBJECT_ADDED,
            severity=Severity.ROUTINE,
            confidence=0.45,
            detail=(
                f"A {track.label} that was not there earlier was present at the end "
                "of the clip — consistent with a delivery or drop-off."
            ),
            subject_label=track.label,
            track_id=track.track_id,
            start_offset=track.first_offset,
            end_offset=track.last_offset,
            evidence=_round_evidence({"first_seen_offset": track.first_offset}),
        )

    # -- scene ---------------------------------------------------------

    def _scene_events(self, ctx: DetectionContext) -> list[SecurityEvent]:
        """Camera interference: the view changed drastically, yet nothing is in it.

        A covered, sprayed, or repositioned camera looks exactly like this.
        So, unfortunately, does a floodlight switching on and a sunrise —
        which is why confidence stays low and the detail says so outright.
        """
        deviation = ctx.scene_deviation
        if deviation is None or deviation < self._t.obstruction_deviation:
            return []
        if ctx.tracks:
            return []
        return [
            SecurityEvent(
                event_type=SecurityEventType.CAMERA_OBSTRUCTION,
                severity=Severity.SUSPICIOUS,
                confidence=0.35,
                detail=(
                    f"This camera's view differs sharply from its usual background "
                    f"({deviation:.2f}/1.00) with nothing detected in it — possible "
                    "obstruction or repositioning, but a lighting or weather change "
                    "produces the same signal."
                ),
                start_offset=0.0,
                end_offset=ctx.timeline_end,
                evidence=_round_evidence({"scene_deviation": deviation}),
            )
        ]


#: Events that put a subject at an asset rather than merely in frame — what
#: :meth:`SecurityEventDetector._disturbed_event` needs before it will blame
#: a change in the asset's region on anyone.
_VISIT_EVENTS: frozenset[SecurityEventType] = frozenset(
    {
        SecurityEventType.ZONE_ENTERED,
        SecurityEventType.ASSET_PROXIMITY,
        SecurityEventType.ASSET_REACH,
        SecurityEventType.CONTACT_CANDIDATE,
        SecurityEventType.ANIMAL_ASSET_INTERACTION,
    }
)


def _track_key(event: SecurityEvent) -> tuple[object, ...]:
    """Identify the track an event belongs to, for cross-rule deduplication.

    ``track_id`` alone is not enough: every pseudo-track assembled without a
    real tracker carries ``None``, so keying on it would merge two different
    untracked people into one. The observed span disambiguates them.
    """
    return (event.track_id, event.subject_label, event.start_offset, event.end_offset)


def _asset_place(asset: ProtectedAsset) -> str:
    """Name the asset, or the space it left behind when it is not there.

    A zone rule still fires when the protected vehicle has been driven away
    — the area is worth watching whether or not the car is in it — but
    saying somebody "remained at the blue sedan" when this same pipeline has
    just concluded the blue sedan is not in frame is a claim the evidence
    contradicts, and it reaches both the prompt and the Security tab.
    """
    described = asset.reference
    if asset.present:
        return described
    return f"the space where {described} normally sits"


def _article(label: str) -> str:
    """ "A" or "An" for a detection label, so details read as English."""
    return "An" if label[:1].lower() in ("a", "e", "i", "o", "u") else "A"


def _feet_phrase(feet: float) -> str:
    """Render an approximate distance in the prompt's own 1ft/3ft terms."""
    if feet < 1.0:
        return "under a foot"
    return f"about {feet:.0f} ft"
