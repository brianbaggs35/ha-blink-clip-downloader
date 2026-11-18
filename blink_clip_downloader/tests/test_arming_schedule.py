"""Scheduled arming: validation, storage and the loop that runs the schedules.

Time is injected (``clock`` and ``tick(now)``) and Blink is faked at the one
boundary the scheduler has to it, so the real rules run: the grace window,
once a day, retries, a schedule saved after its time, and what is kept across a
restart.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from blink_downloader import arming_schedule as arming
from blink_downloader.arming_schedule import (
    ArmingScheduler,
    ScheduleError,
    normalize_schedule,
    read_schedules,
    validate_schedules,
)

MONDAY = datetime(2026, 11, 16, 21, 0)  # 2026-11-16 is a Monday


def at(hour: int, minute: int = 0, second: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 11, day, hour, minute, second)


def raw(**extra: Any) -> dict[str, Any]:
    return {
        "name": "Night mode",
        "enabled": True,
        "action": "arm",
        "time": "22:30",
        "days": [0, 1, 2, 3, 4, 5, 6],
        "scope": "system",
        **extra,
    }


MODULES = [
    {"name": "Home", "network_id": 111},
    {"name": "Shed", "network_id": "222"},
]


class FakeBlink:
    """Records arm requests and answers each module as told."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, bool, str | None]] = []

    def snapshot(self) -> list[dict[str, Any]]:
        return MODULES

    async def arm(
        self, name: str, armed: bool, *, network_id: str | None = None
    ) -> bool | None:
        self.calls.append((name, armed, network_id))
        answer = self.answers.get(name, True)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Announcements:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def __call__(self, message: str, title: str, notification_id: str) -> bool:
        self.sent.append((message, title, notification_id))
        return True


def make(
    tmp_path: Path,
    schedules: list[dict[str, Any]] | None = None,
    blink: FakeBlink | None = None,
    clock: datetime = MONDAY,
    announce: Announcements | None | bool = True,
) -> tuple[ArmingScheduler, FakeBlink, Announcements | None]:
    blink = blink or FakeBlink()
    announcements = Announcements() if announce is True else None
    scheduler = ArmingScheduler(
        blink.snapshot,
        blink.arm,
        announce=announcements,
        schedules_file=tmp_path / "schedules.json",
        state_file=tmp_path / "state.json",
        clock=lambda: clock,
    )
    if schedules is not None:
        scheduler.replace(schedules)
    return scheduler, blink, announcements


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_a_valid_schedule_is_canonical_and_gets_an_id() -> None:
    schedule = normalize_schedule(raw(days=[6, 0, 0, 3], name="  Night \n mode  "))
    assert schedule["name"] == "Night mode"
    assert schedule["days"] == [0, 3, 6]
    assert len(schedule["id"]) == 12
    assert schedule["scope"] == "system"
    assert schedule["sync_module"] == "" and schedule["network_id"] == ""


def test_a_system_schedule_forgets_a_module_it_was_given() -> None:
    schedule = normalize_schedule(raw(sync_module="Home", network_id=5))
    assert schedule["sync_module"] == "" and schedule["network_id"] == ""


def test_a_module_schedule_keeps_the_name_and_the_network_id_as_text() -> None:
    schedule = normalize_schedule(
        raw(scope="sync_module", sync_module="Home", network_id=5)
    )
    assert schedule["sync_module"] == "Home"
    assert schedule["network_id"] == "5"


@pytest.mark.parametrize("network_id", [None, "", True, 1.5, ["1"]])
def test_a_network_id_that_is_not_a_string_or_number_means_none(
    network_id: Any,
) -> None:
    schedule = normalize_schedule(
        raw(scope="sync_module", sync_module="Home", network_id=network_id)
    )
    assert schedule["network_id"] == ""


