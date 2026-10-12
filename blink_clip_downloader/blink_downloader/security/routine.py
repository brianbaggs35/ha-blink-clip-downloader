"""Telling an ordinary trip through a door from someone working at it.

A front door is the camera that cries wolf. Its owner arrives, unlocks the
door with their back to the lens, and goes in; the face is never seen, so the
face-recognition bypass has nothing to work with, and an AI model that cannot
see what their hands are doing at the lock hedges with a low-confidence
"suspicious". What the owner means by suspicious is different and
describable: someone hanging around, working at the lock for a long time,
trying the handle and leaving.

This module is the *behavioural* line of evidence for that. It does not
identify anybody -- it cannot, from behind -- so it is built to be harder to
satisfy than the face bypass, not easier:

* the clip must be **nothing but** one person's brief trip through a marked
  door (:func:`routine_door_visit`): the same asset throughout, no other
  person, nothing at or above the suspicious band, no lingering, and evidence
  good enough to trust the tracking it rests on;
* it may only lower a flag the model raised **unsure of itself**
  (:func:`flag_can_be_cleared`): a confident finding of tampering stands, and
  how unsure counts depends on what the face evidence says -- a household
  member matched in the clip widens it;
* a visit is only weighed at all (:func:`door_use_allowed`) at an hour this
  camera's own history says it sees activity in, unless a household member
  was positively matched, and never beside a clear look at a face that matches
  nobody (that veto is applied by the analyzer, which is the only place the
  faces are in hand). Decided before the model is asked, because a visit also
  reaches the prompt: telling the model a visit is ordinary use of a door at
  an hour nobody comes home would soften its own verdict on the very clip
  that most needs it left alone.

Nothing here changes a verdict by itself. The analyzer applies it, behind
``ai_routine_door_use_enabled`` (off by default), and records that it did.
"""

from __future__ import annotations

from .events import SecurityEvent, SecurityEventType, Severity, severity_rank
from .evidence import EvidenceQuality
from .tracks import PERSON_LABEL

#: Model confidence at or above which a flag is never cleared as routine door
#: use. The analysis prompt asks for 0.5 or more on any suspicious verdict and
#: ties 0.8 and above to the plainest cases ("touching... confidence >= 0.8");
#: a flag that carries a hedge sits at the bottom of that range. Judgement,
#: not a measurement -- there is no corpus of real front-door clips to
#: calibrate against -- so it errs toward leaving the flag alone.
ROUTINE_VISIT_MAX_CONFIDENCE = 0.7

#: The same, when a household member was positively matched in the clip. This
#: reaches a little into the 0.8 and above the prompt ties to the plainest
#: findings -- the face bypass, which needs every face matched, would have
#: cleared such a clip at any confidence -- but stops well short of certain: a
#: face the model was told is known does not make a sure finding of forcing a
#: lock any less of one. Judgement, like the cap above.
ROUTINE_VISIT_MAX_CONFIDENCE_CONFIRMED = 0.85

#: Anomaly score (0.0-1.0, ``ClipDatabase.get_anomaly_score``) at or above
#: which this camera's own history calls the clip unusual. The score adds the
#: hour's rarity to how unusual the clip's length is, so this is reached by the
#: rarest two hour bands on their own (0.35 for a rarely used hour, 0.5 for one
#: it has never seen) or by an uncommon hour (0.15) with a clip of a very
#: unusual length (0.25). Below it the hour is one the camera has seen enough
#: activity in for a visit to be ordinary: "coming and going regularly", in the
#: owner's words. Zero until the camera has 30 events of history, so a new
#: install is not blocked by having nothing to compare with.
ROUTINE_VISIT_MAX_ANOMALY = 0.35

#: What one trip through a door produces besides the :data:`DOOR_USED`
#: event itself: someone was there, walked up, came close and perhaps went
#: away again, and the door's own image changed because it opened. Anything
#: else in the clip is something the visit does not explain.
_EXPLAINED_BY_A_VISIT: frozenset[SecurityEventType] = frozenset(
    {
        SecurityEventType.DOOR_USED,
        SecurityEventType.SUBJECT_PRESENT,
        SecurityEventType.ZONE_ENTERED,
        SecurityEventType.ASSET_APPROACHED,
        SecurityEventType.ASSET_PROXIMITY,
        SecurityEventType.RETREAT,
        SecurityEventType.ASSET_DISTURBED,
    }
)


def routine_door_visit(
    events: list[SecurityEvent], evidence: EvidenceQuality
) -> SecurityEvent | None:
    """The :data:`DOOR_USED` event, when it accounts for the whole clip.

    ``None`` unless there is exactly one such event (two doors, or two
    people, leave a clip nobody can read as a single visit) and everything
    else in the clip is something a visit explains: only the event types in
    :data:`_EXPLAINED_BY_A_VISIT`, none at or above the suspicious band,
    every one about the same asset or none, and any event about a person is
    about that same person -- a second person, tracked or not, always
    disqualifies it. Weak evidence disqualifies it too: every claim here is that
    something did *not* happen (no lingering, no tampering), and a dark,
    tiny, barely tracked figure supports no such claim.
    """
    visits = [e for e in events if e.event_type is SecurityEventType.DOOR_USED]
    if len(visits) != 1 or evidence.label == "weak":
        return None
    visit = visits[0]
    suspicious = severity_rank(Severity.SUSPICIOUS)
    for event in events:
        if (
            event.event_type not in _EXPLAINED_BY_A_VISIT
            or severity_rank(event.severity) >= suspicious
            or (event.asset_name and event.asset_name != visit.asset_name)
            or (
                event.subject_label == PERSON_LABEL and event.track_id != visit.track_id
            )
        ):
            return None
    return visit


def door_use_allowed(anomaly_score: float, *, identity_confirmed: bool) -> bool:
    """Whether a visit to a door may be weighed at all at this hour.

    *identity_confirmed* is a household member having been positively matched
    in the clip, which drops the unusual-hour condition: the face bypass has
    never cared what time a recognized member comes home. Without one, the
    hour must be one this camera's own history says it sees activity in.
    """
    return identity_confirmed or anomaly_score < ROUTINE_VISIT_MAX_ANOMALY


def flag_can_be_cleared(confidence: float, *, identity_confirmed: bool) -> bool:
    """Whether a flag the model raised at *confidence* may be lowered because
    the clip is a routine visit to a door.

    With a household member matched in the clip the model may have been
    surer, though never as sure as the prompt asks it to be of the plainest
    cases; without one it must have been plainly unsure.
    """
    cap = (
        ROUTINE_VISIT_MAX_CONFIDENCE_CONFIRMED
        if identity_confirmed
        else ROUTINE_VISIT_MAX_CONFIDENCE
    )
    return confidence < cap
