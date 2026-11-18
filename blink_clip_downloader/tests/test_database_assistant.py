"""The Security Assistant's database half: activity summaries, chats, usage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from blink_downloader.assistant_search import ClipSearch, SearchTerm
from blink_downloader.database import ClipDatabase
from blink_downloader.security import SecurityEvent, SecurityEventType, Severity

NOW = datetime.now(UTC)


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


async def _clip(db: ClipDatabase, clip_id: str, camera: str, hours_ago: float) -> None:
    await db.add_clip(
        {
            "id": clip_id,
            "camera": camera,
            "path": f"/tmp/{clip_id}.mp4",
            "timestamp": _ts(hours_ago),
            "size_bytes": 10,
            "duration": 6,
            "source": "pir",
            "network_id": 1,
        }
    )


async def _analysis(
    db: ClipDatabase,
    clip_id: str,
    camera: str,
    *,
    suspicious: bool = False,
    severity: str = "routine",
    summary: str = "A person walks by.",
    analyzed_hours_ago: float = 0.0,
    **extra: Any,
) -> None:
    await db.add_analysis_result(
        {
            "clip_id": clip_id,
            "camera": camera,
            "model": "m",
            "is_suspicious": suspicious,
            "confidence": 0.8,
            "summary": summary,
            "analyzed_at": _ts(analyzed_hours_ago),
            "severity": severity,
            "risk_score": 40.0,
            **extra,
        }
    )


def _event(kind: str, severity: str) -> SecurityEvent:
    return SecurityEvent(
        event_type=SecurityEventType(kind),
        severity=Severity(severity),
        confidence=0.9,
        detail="d",
        subject_label="person",
        track_id=1,
        start_offset=0.0,
        end_offset=1.0,
    )


async def test_camera_counts_group_by_name_and_use_the_latest_verdict(
    db: ClipDatabase,
) -> None:
    await _clip(db, "a1", "Front Door", 1)
    await _clip(db, "a2", "front door", 2)
    await _clip(db, "a3", "Yard", 3)
    await _clip(db, "old", "Yard", 24 * 40)
    # a1 was flagged, then re-analyzed and cleared: it must not count.
    await _analysis(db, "a1", "Front Door", suspicious=True, analyzed_hours_ago=5)
    await _analysis(db, "a1", "Front Door", suspicious=False, analyzed_hours_ago=1)
    await _analysis(db, "a2", "front door", suspicious=True)

    rows = await db.get_assistant_camera_counts(7)

    assert rows == [
        {"camera": "Front Door", "clips": 2, "analyzed": 2, "flagged": 1},
        {"camera": "Yard", "clips": 1, "analyzed": 0, "flagged": 0},
    ]


async def test_clips_lists_latest_verdict_events_and_flags(db: ClipDatabase) -> None:
    await _clip(db, "c1", "Porch", 1)
    await _clip(db, "c2", "Porch", 2)
    await _clip(db, "c3", "Porch", 3)
    await _clip(db, "unanalyzed", "Porch", 4)
    await _analysis(
        db,
        "c1",
        "Porch",
        suspicious=True,
        severity="suspicious",
        face_bypass_applied=True,
        routine_visit_applied=True,
    )
    await _analysis(db, "c2", "Porch", severity="noteworthy")
    await _analysis(db, "c3", "Porch")
    await db.save_security_events(
        "c1",
        "Porch",
        [_event("subject_present", "routine"), _event("loitering", "suspicious")],
    )
    await db.star_clip("c3", True)

    everything = await db.get_assistant_clips(7, 10, notable_only=False)
    notable = await db.get_assistant_clips(7, 10, notable_only=True)

    assert [c["id"] for c in everything] == ["c1", "c2", "c3"]
    assert [c["id"] for c in notable] == ["c1", "c2"]
    first = everything[0]
    assert first["is_suspicious"] is True
    assert first["severity"] == "suspicious"
    # Worst event first, each kind once.
    assert first["event_types"] == ["loitering", "subject_present"]
    assert first["cleared_as_household"] is True
    assert first["cleared_as_routine_door_use"] is True
    assert everything[2]["starred"] is True
    assert everything[1]["event_types"] == []
    assert everything[1]["duration"] == 6


async def test_clips_respects_the_limit_and_window(db: ClipDatabase) -> None:
    await _clip(db, "new", "Porch", 1)
    await _clip(db, "old", "Porch", 24 * 10)
    await _analysis(db, "new", "Porch")
    await _analysis(db, "old", "Porch")

    assert [c["id"] for c in await db.get_assistant_clips(7, 10, False)] == ["new"]
    assert [c["id"] for c in await db.get_assistant_clips(30, 1, False)] == ["new"]
    assert await db.get_assistant_clips(30, 0, False) == []


async def test_enrolled_names_are_listed_once_for_redaction(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Riley", [0.1, 0.2])
    await db.add_face_enrollment("Riley", [0.3, 0.4])
    await db.add_face_enrollment("Sam", [0.5, 0.6])
    assert await db.get_face_enrollment_names() == ["Riley", "Sam"]


async def test_chats_are_created_renamed_appended_and_deleted(
    db: ClipDatabase,
) -> None:
    first = await db.create_assistant_chat("c1", "New chat")
    assert first["message_count"] == 0
    assert first["messages"] == []
    await db.create_assistant_chat("c2", "Second")

    assert await db.append_assistant_messages(
        "c1", [{"role": "user", "content": "hi", "at": "t"}]
    )
    assert await db.append_assistant_messages(
        "c1", [{"role": "assistant", "content": "hello", "at": "t"}]
    )

    chat = await db.get_assistant_chat("c1")
    assert chat is not None
    assert [m["content"] for m in chat["messages"]] == ["hi", "hello"]
    assert chat["message_count"] == 2
    # Most recently used first: c1 was just written to.
    assert [c["id"] for c in await db.list_assistant_chats()] == ["c1", "c2"]
    assert "messages" not in (await db.list_assistant_chats())[0]

    assert await db.rename_assistant_chat("c1", "Porch")
    renamed = await db.get_assistant_chat("c1")
    assert renamed is not None
    assert renamed["title"] == "Porch"

    assert await db.delete_assistant_chat("c1")
    assert await db.get_assistant_chat("c1") is None
    assert [c["id"] for c in await db.list_assistant_chats()] == ["c2"]


async def test_a_chat_that_does_not_exist_reports_that(db: ClipDatabase) -> None:
    assert await db.get_assistant_chat("nope") is None
    assert not await db.rename_assistant_chat("nope", "x")
    assert not await db.delete_assistant_chat("nope")
    assert not await db.append_assistant_messages("nope", [{"role": "user"}])


async def test_a_damaged_messages_column_reads_as_an_empty_chat(
    db: ClipDatabase,
) -> None:
    await db.create_assistant_chat("c1", "Broken")
    assert db._pool is not None
    await db._pool.execute(
        "UPDATE assistant_chats SET messages='not json' WHERE id='c1'"
    )
    chat = await db.get_assistant_chat("c1")
    assert chat is not None
    assert chat["messages"] == []
    await db._pool.execute(
        "UPDATE assistant_chats SET messages='{\"a\": 1}' WHERE id='c1'"
    )
    chat = await db.get_assistant_chat("c1")
    assert chat is not None
    assert chat["messages"] == []


async def test_nothing_is_stored_before_the_database_is_open() -> None:
    closed = ClipDatabase("postgresql://unused")
    assert await closed.get_assistant_camera_counts(7) == []
    assert await closed.get_assistant_clips(7, 5, False) == []
    assert await closed.get_face_enrollment_names() == []
    assert await closed.list_assistant_chats() == []
    assert await closed.get_assistant_chat("x") is None
    assert not await closed.rename_assistant_chat("x", "y")
    assert not await closed.delete_assistant_chat("x")
    assert not await closed.append_assistant_messages("x", [])
    await closed.add_assistant_usage("openai", "m", 1, 1)
    created = await closed.create_assistant_chat("x", "t")
    assert created["id"] == "x"


async def test_assistant_usage_joins_the_token_totals(db: ClipDatabase) -> None:
    await _clip(db, "c1", "Porch", 1)
    await _analysis(db, "c1", "Porch", tokens_prompt=100, tokens_completion=50)
    await db.add_assistant_usage("anthropic", "claude-x", 400, 100)
    await db.add_assistant_usage("anthropic", "claude-x", 600, 200)
    await db.add_assistant_usage("openai", "gpt-y", -5, 10)

    usage = await db.get_token_usage_stats()

    assert usage["assistant"] == {
        "questions": 3,
        "tokens_prompt": 1000,
        "tokens_completion": 310,
        "tokens_total": 1310,
    }
    # Clips analyzed leaves the questions out; the token totals do not.
    assert usage["total_analyses"] == 1
    assert usage["total_tokens"] == 150 + 1310
    rows = {(r["model"], r["assistant"]): r for r in usage["by_model"]}
    assert rows[("claude-x", True)]["analyses"] == 2
    assert rows[("claude-x", True)]["provider"] == "anthropic"
    assert rows[("m", False)]["assistant"] is False


async def test_clear_stats_resets_the_assistant_spend_too(db: ClipDatabase) -> None:
    await db.add_assistant_usage("anthropic", "claude-x", 400, 100)
    await db.clear_ai_usage_stats()
    await db.add_assistant_usage("anthropic", "claude-x", 10, 5)

    usage = await db.get_token_usage_stats()
    assert usage["assistant"]["questions"] == 1
    assert usage["assistant"]["tokens_total"] == 15
    daily = await db.get_daily_usage_stats(days=2)
    assert [(d["model"], d["tokens_prompt"]) for d in daily if d["assistant"]] == [
        ("claude-x", 10)
    ]


async def test_daily_usage_includes_assistant_rows(db: ClipDatabase) -> None:
    await _clip(db, "c1", "Porch", 1)
    await _analysis(db, "c1", "Porch", tokens_prompt=100, tokens_completion=50)
    await db.add_assistant_usage("anthropic", "claude-x", 400, 100)

    daily = await db.get_daily_usage_stats(days=3)

    flags = {(d["model"], d["assistant"], d["escalated"]) for d in daily}
    assert flags == {("m", False, False), ("claude-x", True, False)}


async def test_usage_is_empty_without_a_database() -> None:
    closed = ClipDatabase("postgresql://unused")
    usage = await closed.get_token_usage_stats()
    assert usage["assistant"]["questions"] == 0
    assert json.dumps(usage)


# ---------------------------------------------------------------------------
# What the detector and the sound model recorded
# ---------------------------------------------------------------------------


async def _detect(db: ClipDatabase, clip_id: str, *labels: str) -> None:
    from blink_downloader.vision import DetectedObject

    await db.save_detected_objects(
        clip_id,
        [DetectedObject(label, 0.9, (0.0, 0.0, 1.0, 1.0), None, 0) for label in labels],
    )


async def test_clips_list_the_distinct_objects_seen_and_sounds_heard(
    db: ClipDatabase,
) -> None:
    await _clip(db, "c1", "Porch", 1)
    await _clip(db, "c2", "Porch", 2)
    await _clip(db, "c3", "Porch", 3)
    await _analysis(
        db,
        "c1",
        "Porch",
        audio_labels=json.dumps(
            [{"label": "dog_bark", "score": 0.8}, {"label": "", "score": 0.4}, 7]
        ),
    )
    await _analysis(db, "c2", "Porch", audio_labels="not json")
    await _analysis(db, "c3", "Porch", audio_labels='{"label": "x"}')
    await _detect(db, "c1", "person", "car", "person")

    rows = {c["id"]: c for c in await db.get_assistant_clips(7, 10, False)}

    assert rows["c1"]["objects"] == ["car", "person"]
    assert rows["c1"]["heard"] == ["dog_bark"]
    assert rows["c2"]["objects"] == []
    assert rows["c2"]["heard"] == []
    assert rows["c3"]["heard"] == []


# ---------------------------------------------------------------------------
# Searching the stored analyses
# ---------------------------------------------------------------------------


def _search(
    *,
    terms: tuple[SearchTerm, ...] = (),
    cameras: tuple[str, ...] = (),
    since_hours: float = 24 * 7,
    until_hours: float | None = None,
    suspicious: bool = False,
    starred: bool = False,
) -> ClipSearch:
    return ClipSearch(
        since=_ts(since_hours),
        until=_ts(until_hours) if until_hours is not None else None,
        cameras=cameras,
        terms=terms,
        suspicious_only=suspicious,
        starred_only=starred,
        description="test",
    )


async def _seed_search(db: ClipDatabase) -> None:
    await _clip(db, "door-pkg", "Front Door", 1)
    await _clip(db, "door-dog", "front door", 2)
    await _clip(db, "yard-pkg", "Yard", 3)
    await _clip(db, "yard-old", "Yard", 24 * 3)
    await _clip(db, "never-analyzed", "Yard", 1)
    await _analysis(db, "door-pkg", "Front Door", summary="A courier leaves a Package.")
    await _analysis(db, "door-dog", "front door", summary="Nothing much happens.")
    await _analysis(
        db, "yard-pkg", "Yard", summary="A package and a dog.", suspicious=True
    )
    await _analysis(db, "yard-old", "Yard", summary="A package, days ago.")
    await _detect(db, "door-dog", "dog")
    await db.star_clip("door-dog", True)


async def test_search_matches_the_description_without_regard_to_case(
    db: ClipDatabase,
) -> None:
    await _seed_search(db)

    found = await db.search_assistant_clips(_search(terms=(SearchTerm("package"),)), 10)

    assert [c["id"] for c in found] == ["door-pkg", "yard-pkg", "yard-old"]


async def test_search_matches_a_detector_label_the_description_never_names(
    db: ClipDatabase,
) -> None:
    await _seed_search(db)

    found = await db.search_assistant_clips(
        _search(terms=(SearchTerm("dog", ("dog",)),)), 10
    )

    assert [c["id"] for c in found] == ["door-dog", "yard-pkg"]
    assert found[0]["objects"] == ["dog"]


async def test_the_clip_matching_more_terms_comes_first(db: ClipDatabase) -> None:
    await _seed_search(db)

    found = await db.search_assistant_clips(
        _search(terms=(SearchTerm("package"), SearchTerm("dog", ("dog",)))), 10
    )

    assert found[0]["id"] == "yard-pkg"
    assert {c["id"] for c in found} == {"door-pkg", "door-dog", "yard-pkg", "yard-old"}


async def test_search_with_no_terms_lists_every_analyzed_clip_newest_first(
    db: ClipDatabase,
) -> None:
    await _seed_search(db)

    found = await db.search_assistant_clips(_search(), 10)

    assert [c["id"] for c in found] == ["door-pkg", "door-dog", "yard-pkg", "yard-old"]


async def test_search_narrows_by_camera_time_and_flags(db: ClipDatabase) -> None:
    await _seed_search(db)

    by_camera = await db.search_assistant_clips(_search(cameras=("Front Door",)), 10)
    by_window = await db.search_assistant_clips(
        _search(since_hours=4, until_hours=1.5), 10
    )
    flagged = await db.search_assistant_clips(_search(suspicious=True), 10)
    starred = await db.search_assistant_clips(_search(starred=True), 10)

    assert [c["id"] for c in by_camera] == ["door-pkg", "door-dog"]
    assert [c["id"] for c in by_window] == ["door-dog", "yard-pkg"]
    assert [c["id"] for c in flagged] == ["yard-pkg"]
    assert [c["id"] for c in starred] == ["door-dog"]


async def test_search_respects_the_limit_and_an_empty_result(
    db: ClipDatabase,
) -> None:
    await _seed_search(db)

    assert len(await db.search_assistant_clips(_search(), 2)) == 2
    assert await db.search_assistant_clips(_search(), 0) == []
    assert (
        await db.search_assistant_clips(_search(terms=(SearchTerm("zebra"),)), 10) == []
    )


async def test_search_reads_only_a_clips_latest_analysis(db: ClipDatabase) -> None:
    await _clip(db, "c1", "Porch", 1)
    await _analysis(db, "c1", "Porch", summary="A package.", analyzed_hours_ago=5)
    await _analysis(db, "c1", "Porch", summary="Nothing.", analyzed_hours_ago=1)

    assert (
        await db.search_assistant_clips(_search(terms=(SearchTerm("package"),)), 10)
        == []
    )


async def test_a_search_term_is_data_not_sql(db: ClipDatabase) -> None:
    await _seed_search(db)
    attack = SearchTerm("x'); DROP TABLE clips; --", ("dog'; --",))

    assert await db.search_assistant_clips(_search(terms=(attack,)), 10) == []
    assert len(await db.get_assistant_clips(7, 10, False)) == 4


async def test_search_without_a_database_finds_nothing() -> None:
    closed = ClipDatabase("postgresql://unused")

    assert await closed.search_assistant_clips(_search(), 5) == []