def test_a_missing_scope_means_the_whole_system() -> None:
    body = raw()
    del body["scope"]
    assert normalize_schedule(body)["scope"] == "system"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"action": "toggle"}, '"action"'),
        ({"action": None}, '"action"'),
        ({"time": "24:00"}, '"time"'),
        ({"time": "9:30"}, '"time"'),
        ({"time": "22:60"}, '"time"'),
        ({"time": 2230}, '"time"'),
        ({"days": []}, '"days"'),
        ({"days": "all"}, '"days"'),
        ({"days": [7]}, '"days"'),
        ({"days": [-1]}, '"days"'),
        ({"days": [True]}, '"days"'),
        ({"days": ["0"]}, '"days"'),
        ({"enabled": "yes"}, '"enabled"'),
        ({"scope": "camera"}, '"scope"'),
        ({"scope": "sync_module"}, "Choose which sync module"),
        ({"scope": "sync_module", "sync_module": "  "}, "Choose which sync module"),
        ({"id": "NOT VALID"}, "id is not valid"),
        ({"id": 7}, "id is not valid"),
    ],
)
def test_a_wrong_field_is_refused_with_a_reason(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ScheduleError, match=message):
        normalize_schedule(raw(**change))


def test_a_schedule_must_be_an_object() -> None:
    with pytest.raises(ScheduleError, match="must be an object"):
        normalize_schedule(["arm"])


def test_a_stored_schedule_without_an_id_is_not_made_up() -> None:
    with pytest.raises(ScheduleError, match="no id"):
        normalize_schedule(raw(), strict=False)


def test_a_name_is_one_bounded_line_and_control_characters_go() -> None:
    schedule = normalize_schedule(raw(name="a\x00b\x1f" + "x" * 200))
    assert (
        "\x00" not in schedule["name"]
        and len(schedule["name"]) == arming.MAX_NAME_LENGTH
    )
    assert normalize_schedule(raw(name=5))["name"] == ""


def test_the_list_is_validated_whole_and_names_the_bad_entry() -> None:
    with pytest.raises(ScheduleError, match=r"^Schedule 2: \"time\""):
        validate_schedules([raw(), raw(time="nope")])


def test_the_list_must_be_a_list() -> None:
    with pytest.raises(ScheduleError, match="must be a list"):
        validate_schedules({"schedules": []})


def test_too_many_schedules_are_refused() -> None:
    with pytest.raises(ScheduleError, match="At most"):
        validate_schedules([raw() for _ in range(arming.MAX_SCHEDULES + 1)])


def test_a_repeated_id_is_refused() -> None:
    one = normalize_schedule(raw())
    with pytest.raises(ScheduleError, match="Schedule 2: its id is used twice"):
        validate_schedules([one, one])


def test_an_empty_list_is_valid() -> None:
    assert validate_schedules([]) == []


# --------------------------------------------------------------------------
# Reading the file back
# --------------------------------------------------------------------------


def test_no_file_means_no_schedules(tmp_path: Path) -> None:
    assert read_schedules(tmp_path / "nope.json") == []


def test_the_default_path_is_looked_up_when_called(tmp_path: Path) -> None:
    arming.write_schedules([normalize_schedule(raw(id="aaaaaaaaaaaa"))])
    assert [s["id"] for s in read_schedules()] == ["aaaaaaaaaaaa"]
    assert arming.SCHEDULES_FILE.exists()


def test_an_unreadable_file_reads_as_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert read_schedules(path) == []
    assert "no schedules loaded" in caplog.text


@pytest.mark.parametrize("content", ["[]", '{"schedules": "x"}', "{}"])
def test_a_file_of_the_wrong_shape_reads_as_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(content)
    assert read_schedules(path) == []


def test_a_broken_entry_loses_only_itself(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    good = normalize_schedule(raw(id="aaaaaaaaaaaa"))
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "schedules": [
                    good,
                    {**good, "id": "bbbbbbbbbbbb", "time": "99:99"},
                    {**good, "id": None},
                    good,
                ]
            }
        )
    )
    assert [s["id"] for s in read_schedules(path)] == ["aaaaaaaaaaaa"]
    assert caplog.text.count("Ignoring a stored arming schedule") == 2


def test_only_the_most_that_are_kept_are_read(tmp_path: Path) -> None:
    items = [
        normalize_schedule(raw(id=f"{i:012d}")) for i in range(arming.MAX_SCHEDULES + 5)
    ]
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"schedules": items}))
    assert len(read_schedules(path)) == arming.MAX_SCHEDULES


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


