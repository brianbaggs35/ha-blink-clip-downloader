"""The load-failure bookkeeping shared by the optional vision stages.

``LoadGuard`` holds no model and loads nothing, so these tests drive it with a
fake clock rather than sleeping.
"""

from __future__ import annotations

import pytest

from blink_downloader.vision import runtime
from blink_downloader.vision.runtime import (
    LoadGuard,
    StageHealth,
    describe_load_failure,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def guard(clock: _Clock) -> LoadGuard:
    return LoadGuard("face recognition", clock)


# --- describe_load_failure ---------------------------------------------------


def test_a_failure_is_described_by_its_class_and_first_line() -> None:
    exc = OSError("Can't load tokenizer\n  File x.py, line 3\nTraceback ...")
    assert describe_load_failure(exc) == "OSError: Can't load tokenizer"


def test_a_failure_with_no_message_is_described_by_its_class() -> None:
    assert describe_load_failure(RuntimeError()) == "RuntimeError"
    assert describe_load_failure(RuntimeError("   ")) == "RuntimeError"


def test_a_token_an_upstream_message_echoed_is_removed() -> None:
    exc = OSError("401 for https://x/y with Bearer hf_abcdefghijklmnop1234")
    reason = describe_load_failure(exc)
    assert "hf_abcdefghijklmnop1234" not in reason
    assert "hf_***" in reason


def test_a_long_reason_is_cut_to_one_bounded_line() -> None:
    reason = describe_load_failure(RuntimeError("x" * 5000))
    assert len(reason) == runtime._MAX_REASON_LENGTH
    assert reason.endswith("…")


def test_a_reason_exactly_at_the_limit_is_not_cut() -> None:
    prefix = "RuntimeError: "
    message = "y" * (runtime._MAX_REASON_LENGTH - len(prefix))
    assert describe_load_failure(RuntimeError(message)) == prefix + message


# --- LoadGuard ---------------------------------------------------------------


def test_a_stage_nobody_asked_for_has_no_health(guard: LoadGuard) -> None:
    assert guard.health() is None
    assert guard.backing_off() is False


def test_a_loaded_stage_is_ready(guard: LoadGuard) -> None:
    guard.ready()
    assert guard.health() == StageHealth("face recognition", "ready")
    assert guard.backing_off() is False


def test_a_failed_load_is_not_retried_until_the_wait_is_over(
    guard: LoadGuard, clock: _Clock
) -> None:
    guard.failed("OSError: offline")
    assert guard.backing_off() is True
    clock.now += runtime._RETRY_FIRST_SECONDS - 1
    assert guard.backing_off() is True
    clock.now += 1
    assert guard.backing_off() is False


def test_each_further_failure_waits_twice_as_long_up_to_a_ceiling(
    guard: LoadGuard, clock: _Clock
) -> None:
    waits = []
    for _ in range(10):
        guard.failed("OSError: offline")
        health = guard.health()
        assert health is not None
        assert health.retry_in is not None
        waits.append(health.retry_in)
        clock.now += health.retry_in
    assert waits[:4] == [60.0, 120.0, 240.0, 480.0]
    assert waits[-1] == runtime._RETRY_MAX_SECONDS
    assert max(waits) == runtime._RETRY_MAX_SECONDS


def test_a_permanent_failure_is_never_retried_and_names_no_wait(
    guard: LoadGuard, clock: _Clock
) -> None:
    guard.failed("package is not installed", permanent=True)
    clock.now += 10 * runtime._RETRY_MAX_SECONDS
    assert guard.backing_off() is True
    health = guard.health()
    assert health == StageHealth(
        "face recognition",
        "failed",
        detail="package is not installed",
        permanent=True,
        retry_in=None,
    )


def test_retry_now_makes_a_transient_failure_due(
    guard: LoadGuard, clock: _Clock
) -> None:
    guard.failed("OSError: offline")
    guard.retry_now()
    assert guard.backing_off() is False
    # ...without forgetting that it failed, so a second failure still backs off
    # further than the first did.
    guard.failed("OSError: offline")
    health = guard.health()
    assert health is not None
    assert health.retry_in == 120.0


def test_retry_now_cannot_install_a_package(guard: LoadGuard) -> None:
    guard.failed("package is not installed", permanent=True)
    guard.retry_now()
    assert guard.backing_off() is True


def test_retry_now_with_nothing_failed_changes_nothing(guard: LoadGuard) -> None:
    guard.retry_now()
    assert guard.health() is None
    assert guard.backing_off() is False


def test_only_a_new_reason_is_news(guard: LoadGuard) -> None:
    assert guard.failed("OSError: offline") is True
    assert guard.failed("OSError: offline") is False
    assert guard.failed("RuntimeError: corrupt weights") is True
    assert guard.failed("RuntimeError: corrupt weights") is False


def test_a_failure_after_a_recovery_is_news_again(guard: LoadGuard) -> None:
    guard.failed("OSError: offline")
    guard.ready()
    assert guard.failed("OSError: offline") is True


def test_loading_after_failures_clears_them(guard: LoadGuard, clock: _Clock) -> None:
    guard.failed("OSError: offline", permanent=False)
    guard.ready()
    assert guard.health() == StageHealth("face recognition", "ready")
    assert guard.backing_off() is False
    # The next failure starts the schedule over rather than continuing it.
    guard.failed("OSError: offline")
    health = guard.health()
    assert health is not None
    assert health.retry_in == runtime._RETRY_FIRST_SECONDS


def test_the_wait_left_never_goes_negative(guard: LoadGuard, clock: _Clock) -> None:
    guard.failed("OSError: offline")
    clock.now += 10_000
    health = guard.health()
    assert health is not None
    assert health.retry_in == 0.0


def test_the_default_clock_is_monotonic() -> None:
    guard = LoadGuard("depth estimation")
    guard.failed("x")
    assert guard.backing_off() is True


# --- StageHealth.to_dict -----------------------------------------------------


def test_the_json_form_rounds_the_wait_to_whole_seconds() -> None:
    health = StageHealth("face recognition", "failed", "boom", False, 44.6)
    assert health.to_dict() == {
        "stage": "face recognition",
        "state": "failed",
        "detail": "boom",
        "permanent": False,
        "retry_in": 45,
    }


def test_the_json_form_of_a_ready_stage_has_no_wait() -> None:
    assert StageHealth("face recognition", "ready").to_dict() == {
        "stage": "face recognition",
        "state": "ready",
        "detail": "",
        "permanent": False,
        "retry_in": None,
    }
