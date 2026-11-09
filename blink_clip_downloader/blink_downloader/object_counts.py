"""How many of each thing a clip held, from its stored per-frame boxes.

``detected_objects`` holds one row per box per sampled frame, so the number
of rows is never the answer. The answer is the number of distinct track ids
— floored by the most seen in any one frame — with one correction, which
is the reason this is a module rather than a line of SQL: **a thing that
stayed where it was is one thing, however many ids the tracker gave it.**

The tracker is handed frames sampled seconds apart. It drops a track it
has not matched for a while and opens a new one when the object is detected
again, so a parked car that goes unseen for a few frames (headlights sweep
over it, someone walks in front of it, the camera switches to infrared)
comes back under a second id. Six parked cars were reported as eight that
way.

The correction is for the chips only. The security layer keeps the
tracker's own identities (``security/tracks.py``), and a person is never
merged here: "2 separate people were tracked" is a claim the events make
from those ids, and the chip beside it must not contradict it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .security.geometry import Box, box_iou
from .security.tracks import PERSON_LABEL

#: How much two boxes must overlap (intersection over union) for two track
#: ids to be the same object left where it was. A judgement call, not a
#: measurement: a parked car detected again is boxed almost identically
#: (well above this), while a car that takes another's place rarely lines up
#: this closely. The rule that the two were never seen in the same frame
#: does the rest.
SAME_PLACE_IOU = 0.5


@dataclass
class _Track:
    """One track id's sightings: which frames, and the box in each."""

    boxes: dict[int, Box] = field(default_factory=dict)

    def nearest_boxes(self, other: _Track) -> tuple[Box, Box]:
        """The pair of boxes, one from each, closest together in time."""
        mine, theirs = min(
            ((a, b) for a in self.boxes for b in other.boxes),
            key=lambda pair: (abs(pair[0] - pair[1]), pair),
        )
        return self.boxes[mine], other.boxes[theirs]


def _same_object(track: _Track, group: list[_Track]) -> float:
    """How well *track* continues *group*, or 0.0 when it cannot be it.

    It cannot when any member of the group was seen in a frame *track* was
    also seen in: two boxes in one frame are two objects, whatever their
    overlap. Otherwise the score is the overlap with the member's box
    nearest in time, and anything under ``SAME_PLACE_IOU`` is no match.
    """
    best = 0.0
    for member in group:
        if track.boxes.keys() & member.boxes.keys():
            return 0.0
        best = max(best, box_iou(*track.nearest_boxes(member)))
    return best if best >= SAME_PLACE_IOU else 0.0


def _distinct_objects(tracks: list[_Track]) -> int:
    """Count *tracks* after joining the ones that are one object in place."""
    groups: list[list[_Track]] = []
    for track in sorted(tracks, key=lambda t: min(t.boxes)):
        scored = [(_same_object(track, group), i) for i, group in enumerate(groups)]
        score, index = max(scored, default=(0.0, -1), key=lambda s: (s[0], -s[1]))
        if score > 0.0:
            groups[index].append(track)
        else:
            groups.append([track])
    return len(groups)


def summarize_detected_objects(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate one clip's ``detected_objects`` rows by label.

    Returns ``{label, count, detections, max_confidence}`` per label, most
    numerous first (ties: more boxes first, then by label). ``count`` is how
    many distinct ones appeared; ``detections`` is the raw box total.
    """
    per_frame: dict[str, dict[int, int]] = {}
    tracks: dict[str, dict[int, _Track]] = {}
    best: dict[str, float] = {}
    for row in rows:
        label = row["label"]
        frame = int(row["frame_index"] or 0)
        frames = per_frame.setdefault(label, {})
        frames[frame] = frames.get(frame, 0) + 1
        best[label] = max(best.get(label, 0.0), float(row["confidence"] or 0.0))
        if row["track_id"] is not None:
            track = tracks.setdefault(label, {}).setdefault(row["track_id"], _Track())
            track.boxes[frame] = (
                float(row["box_x1"]),
                float(row["box_y1"]),
                float(row["box_x2"]),
                float(row["box_y2"]),
            )

    summary = []
    for label, frames in per_frame.items():
        label_tracks = list(tracks.get(label, {}).values())
        tracked = (
            len(label_tracks)
            if label == PERSON_LABEL
            else _distinct_objects(label_tracks)
        )
        summary.append(
            {
                "label": label,
                # The per-frame peak is the floor: it covers rows stored
                # with no track id at all, where counting ids gives zero.
                "count": max(max(frames.values()), tracked),
                "detections": sum(frames.values()),
                "max_confidence": best[label],
            }
        )
    summary.sort(key=lambda r: (-r["count"], -r["detections"], r["label"]))
    return summary
