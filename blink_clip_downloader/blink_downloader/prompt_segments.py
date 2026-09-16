"""The individual sections a clip-analysis prompt is assembled from.

Each function here renders one self-contained block of prompt text from
plain arguments — a timestamp, a duration, a deviation score — and returns
"" when that block has nothing to say, which is how ``_build_prompt``
decides whether to include it at all. None of them consult analyzer state,
reach the network, or decide anything about a clip; they are the wording,
not the judgment.

Kept apart from ``analyzer.py`` for the same reason as ``model_catalog.py``
and ``frame_motion.py``: this is text that gets reworded on its own
schedule (a phrase that misleads a small model, a threshold that reads
better as a sentence) and it is easier to review a change to it against the
whole set of segments than buried among the request/response plumbing. The
module imports nothing heavy, so it stays cheap to load and to test.

Two segment builders deliberately stay on ``BaseAnalyzer``:
``_camera_context_segment`` and ``_car_protection_segment`` both read
configured analyzer state, and the latter is part of the safety-critical
protected-vehicle path described in CLAUDE.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .vision import VisionHints

# Deviation score (0.0-1.0, see ClipDatabase.get_scene_deviation) at or above
# which the prompt calls out that the scene looks different from usual.
SCENE_DEVIATION_ALERT_THRESHOLD: float = 0.12

# A clip at or under this length (seconds) is far more consistent with a
# single brief interaction — using a door, grabbing mail or a delivery,
# walking to/from a car — than with lingering, casing, or tampering, which
# typically take noticeably longer to unfold. See short_event_segment()
# below, added specifically to counter small vision models over-flagging
# ordinary quick coming-and-going as suspicious.
SHORT_EVENT_DURATION_SECONDS: float = 10.0


def vehicle_absent(vision_hints: VisionHints | None) -> bool:
    """True when identification concluded the protected vehicle is gone.

    Distinct from "not detected": other vehicles were found and none of
    them is the protected one, which is the driven-to-work case. A
    vehicle simply missed by the detector leaves this False, so the
    distance rules keep applying exactly as they did before.
    """
    asset = getattr(vision_hints, "asset", None)
    return asset is not None and not asset.present


def time_of_day_segment(clip_timestamp: str) -> str | None:
    """Time-of-day context (helps AI calibrate what's "normal").

    clip_timestamp is always UTC (Blink's API convention), but "is this
    normal for the time of day" is inherently a *local* question — a
    camera in any timezone west of UTC has its afternoon/evening clips
    fall on the UTC-hour boundaries this function used to read
    directly, mislabeling broad-daylight clips as "night" and
    defeating the whole point of telling the model to weigh the time
    of day at all. astimezone() with no argument converts to the
    system's configured local timezone (the same TZ the container gets
    from HA Supervisor that datetime.now() already relies on
    elsewhere, e.g. ai_schedule_start/end and digest_time).
    """
    if not clip_timestamp:
        return None
    try:
        from datetime import datetime as _dt

        dt = _dt.fromisoformat(clip_timestamp).astimezone()
        hour = dt.hour
        if hour < 5:
            tod = "late night"
        elif hour < 9:
            tod = "early morning"
        elif hour < 12:
            tod = "morning"
        elif hour < 17:
            tod = "afternoon"
        elif hour < 20:
            tod = "evening"
        else:
            tod = "night"
        return (
            f"\n\nTime of day: {tod} ({dt.strftime('%H:%M')} local time). "
            "Factor this into your assessment of whether the activity is normal."
        )
    except Exception:  # noqa: BLE001
        return None


def anomaly_alert_segment(anomaly_score: float) -> str | None:
    """Behavior anomaly alert (temporal — unusual hour/duration for camera)."""
    if anomaly_score < 0.6:
        return None
    return (
        f"\n\nBEHAVIOR ALERT: This event is statistically unusual for "
        f"this camera at this time (anomaly score {anomaly_score:.2f}/1.00). "
        "Apply heightened scrutiny to any persons or vehicles in the frame."
    )


def short_event_segment(clip_duration: float) -> str | None:
    """Short-event hint — a code-computed signal from the clip's real
    duration (see SHORT_EVENT_DURATION_SECONDS) that a quick, single
    interaction is far more consistent with routine coming-and-going
    than with lingering, casing, or tampering. Framed as a hint the
    model can override, not a rule, since a genuinely short but
    visibly suspicious clip (e.g. a quick tamper-and-flee) must still
    be flagged on its own visible content."""
    if not (0 < clip_duration <= SHORT_EVENT_DURATION_SECONDS):
        return None
    return (
        "\n\nSHORT EVENT: This entire clip lasts only "
        f"{clip_duration:.0f} seconds. A brief, single interaction "
        "like this — walking up, using a door, grabbing mail or a "
        "delivery, or heading to a car — is far more consistent "
        "with ordinary coming and going than with lingering, "
        "casing, or tampering, which typically take noticeably "
        "longer to unfold. Treat this as a hint favoring a routine "
        "read, not a verdict on its own — only set suspicious=true "
        "if the frames themselves clearly show concrete suspicious "
        "behavior (forced entry, lock tampering, casing, etc.), "
        "not brevity alone."
    )


def scene_baseline_segment(
    scene_deviation: float | None, car_applies: bool
) -> str | None:
    """Visual scene-baseline ("smart brain") signal. This camera is fixed
    in place, so its background should look almost the same clip after
    clip. A large deviation suggests something new (object, vehicle,
    obstruction) is in frame; a close match is a strong signal this is
    routine, which helps suppress false positives on ordinary daily
    activity."""
    if scene_deviation is None:
        return None
    if scene_deviation >= SCENE_DEVIATION_ALERT_THRESHOLD:
        return (
            "\n\nSCENE BASELINE: This camera's view currently differs from its "
            f"usual background (deviation {scene_deviation:.2f}/1.00). Something "
            "not normally present — an object, vehicle, or obstruction — may be "
            "in frame. Treat this as a hint to look closer, not a verdict on its own. "
            "This deviation is frequently just lighting, weather, shadows, or the "
            "day/night transition — none of which are suspicious by themselves. "
            "Only raise suspicion if the frames themselves clearly show a specific "
            "new person, animal, or vehicle; if you cannot identify one, describe "
            "the scene plainly and set suspicious=false."
        )
    if car_applies:
        # Deliberately omitted when car_applies: the protected vehicle
        # parked in its usual spot IS this camera's learned "usual
        # background" by definition, so a low deviation score here
        # means nothing more than "the car is where it always is" —
        # it says nothing about whether a person is *currently* right
        # next to it, since the opening frame this score is computed
        # from is captured at or before the moment motion begins,
        # often before someone has walked into contact with the car.
        # A "favor calm read" framing on every single clip from this
        # camera would actively work against the strict PROTECTED
        # VEHICLE distance rules below, which must be the sole
        # authority for this camera's verdict — this was found to
        # measurably suppress genuine close-proximity notifications
        # (e.g. a person leaning on or standing within a foot of the
        # vehicle) that the distance rules alone correctly catch.
        return None
    return (
        "\n\nSCENE BASELINE: This camera's view closely matches its usual "
        "background — no unexplained new objects. Favor a calm, routine read "
        "of the activity unless the frames themselves show something genuinely "
        "concerning."
    )


def trajectory_segment(trajectory_hint: str | None) -> str | None:
    """Movement-trajectory hint ("smart brain" motion reasoning) — a
    rough automated estimate of direction/intensity trend across the
    selected frames, derived from frame-to-frame pixel differences
    already computed during frame selection. Explicitly framed as a
    coarse hint so the model relies on what it can actually see, not
    this estimate, for its final judgment of
    approaching/retreating/lingering behavior."""
    if not trajectory_hint:
        return None
    return (
        "\n\nMOVEMENT: Across these frames, the main area of motion "
        f"appears to be {trajectory_hint}. This is a rough automated "
        "estimate from frame-to-frame pixel differences, not a precise "
        "tracked path — use it only as a coarse hint about direction "
        "of travel, and rely on what you can actually see in the "
        "frames for your judgment of approaching/retreating/lingering "
        "behavior."
    )


def corrections_segment(
    recent_corrections: list[dict[str, Any]] | None,
) -> str | None:
    """Recent human corrections for this camera (adaptive learning — see
    ClipDatabase.get_prompt_corrections). Bounded to a handful of the
    most recent notes so this can't grow unbounded or drown out the
    rest of the prompt, and framed as a hint rather than a rule so the
    model still judges this specific clip on its own visible content."""
    if not recent_corrections:
        return None
    correction_lines = "\n".join(
        "- A past clip on this camera was marked "
        f"{'suspicious' if c.get('original_suspicious') else 'not suspicious'} "
        "by the AI, but a human reviewer said this was WRONG. "
        f'Reviewer\'s note: "{str(c.get("correction_note", ""))[:200]}"'
        for c in recent_corrections[:3]
        if c.get("correction_note")
    )
    if not correction_lines:
        return None
    return (
        "\n\nRECENT HUMAN CORRECTIONS on this camera (learn from "
        "these, but judge THIS clip on its own visible content — "
        "do not assume the same pattern repeats):\n" + correction_lines
    )


def zone_motion_segment(zone_motion_fraction: float | None) -> str | None:
    """Zone-motion evidence — a code-computed signal (see
    frame_motion.zone_motion_fraction) telling the model what share of
    this clip's overall pixel motion actually fell inside the
    configured car zone, versus happening elsewhere in the frame. Only
    emitted when a zone is configured and there's enough clip motion to
    attribute meaningfully."""
    if zone_motion_fraction is None:
        return None
    if zone_motion_fraction >= 0.5:
        return (
            "\n\nZONE MOTION: "
            f"{zone_motion_fraction:.0%} of this clip's overall motion occurred "
            "within the configured car zone — the activity is concentrated at or "
            "near the protected vehicle's usual spot, not just passing through the "
            "wider frame."
        )
    return (
        "\n\nZONE MOTION: "
        f"Only {zone_motion_fraction:.0%} of this clip's overall motion occurred "
        "within the configured car zone — most of the activity is happening "
        "elsewhere in the frame, away from the protected vehicle's usual spot. "
        "Do not assume the vehicle is involved just because something moved "
        "somewhere in frame."
    )


def vision_hint_segments(vision_hints: VisionHints | None) -> list[str]:
    """Optional computer-vision pipeline hints (see vision.py) — each is
    independently toggled in config and already fully formatted with its
    own section header and hedging language; simply collect whichever
    ones a given clip actually produced. None of these exist unless
    attach_vision_pipeline() was called and the corresponding stage is
    enabled."""
    if vision_hints is None:
        return []
    return [
        hint
        for hint in (
            vision_hints.detection_hint,
            vision_hints.tracking_hint,
            vision_hints.depth_hint,
            vision_hints.contact_hint,
            vision_hints.posture_hint,
            vision_hints.recognized_resident_hint,
        )
        if hint
    ]


def output_rules_segment(camera: str, car_applies: bool) -> str:
    """Ensure the description stays human-readable, never exposes internal
    data, and never borrows scenery from a different camera. The example
    phrase is only about vehicle distance when this specific camera has
    car-distance rules in play (car_applies) — otherwise it uses a
    camera-agnostic example so smaller models aren't nudged into
    inventing a car or driveway that this camera cannot actually see
    (each camera has its own field of view)."""
    # Both variants deliberately pair a person-shaped phrase with a
    # subject-free one. When every example named a person, a small model
    # handed a clip of an empty driveway would reliably produce one
    # anyway — the examples are the strongest prior in a prompt this
    # long, and they all pointed the same way.
    example_phrase = (
        "Use natural phrases like 'standing about 2 feet from the car', "
        "'walking past the driveway', or 'the car is parked as usual with nobody near it'. "
        if car_applies
        else "Use natural phrases like 'standing near the front steps', "
        "'walking across the yard', or 'the yard is empty and nothing is moving'. "
    )
    return (
        "\n\nOUTPUT RULES: The 'description' field must be written in plain English "
        "as if explaining to a homeowner what happened, and must describe ONLY what is "
        f"actually visible in these specific frames from the {camera} camera — never assume "
        "objects or areas seen by other cameras on the property are visible here. "
        "Keep it SHORT: one sentence, or at most two only when genuinely necessary — "
        "about the length of 'A person is walking past the car', 'A car is parked in the "
        "driveway with nobody around it', or 'A person is standing very close to the car "
        "and appears to be looking inside it'. State only the "
        "notable person, vehicle, or animal and what they are doing, from a security-"
        "monitoring perspective focused on this property's assets. "
        "Every example above is only a guide to LENGTH and TONE — never to content. "
        "Describe the subjects that are actually in these frames: if no person is "
        "present, do not put one in the description, and if nothing is happening at "
        "all, say so plainly (e.g. 'Nothing notable — the parked cars are "
        "undisturbed and nobody is around'). A clip where nothing happened is a "
        "normal and expected result, not a failure to find something. Do NOT list or "
        "describe static background scenery that isn't part of the notable activity — "
        "other parked vehicles that aren't involved, houses, weather, foliage or grass, "
        "utility poles, power lines, and general neighborhood description all add nothing "
        "to a security summary and must be left out entirely. "
        + example_phrase
        + "Identify subjects accurately: state plainly whether you see a person, a "
        "vehicle (car, truck, van, motorcycle), or an animal — never describe a "
        "vehicle's movement as if it were a person, or vice versa. A vehicle simply "
        "moving along the street without stopping, parking, or approaching a person "
        "or property is routine traffic — describe it plainly, e.g. 'a car drove up "
        "the street', and do not say it was 'near' anyone or anything unless it "
        "actually stopped or slowed close to a specific person, vehicle, or entryway. "
        "A person or animal simply walking, running, or otherwise passing through the "
        "frame — on a sidewalk, street, or yard — without stopping, lingering, or "
        "interacting with anything on the property is routine and must be marked "
        "suspicious=false: merely being visible to a security camera is never "
        "suspicious by itself, only stopping, lingering, tampering, or clearly "
        "unusual behavior is. "
        "Write in the calm, factual tone of a professional security analyst — state "
        "only what is observable, avoid speculation or alarmist language, and reserve "
        "words like 'suspicious' for genuine cause for concern. "
        "If you set suspicious=true, confidence must be at least 0.5 — that value is "
        "not a hedge, it means you identified a specific person, animal, or vehicle "
        "actually behaving in a concerning way (lingering, tampering, or approaching "
        "closely as described above). An ambient change alone — lighting, weather, "
        "shadows, or a shift in background scenery — is never sufficient for "
        "suspicious=true. If your certainty is below 0.5, set suspicious=false instead "
        "of reporting a low-confidence guess. "
        "NEVER include any of these technical terms in the description: "
        "'bounding box', 'normalized', 'frame width', 'frame percentage', 'spatial data', "
        "'INTERNAL', 'CONTEXT', 'proximity analysis', 'overlap', 'gap 0.', 'risk score', "
        "'evidence quality', 'protection zone', 'track', 'detector', or any decimal "
        "coordinates, percentages, or clip timestamps quoted from the sections above. "
        "Any internal proximity, tracking or security-evidence hints provided are for "
        "your reasoning only — do not quote them."
    )