def test_replace_saves_the_list_and_a_new_scheduler_reads_it(tmp_path: Path) -> None:
    scheduler, _, _ = make(
        tmp_path, [raw(), raw(name="Morning", action="disarm", time="07:00")]
    )
    saved = scheduler.schedules
    assert [s["name"] for s in saved] == ["Night mode", "Morning"]
    again, _, _ = make(tmp_path)
    assert again.schedules == saved


def test_the_returned_list_is_a_copy(tmp_path: Path) -> None:
    scheduler, _, _ = make(tmp_path, [raw()])
    scheduler.schedules[0]["enabled"] = False
    assert scheduler.schedules[0]["enabled"] is True


def test_a_bad_list_changes_nothing(tmp_path: Path) -> None:
    scheduler, _, _ = make(tmp_path, [raw()])
    before = (tmp_path / "schedules.json").read_text()
    with pytest.raises(ScheduleError):
        scheduler.replace([raw(time="nope")])
    assert (tmp_path / "schedules.json").read_text() == before
    assert len(scheduler.schedules) == 1


def test_a_failed_write_changes_nothing_and_raises(tmp_path: Path) -> None:
    scheduler, _, _ = make(tmp_path, [raw()])
    scheduler._schedules_file = tmp_path / "missing-dir" / "s.json"
    with pytest.raises(OSError):
        scheduler.replace([])
    assert len(scheduler.schedules) == 1


# --------------------------------------------------------------------------
# When a schedule runs
# --------------------------------------------------------------------------


async def test_a_schedule_runs_when_its_time_comes_and_not_before(
    tmp_path: Path,
) -> None:
    scheduler, blink, _ = make(tmp_path, [raw(time="22:30")], clock=at(21))
    await scheduler.tick(at(22, 29, 59))
    assert blink.calls == []
    await scheduler.tick(at(22, 30, 0))
    assert blink.calls == [("Home", True, "111"), ("Shed", True, "222")]
    outcome = next(iter(scheduler.outcomes().values()))
    assert outcome["outcome"] == "done"
    assert outcome["detail"] == "Armed 2 sync modules"
    assert outcome["at"] == "2026-11-16T22:30:00"


async def test_a_disarm_is_a_disarm(tmp_path: Path) -> None:
    scheduler, blink, _ = make(
        tmp_path, [raw(action="disarm", time="07:00")], clock=at(6)
    )
    await scheduler.tick(at(7, 0, 20, day=17))
    assert [c[1] for c in blink.calls] == [False, False]
    assert (
        next(iter(scheduler.outcomes().values()))["detail"] == "Disarmed 2 sync modules"
    )


