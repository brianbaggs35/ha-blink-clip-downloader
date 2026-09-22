"""Tests for face_enrollment.py: holding, pruning and grouping scan results,
and reviewing what is already enrolled."""

from __future__ import annotations

import math

from blink_downloader.face_enrollment import (
    FaceCandidate,
    FaceCandidateStore,
    group_candidates,
    review_enrollments,
    suppress_near_duplicates,
)


def _unit(angle_degrees: float) -> list[float]:
    """A 2-d unit vector; the cosine between two is cos(their angle apart)."""
    radians = math.radians(angle_degrees)
    return [math.cos(radians), math.sin(radians)]


def _candidate(candidate_id: str, angle: float, quality: float = 0.5) -> FaceCandidate:
    return FaceCandidate(
        id=candidate_id,
        embedding=_unit(angle),
        thumbnail=b"",
        quality=quality,
        created=0.0,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# FaceCandidateStore
# ---------------------------------------------------------------------------


def test_store_round_trips_a_candidate() -> None:
    store = FaceCandidateStore()
    candidate_id = store.add([0.1, 0.2], b"jpeg", 0.7)
    candidate = store.get(candidate_id)
    assert candidate is not None
    assert candidate.embedding == [0.1, 0.2]
    assert candidate.thumbnail == b"jpeg"
    assert candidate.quality == 0.7


def test_store_ids_are_unguessable_and_distinct() -> None:
    store = FaceCandidateStore()
    ids = {store.add([0.0], b"", 0.0) for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) >= 16 for i in ids)


def test_store_take_removes_the_candidate() -> None:
    store = FaceCandidateStore()
    candidate_id = store.add([0.1], b"", 0.5)
    assert store.take(candidate_id) is not None
    assert store.take(candidate_id) is None
    assert store.get(candidate_id) is None


def test_store_unknown_id_is_none() -> None:
    assert FaceCandidateStore().get("nope") is None


def test_store_expires_old_candidates() -> None:
    clock = _Clock()
    store = FaceCandidateStore(ttl=60.0, clock=clock)
    old = store.add([0.1], b"", 0.5)
    clock.now += 30
    newer = store.add([0.2], b"", 0.5)
    clock.now += 31
    assert store.get(old) is None
    assert store.get(newer) is not None


def test_store_evicts_the_oldest_beyond_capacity() -> None:
    store = FaceCandidateStore(capacity=2)
    first = store.add([0.1], b"", 0.5)
    second = store.add([0.2], b"", 0.5)
    third = store.add([0.3], b"", 0.5)
    assert store.get(first) is None
    assert store.get(second) is not None
    assert store.get(third) is not None


# ---------------------------------------------------------------------------
# suppress_near_duplicates
# ---------------------------------------------------------------------------


def test_suppress_near_duplicates_empty() -> None:
    assert suppress_near_duplicates([], []) == []


def test_suppress_near_duplicates_keeps_the_best_of_a_run_of_shots() -> None:
    # Three near-identical shots (cos 1° = 0.9998) and one different face.
    embeddings = [_unit(0), _unit(1), _unit(60), _unit(0.5)]
    qualities = [0.4, 0.9, 0.5, 0.6]
    kept = suppress_near_duplicates(embeddings, qualities)
    assert kept == [1, 2]


def test_suppress_near_duplicates_keeps_distinct_views_of_one_person() -> None:
    """cos 25° ≈ 0.91: the same person from another angle is still worth
    offering, only a near-copy is not."""
    kept = suppress_near_duplicates([_unit(0), _unit(25)], [0.5, 0.5])
    assert sorted(kept) == [0, 1]


def test_suppress_near_duplicates_tolerates_a_zero_vector() -> None:
    kept = suppress_near_duplicates([[0.0, 0.0], _unit(0)], [0.9, 0.1])
    assert kept == [0, 1]


# ---------------------------------------------------------------------------
# group_candidates
# ---------------------------------------------------------------------------


def test_group_candidates_empty() -> None:
    assert group_candidates([]) == []


def test_group_candidates_separates_people_and_orders_by_size() -> None:
    candidates = [
        _candidate("a1", 0),
        _candidate("b1", 90),
        _candidate("a2", 10),
        _candidate("a3", 20),
        _candidate("b2", 95),
    ]
    groups = group_candidates(candidates)
    assert [sorted(g) for g in groups] == [["a1", "a2", "a3"], ["b1", "b2"]]


def test_group_candidates_lists_the_best_face_first() -> None:
    groups = group_candidates(
        [_candidate("worse", 0, quality=0.2), _candidate("better", 5, quality=0.9)]
    )
    assert groups == [["better", "worse"]]


def test_group_candidates_splits_rather_than_merges_a_borderline_pair() -> None:
    """cos 45° ≈ 0.71 — as close as two *different* people were measured to
    get. Merging them would put a stranger's face under someone's name."""
    groups = group_candidates([_candidate("x", 0), _candidate("y", 45)])
    assert sorted(groups) == [["x"], ["y"]]


def test_group_candidates_one_outlier_cannot_bridge_two_people() -> None:
    """A blurry in-between face joins the nearer person; it does not chain
    the two people into one group the way pairwise linking would."""
    candidates = [
        _candidate("a1", 0, quality=0.9),
        _candidate("a2", 5, quality=0.8),
        _candidate("bridge", 40, quality=0.1),
        _candidate("b1", 80, quality=0.9),
        _candidate("b2", 75, quality=0.8),
    ]
    groups = group_candidates(candidates)
    assert all(not ({"a1", "b1"} <= set(group)) for group in groups)


# ---------------------------------------------------------------------------
# review_enrollments
# ---------------------------------------------------------------------------


def _enrollment(enrollment_id: int, name: str, angle: float) -> dict:
    return {"id": enrollment_id, "name": name, "embedding": _unit(angle)}


def test_review_needs_at_least_two_photos() -> None:
    assert review_enrollments([]) == {}
    assert review_enrollments([_enrollment(1, "Amy", 0)]) == {}


def test_review_flags_a_photo_unlike_the_persons_others() -> None:
    enrollments = [
        _enrollment(1, "Amy", 0),
        _enrollment(2, "Amy", 10),
        _enrollment(3, "Amy", 80),  # cos 70° to the nearest ≈ 0.34
    ]
    assert review_enrollments(enrollments) == {
        3: {"unlike_others": True, "also_matches": ""}
    }


def test_review_flags_a_photo_that_matches_someone_else() -> None:
    enrollments = [
        _enrollment(1, "Amy", 0),
        _enrollment(2, "Amy", 5),
        _enrollment(3, "Ben", 90),
        _enrollment(4, "Ben", 12),  # cos 7° to Amy's second photo
    ]
    warnings = review_enrollments(enrollments)
    assert warnings[4] == {"unlike_others": True, "also_matches": "Amy"}
    assert warnings[2]["also_matches"] == "Ben"


def test_review_a_person_with_one_photo_is_never_unlike_themselves() -> None:
    warnings = review_enrollments([_enrollment(1, "Amy", 0), _enrollment(2, "Ben", 90)])
    assert warnings == {}


def test_review_skips_rows_that_cannot_be_compared() -> None:
    """A stored embedding of another length (an older model, a corrupted
    row) makes the whole matrix meaningless — review nothing rather than
    raise, since this runs on every visit to the tab."""
    enrollments = [
        {"id": 1, "name": "Amy", "embedding": [1.0, 0.0]},
        {"id": 2, "name": "Amy", "embedding": [1.0, 0.0, 0.0]},
        {"id": 3, "name": "Amy", "embedding": []},
    ]
    assert review_enrollments(enrollments) == {}
