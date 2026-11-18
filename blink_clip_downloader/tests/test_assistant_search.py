"""Reading a question for the clips it asks about (assistant_search.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from blink_downloader.assistant_search import (
    MAX_SEARCH_DAYS,
    MAX_TERMS,
    ClipSearch,
    SearchTerm,
    parse_question,
)

# 14:30 local time, four hours behind UTC, so a window computed in local time
# and one wrongly computed in UTC land on different instants.
LOCAL = timezone(timedelta(hours=-4))
NOW = datetime(2026, 11, 18, 14, 30, tzinfo=LOCAL)
CAMERAS = ["Front Door", "Driveway", "Back Yard Cam"]


def _search(question: str, days: int = 7) -> ClipSearch:
    result = parse_question(question, CAMERAS, default_days=days, now=NOW)
    assert result is not None
    return result


def _local(instant: str | None) -> datetime:
    assert instant is not None
    return datetime.fromisoformat(instant).astimezone(LOCAL)


def test_a_question_that_names_nothing_is_not_a_search() -> None:
    assert parse_question("How are you?", CAMERAS, default_days=7, now=NOW) is None
    assert parse_question("", CAMERAS, default_days=7, now=NOW) is None


def test_a_camera_is_found_by_its_full_name_in_any_case() -> None:
    search = _search("Anything on the FRONT DOOR camera?")
    assert search.cameras == ("Front Door",)
    # The name's own words are not also searched for in the descriptions.
    assert search.terms == ()
    assert "camera Front Door" in search.description


def test_the_longest_camera_name_wins_and_partial_names_do_not_match() -> None:
    names = ["Door", "Front Door"]
    search = parse_question("front door", names, default_days=7, now=NOW)
    assert search is not None
    assert search.cameras == ("Front Door",)
    assert parse_question("the doorbell", names, default_days=7, now=NOW) is not None
    result = parse_question("the doorbell", names, default_days=7, now=NOW)
    assert result is not None
    assert result.cameras == ()


def test_a_camera_name_with_regex_characters_is_matched_literally() -> None:
    search = parse_question(
        "what about cam (north)?", ["cam (north)", "Cam .*"], default_days=7, now=NOW
    )
    assert search is not None
    assert search.cameras == ("cam (north)",)


def test_blank_camera_names_are_ignored() -> None:
    assert parse_question("the", ["", "  "], default_days=7, now=NOW) is None


@pytest.mark.parametrize(
    ("phrase", "start", "end", "label"),
    [
        ("today", (2026, 11, 18, 0), None, "today"),
        ("yesterday", (2026, 11, 17, 0), (2026, 11, 18, 0), "yesterday"),
        (
            "last night",
            (2026, 11, 17, 18),
            (2026, 11, 18, 7),
            "overnight (18:00 yesterday to 07:00 today)",
        ),
        (
            "overnight",
            (2026, 11, 17, 18),
            (2026, 11, 18, 7),
            "overnight (18:00 yesterday to 07:00 today)",
        ),
        (
            "this morning",
            (2026, 11, 18, 5),
            (2026, 11, 18, 12),
            "this morning (05:00 to 12:00)",
        ),
    ],
)
def test_named_times_are_windows_in_local_time(
    phrase: str,
    start: tuple[int, ...],
    end: tuple[int, ...] | None,
    label: str,
) -> None:
    search = _search(f"anything {phrase}?")
    assert _local(search.since) == datetime(*start, tzinfo=LOCAL)
    if end is None:
        assert search.until is None
    else:
        assert _local(search.until) == datetime(*end, tzinfo=LOCAL)
    assert label in search.description


@pytest.mark.parametrize(
    ("phrase", "delta", "label"),
    [
        ("last 3 hours", timedelta(hours=3), "the last 3 hours"),
        ("past 2 days", timedelta(days=2), "the last 2 days"),
        ("last 2 weeks", timedelta(weeks=2), "the last 2 weeks"),
        ("last hour", timedelta(hours=1), "the last 1 hour"),
        ("past day", timedelta(days=1), "the last 1 day"),
        ("last week", timedelta(weeks=1), "the last 1 week"),
        ("this week", timedelta(days=7), "the past 7 days"),
        ("this month", timedelta(days=MAX_SEARCH_DAYS), "the past 30 days"),
        ("last 0 days", timedelta(days=1), "the last 1 day"),
    ],
)
def test_spans_count_back_from_now(phrase: str, delta: timedelta, label: str) -> None:
    search = _search(f"any activity in the {phrase}")
    assert _local(search.since) == NOW - delta
    assert search.until is None
    assert label in search.description


def test_a_span_never_reaches_past_the_search_limit() -> None:
    search = _search("last 90 days")
    assert _local(search.since) == NOW - timedelta(days=MAX_SEARCH_DAYS)


def test_without_a_time_phrase_the_window_is_the_assistants_days() -> None:
    search = _search("front door", days=3)
    assert _local(search.since) == NOW - timedelta(days=3)
    assert "the last 3 day(s)" in search.description


def test_the_time_phrase_is_not_searched_for_as_a_word() -> None:
    search = _search("a package yesterday")
    assert [term.text for term in search.terms] == ["package"]


def test_words_become_terms_with_plurals_stemmed_and_labels_added() -> None:
    search = _search("did the cars or a person drop packages?")
    by_text = {term.text: term for term in search.terms}
    assert by_text["car"] == SearchTerm("car", ("car", "truck"))
    assert by_text["person"] == SearchTerm("person", ("person",))
    assert by_text["package"] == SearchTerm("package", ())
    assert by_text["drop"] == SearchTerm("drop", ())


def test_a_plural_label_word_uses_its_own_labels() -> None:
    search = _search("people")
    assert search.terms == (SearchTerm("people", ("person",)),)


def test_filler_short_and_repeated_words_are_dropped() -> None:
    search = _search("Show me all the clips of a dog, dogs and ox")
    assert [term.text for term in search.terms] == ["dog"]


def test_double_s_words_keep_their_ss() -> None:
    assert _search("glass").terms == (SearchTerm("glass", ()),)


def test_stray_punctuation_around_a_word_is_trimmed() -> None:
    assert _search("-delivery- 'truck'").terms == (
        SearchTerm("delivery", ()),
        SearchTerm("truck", ("truck", "car")),
    )


def test_no_more_than_the_limit_of_terms_is_used() -> None:
    search = _search("alpha bravo charlie delta echo foxtrot golf")
    assert len(search.terms) == MAX_TERMS
    assert [term.text for term in search.terms] == [
        "alpha",
        "bravo",
        "charlie",
        "delta",
        "echo",
    ]


@pytest.mark.parametrize("word", ["suspicious", "flagged", "alerts", "threat"])
def test_asking_for_flagged_clips_is_a_filter_not_a_word(word: str) -> None:
    search = _search(f"show {word} clips")
    assert search.suspicious_only is True
    assert search.terms == ()
    assert "flagged suspicious only" in search.description


@pytest.mark.parametrize("word", ["starred", "favorites", "favourite"])
def test_asking_for_starred_clips_is_a_filter_not_a_word(word: str) -> None:
    search = _search(f"my {word} clips")
    assert search.starred_only is True
    assert search.terms == ()
    assert "starred only" in search.description


def test_a_time_alone_is_enough_to_search() -> None:
    search = _search("what happened today")
    assert search.terms == ()
    assert search.cameras == ()
    assert search.description == "any camera; today"


def test_the_whole_description_says_what_was_searched() -> None:
    search = _search("suspicious person at the Driveway last night")
    assert search.description == (
        "camera Driveway; overnight (18:00 yesterday to 07:00 today); "
        "flagged suspicious only; mentioning person"
    )
    assert search.cameras == ("Driveway",)
    assert search.since.endswith("+00:00")