async def test_it_runs_once_a_day_however_often_it_is_looked_at(tmp_path: Path) -> None:
    scheduler, blink, _ = make(tmp_path, [raw(time="22:30")], clock=at(21))
    for second in range(0, 240, 20):
        await scheduler.tick(at(22, 30 + second // 60, second % 60))
    assert len(blink.calls) == 2
    await scheduler.tick(at(22, 30, 0, day=17))
    assert len(blink.calls) == 4


async def test_only_the_chosen_weekdays_run(tmp_path: Path) -> None:
    scheduler, blink, _ = make(tmp_path, [raw(days=[0], time="22:30")], clock=at(21))
    await scheduler.tick(at(22, 30, day=17))  # Tuesday
    assert blink.calls == []
    await scheduler.tick(at(22, 30, day=16))  # Monday
    assert len(blink.calls) == 2


async def test_a_disabled_schedule_does_not_run(tmp_path: Path) -> None:
    scheduler, blink, _ = make(tmp_path, [raw(enabled=False)], clock=at(21))
    await scheduler.tick(at(22, 30))
    assert blink.calls == []


async def test_one_module_schedule_arms_only_that_module_by_id(tmp_path: Path) -> None:
    scheduler, blink, _ = make(
        tmp_path,
        [raw(scope="sync_module", sync_module="Shed", network_id="222")],
        clock=at(21),
    )
    await scheduler.tick(at(22, 30))
    assert blink.calls == [("Shed", True, "222")]
    assert next(iter(scheduler.outcomes().values()))["detail"] == "Armed 1 sync module"


async def test_a_module_without_a_network_id_is_asked_for_by_name_alone(
    tmp_path: Path,
) -> None:
    scheduler, blink, _ = make(
        tmp_path, [raw(scope="sync_module", sync_module="Shed")], clock=at(21)
    )
    await scheduler.tick(at(22, 30))
    assert blink.calls == [("Shed", True, None)]


async def test_a_schedule_saved_after_its_time_waits_for_tomorrow(
    tmp_path: Path,
) -> None:
    scheduler, blink, _ = make(tmp_path, [raw(time="22:00")], clock=at(22, 2))
    await scheduler.tick(at(22, 3))
    assert blink.calls == []
    assert scheduler.outcomes() == {}
    await scheduler.tick(at(22, 0, day=17))
    assert len(blink.calls) == 2


async def test_a_schedule_saved_before_its_time_runs_today(tmp_path: Path) -> None:
    scheduler, blink, _ = make(tmp_path, [raw(time="22:00")], clock=at(9))
    await scheduler.tick(at(22, 0))
    assert len(blink.calls) == 2


async def test_an_edit_starts_the_schedule_from_now(tmp_path: Path) -> None:
    scheduler, blink, _ = make(
        tmp_path, [raw(id="aaaaaaaaaaaa", time="22:00")], clock=at(9)
    )
    await scheduler.tick(at(22, 0))
    assert len(blink.calls) == 2
    # Moved to a time that has already passed today: not run again today.
    scheduler._clock = lambda: at(22, 10)
    scheduler.replace([raw(id="aaaaaaaaaaaa", time="22:05")])
    await scheduler.tick(at(22, 11))
    assert len(blink.calls) == 2
    assert scheduler.outcomes() == {}


async def test_an_untouched_schedule_keeps_its_outcome_through_a_save(
    tmp_path: Path,
) -> None:
    scheduler, blink, _ = make(tmp_path, [raw(id="aaaaaaaaaaaa")], clock=at(9))
    await scheduler.tick(at(22, 30))
    done = scheduler.outcomes()
    scheduler.replace([raw(id="aaaaaaaaaaaa"), raw(id="bbbbbbbbbbbb", time="23:00")])
    assert scheduler.outcomes() == done
    await scheduler.tick(at(22, 31))
    assert len(blink.calls) == 2


async def test_a_removed_schedule_loses_its_outcome(tmp_path: Path) -> None:
    scheduler, _, _ = make(tmp_path, [raw(id="aaaaaaaaaaaa")], clock=at(9))
    await scheduler.tick(at(22, 30))
    scheduler.replace([])
    assert scheduler.outcomes() == {}
    assert json.loads((tmp_path / "state.json").read_text()) == {}


# --------------------------------------------------------------------------
# When Blink does not cooperate
# --------------------------------------------------------------------------


async def test_a_refused_request_is_retried_until_it_goes_through(
    tmp_path: Path,
) -> None:
    blink = FakeBlink({"Home": False})
    scheduler, _, announcements = make(tmp_path, [raw()], blink=blink, clock=at(21))
    await scheduler.tick(at(22, 30))
    await scheduler.tick(at(22, 31))
    assert scheduler.outcomes() == {}
    blink.answers["Home"] = True
    await scheduler.tick(at(22, 32))
    assert next(iter(scheduler.outcomes().values()))["outcome"] == "done"
    assert announcements is not None and announcements.sent == []
    calls = len(blink.calls)
    await scheduler.tick(at(22, 33))
    assert len(blink.calls) == calls


async def test_a_request_that_keeps_failing_is_reported_once_the_grace_runs_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blink = FakeBlink({"Home": False, "Shed": None})
    scheduler, _, announcements = make(
        tmp_path, [raw(id="aaaaaaaaaaaa")], blink=blink, clock=at(21)
    )
    await scheduler.tick(at(22, 30))
    await scheduler.tick(at(22, 34, 59))
    assert announcements is not None and announcements.sent == []
    await scheduler.tick(at(22, 35))
    outcome = scheduler.outcomes()["aaaaaaaaaaaa"]
    assert outcome["outcome"] == "failed"
    assert 'Blink did not accept the request for "Home"' in outcome["detail"]
    assert '"Shed" was not found' in outcome["detail"]
    message, title, notification_id = announcements.sent[0]
    assert 'The schedule "Night mode" could not arm your system at 22:30' in message
    assert title == "Blink Downloader: Scheduled arming failed"
    assert notification_id == "blink_downloader_schedule_aaaaaaaaaaaa"
    assert "failed" in caplog.text
    await scheduler.tick(at(22, 36))
    assert len(announcements.sent) == 1


async def test_a_failure_without_a_name_is_labelled_by_what_it_was(
    tmp_path: Path,
) -> None:
    blink = FakeBlink({"Home": False, "Shed": False})
    scheduler, _, announcements = make(
        tmp_path, [raw(name="", action="disarm")], blink=blink, clock=at(21)
    )
    await scheduler.tick(at(22, 30))
    await scheduler.tick(at(22, 36))
    assert announcements is not None
    assert 'The schedule "Disarm at 22:30" could not disarm' in announcements.sent[0][0]


async def test_a_failure_is_recorded_even_with_nobody_to_tell(tmp_path: Path) -> None:
    blink = FakeBlink({"Home": False})
    scheduler, _, _ = make(tmp_path, [raw()], blink=blink, clock=at(21), announce=False)
    await scheduler.tick(at(22, 30))
    await scheduler.tick(at(22, 36))
    assert next(iter(scheduler.outcomes().values()))["outcome"] == "failed"


async def test_a_module_that_raises_is_a_failure_not_a_crash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blink = FakeBlink({"Home": RuntimeError("boom"), "Shed": True})
    scheduler, _, _ = make(tmp_path, [raw()], blink=blink, clock=at(21))
    await scheduler.tick(at(22, 30))
    assert [c[0] for c in blink.calls] == ["Home", "Shed"]
    assert "Scheduled arm of 'Home' failed" in caplog.text
    await scheduler.tick(at(22, 36))
    assert next(iter(scheduler.outcomes().values()))["outcome"] == "failed"


async def test_no_sync_modules_is_a_failure_that_says_so(tmp_path: Path) -> None:
    blink = FakeBlink()
    blink.snapshot = list  # type: ignore[method-assign]
    scheduler, _, _ = make(tmp_path, [raw()], blink=blink, clock=at(21))
    await scheduler.tick(at(22, 30))
    await scheduler.tick(at(22, 36))
    assert (
        next(iter(scheduler.outcomes().values()))["detail"]
        == "no sync module was available"
    )


async def test_a_failure_with_no_recorded_reason_still_says_something(
    tmp_path: Path,
) -> None:
    scheduler, _, _ = make(tmp_path, [raw(id="aaaaaaaaaaaa")], clock=at(21))
    scheduler._attempts["aaaaaaaaaaaa"] = (
        1  # attempted before a restart, no reason kept
    )
    await scheduler.tick(at(22, 36))
    assert scheduler.outcomes()["aaaaaaaaaaaa"]["detail"] == "Blink did not respond"


async def test_a_module_without_a_name_in_the_snapshot_is_skipped(
    tmp_path: Path,
) -> None:
    blink = FakeBlink()
    blink.snapshot = lambda: [{"name": "", "network_id": 1}, {"name": "Home"}]  # type: ignore[method-assign]
    scheduler, _, _ = make(tmp_path, [raw()], blink=blink, clock=at(21))
    await scheduler.tick(at(22, 30))
    assert blink.calls == [("Home", True, None)]


# --------------------------------------------------------------------------
# Missed, and restarts
# --------------------------------------------------------------------------


async def test_a_schedule_the_add_on_slept_through_is_missed_not_run_late(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler, blink, _ = make(
        tmp_path, [raw(id="aaaaaaaaaaaa", time="03:00")], clock=at(1)
    )
    await scheduler.tick(at(3, 5))
    assert blink.calls == []
    outcome = scheduler.outcomes()["aaaaaaaaaaaa"]
    assert outcome["outcome"] == "missed"
    assert "not running" in outcome["detail"]
    assert "was missed" in caplog.text
    await scheduler.tick(at(3, 6))
    assert caplog.text.count("was missed") == 1


async def test_a_restart_inside_the_grace_window_does_not_run_it_twice(
    tmp_path: Path,
) -> None:
    first, blink, _ = make(
        tmp_path, [raw(id="aaaaaaaaaaaa", time="22:30")], clock=at(9)
    )
    await first.tick(at(22, 30))
    assert len(blink.calls) == 2
    second, blink2, _ = make(tmp_path, clock=at(22, 32))
    await second.tick(at(22, 32))
    assert blink2.calls == []


async def test_a_restart_inside_the_window_before_it_ran_still_runs_it(
    tmp_path: Path,
) -> None:
    _x, _, _ = make(tmp_path, [raw(id="aaaaaaaaaaaa", time="22:30")], clock=at(9))
    second, blink2, _ = make(tmp_path, clock=at(22, 32))
    await second.tick(at(22, 32))
    assert len(blink2.calls) == 2


async def test_a_schedule_with_no_saved_state_is_judged_from_the_start(
    tmp_path: Path,
) -> None:
    # E.g. restored from a backup: the schedules came back, the state did not.
    (tmp_path / "schedules.json").write_text(
        json.dumps(
            {"schedules": [normalize_schedule(raw(id="aaaaaaaaaaaa", time="22:00"))]}
        )
    )
    scheduler, blink, _ = make(tmp_path, clock=at(22, 2))
    await scheduler.tick(at(22, 3))
    assert blink.calls == []


async def test_state_unreadable_or_odd_is_started_without(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for content in (
        "{broken",
        "[]",
        json.dumps({"a": "x", "b": {"outcome": "done", "n": 1}, 3: {}}),
    ):
        (tmp_path / "state.json").write_text(content)
        scheduler, _, _ = make(tmp_path)
        assert isinstance(scheduler.outcomes(), dict)
    assert "starting without it" in caplog.text


async def test_a_state_file_that_cannot_be_written_is_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler, blink, _ = make(tmp_path, [raw()], clock=at(9))
    scheduler._state_file = tmp_path / "missing-dir" / "state.json"
    await scheduler.tick(at(22, 30))
    assert len(blink.calls) == 2
    assert "Could not save the arming schedule state" in caplog.text


def test_the_default_state_file_is_looked_up_when_used(tmp_path: Path) -> None:
    scheduler = ArmingScheduler(list, FakeBlink().arm)
    assert scheduler._state_file == arming.STATE_FILE


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


_real_sleep = asyncio.sleep


async def test_the_loop_ticks_on_the_clock_until_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, blink, _ = make(tmp_path, [raw(time="22:30")], clock=at(9))
    scheduler._clock = lambda: at(22, 30)
    slept: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        slept.append(seconds)
        await _real_sleep(0)

    monkeypatch.setattr(arming.asyncio, "sleep", fast_sleep)
    task = asyncio.create_task(scheduler.run())
    for _ in range(5):
        await _real_sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert len(blink.calls) == 2
    assert slept and slept[0] == arming.TICK_SECONDS


async def test_one_failed_look_does_not_stop_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler, _, _ = make(tmp_path)
    looks = 0

    async def flaky(now: datetime | None = None) -> None:
        nonlocal looks
        looks += 1
        if looks == 1:
            raise RuntimeError("boom")

    async def fast_sleep(_seconds: float) -> None:
        await _real_sleep(0)

    monkeypatch.setattr(scheduler, "tick", flaky)
    monkeypatch.setattr(arming.asyncio, "sleep", fast_sleep)
    task = asyncio.create_task(scheduler.run())
    for _ in range(6):
        await _real_sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert looks >= 2
    assert "Arming schedule check failed" in caplog.text
