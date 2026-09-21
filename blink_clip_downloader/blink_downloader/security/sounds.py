"""Turning classified sounds into security events.

The camera cannot see round a corner and cannot see in the dark, but the
microphone hears both. Glass going at the back of the house while nothing
at all is in frame is the case a purely visual security layer is blind to
by construction, and it is not a marginal one — it is what a break-in
sounds like.

This module is the audio half of :mod:`.detector`. It is deliberately much
smaller and much more conservative, because the evidence is much weaker: a
sound classifier working on compressed outdoor camera audio is wrong far
more often than bounding boxes are. Three rules keep that honest:

* **Only sounds with a near-zero routine-household rate raise an event.**
  That is the same test :data:`.events.BYPASS_BLOCKING_EVENTS` applies.
  Speech, footsteps, a dog, a car, a door, a power tool all reach the AI
  as a hint and stop there: each is something an ordinary household
  produces daily, and an event that fires daily is noise wearing a
  security label. Breaking glass, gunfire and an alarm going off are not
  daily.
* **The bar to raise an event is far higher than the bar to mention one.**
  ``vision.audio`` reports anything over 15% because the AI weighs it
  against the frames. An event here moves a risk score and can raise an
  alert by itself, so it needs :data:`_EVENT_CONFIDENCE`.
* **Nothing here is a verdict.** These are events like any other: scored
  in :mod:`.scoring`, described to the AI provider in :mod:`.narrative` as
  something to reconcile with what it can see.

Nothing in this package imports :mod:`blink_downloader.vision` — the
classified labels arrive as plain ``(label, score)`` pairs, so this module
and its tests need no torch, no transformers and no audio at all.
"""

from __future__ import annotations

import re

from .events import SecurityEvent, SecurityEventType, Severity

#: Confidence a classified sound needs before it becomes an event rather
#: than merely a hint. Judgement, not a measurement: the classifier's
#: real-world false-positive rate on Blink audio is not something this
#: repository can measure, so the bar is set well above the 15% reporting
#: floor and the consequences of being wrong are bounded by the fact that
#: the AI provider is told the sound is a guess and asked to reconcile it
#: with the frames.
_EVENT_CONFIDENCE = 0.5


def matches_any(label: str, keywords: frozenset[str]) -> bool:
    """True when *label* contains any of *keywords* as a whole word.

    Word boundaries rather than a plain substring test, so "Cartoon" is
    not a car and "Glasses" is not breaking glass. Keyword matching rather
    than an exact class allow-list because AudioSet checkpoints spell the
    same class several ways ("Gunshot, gunfire", "Smoke detector, smoke
    alarm") and an allow-list pinned to one spelling silently matches
    nothing the day the model id changes.
    """
    lowered = label.lower()
    return any(
        re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered)
        for word in keywords
    )


#: Glass being broken. "Breaking" on its own is deliberately absent — it
#: is a class in its own right and matches far too much.
GLASS_KEYWORDS: frozenset[str] = frozenset({"glass", "shatter", "smash"})

#: Gunfire and explosions. Rare enough at a home that a false positive
#: costs one notification, and severe enough that a miss costs everything.
GUNSHOT_KEYWORDS: frozenset[str] = frozenset({"gunshot", "gunfire", "explosion"})

#: An alarm actually sounding: a car alarm, a house alarm, a smoke
#: detector. A passing emergency siren matches too and is the known false
#: positive here, which is why this one scores well below the other two
#: and cannot force an alert on its own.
ALARM_KEYWORDS: frozenset[str] = frozenset({"alarm", "siren", "smoke detector"})

_RULES: tuple[tuple[frozenset[str], SecurityEventType, Severity, str], ...] = (
    (
        GLASS_KEYWORDS,
        SecurityEventType.GLASS_BREAK_HEARD,
        Severity.CRITICAL,
        "Breaking glass was heard",
    ),
    (
        GUNSHOT_KEYWORDS,
        SecurityEventType.GUNSHOT_HEARD,
        Severity.CRITICAL,
        "A gunshot or explosion was heard",
    ),
    (
        ALARM_KEYWORDS,
        SecurityEventType.ALARM_HEARD,
        Severity.SUSPICIOUS,
        "An alarm or siren was heard",
    ),
)


def detect_audio_events(
    labels: list[tuple[str, float]] | None,
) -> list[SecurityEvent]:
    """Raise a security event for each alarming sound in *labels*.

    *labels* are ``(class name, confidence)`` pairs as a sound classifier
    produced them — already filtered for relevance, in no particular
    order. Everything that is merely informative produces nothing here.

    Offsets are zero because there is nothing truthful to put there: the
    classifier reports *what* it heard across the whole window it was
    given, never *when* within it. Zero is not a measurement here, and the
    UI does render it as one — a "0s" seek button on the timeline row like
    any other event's. That is accepted rather than fixed, because the
    alternative is a nullable offset threaded through the events table,
    the API and two components for a cosmetic gain; the event's own detail
    text says "Audio only" instead, everywhere the offset is shown.
    """
    events: list[SecurityEvent] = []
    for label, score in labels or []:
        if score < _EVENT_CONFIDENCE:
            continue
        for keywords, event_type, severity, detail in _RULES:
            if not matches_any(label, keywords):
                continue
            events.append(
                SecurityEvent(
                    event_type=event_type,
                    severity=severity,
                    confidence=min(1.0, max(0.0, score)),
                    detail=(
                        f"{detail} — a sound classifier matched this clip's audio "
                        f"to '{label}' with {score:.0%} confidence. Audio only: "
                        "nothing was seen, and the sound may come from outside "
                        "the camera's view."
                    ),
                    evidence={"sound": label, "sound_confidence": round(score, 4)},
                )
            )
            break
    return events
