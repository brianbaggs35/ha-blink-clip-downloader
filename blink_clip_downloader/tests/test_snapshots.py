"""snapshots: which camera picture the Security Feed may show."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from blink_downloader import snapshots
from blink_downloader.snapshots import (
    RECOVERY_ATTEMPTS,
    RECOVERY_INTERVAL_SECONDS,
    SnapshotKeeper,
    image_defect,
)
from tests.snapshot_helpers import cut_short, whole_jpeg

CAMERA = "Front Door"
LOGGER = "blink_downloader.snapshots"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def pass_time(self, seconds: float) -> None:
        self.now += seconds


class Blink:
    """Stands in for downloading the thumbnail again: answers in turn, counts asks."""

    def __init__(self, *answers: bytes | None) -> None:
        self.answers = list(answers)
        self.asked = 0

    def __call__(self) -> Awaitable[bytes | None]:
        self.asked += 1
        return asyncio.sleep(0, self.answers.pop(0) if self.answers else None)


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.001)


# ---------------------------------------------------------------------------
# image_defect
# ---------------------------------------------------------------------------


def test_a_whole_picture_has_no_defect() -> None:
    assert image_defect(whole_jpeg()) is None


@pytest.mark.parametrize("keep", [0.05, 0.6, 0.99])
def test_a_picture_that_ends_early_is_named_as_such(keep: float) -> None:
    """The case the Security Feed drew as a black tile, or black below its top."""
    defect = image_defect(cut_short(whole_jpeg(), keep))

    assert defect is not None
    assert "truncated" in defect.lower()


@pytest.mark.parametrize(
    "body",
    [b"", b"<html>429 Too Many Requests</html>", bytes(200)],
    ids=["empty", "an error page", "zeros"],
)
def test_what_is_not_a_picture_has_a_defect(body: bytes) -> None:
    assert image_defect(body) is not None


# ---------------------------------------------------------------------------
# SnapshotKeeper
# ---------------------------------------------------------------------------


async def test_a_whole_cached_picture_is_served_without_asking_blink() -> None:
    picture = whole_jpeg()
    blink = Blink()

    served = await SnapshotKeeper().latest(CAMERA, picture, blink)

    assert served is picture
    assert blink.asked == 0


async def test_each_cached_picture_is_decoded_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoded: list[bytes] = []
    real = snapshots.image_defect

    def spy(data: bytes) -> str | None:
        decoded.append(data)
        return real(data)

    monkeypatch.setattr(snapshots, "image_defect", spy)
    keeper = SnapshotKeeper()
    first, second = whole_jpeg(1), whole_jpeg(2)

    for cached in (first, first, second, second):
        assert await keeper.latest(CAMERA, cached, Blink()) is cached

    assert decoded == [first, second]


async def test_a_picture_that_ends_early_is_never_served(
    caplog: pytest.LogCaptureFixture,
) -> None:
    damaged = cut_short(whole_jpeg(1))
    fresh = whole_jpeg(2)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        served = await SnapshotKeeper().latest(CAMERA, damaged, Blink(fresh))

    assert served == fresh
    assert "'Front Door'" in caplog.text
    assert "not a whole picture" in caplog.text
    assert f"{len(damaged)} bytes" in caplog.text


async def test_the_last_whole_picture_is_kept_while_blink_cannot_help() -> None:
    keeper = SnapshotKeeper()
    earlier = whole_jpeg(1)
    assert await keeper.latest(CAMERA, earlier, Blink()) is earlier

    served = await keeper.latest(CAMERA, cut_short(whole_jpeg(2)), Blink(None))

    assert served is earlier


@pytest.mark.parametrize(
    "answer",
    [cut_short(whole_jpeg(2)), b"", None],
    ids=["another damaged one", "an empty body", "nothing"],
)
async def test_an_answer_from_blink_that_is_not_whole_is_not_served(
    answer: bytes | None,
) -> None:
    served = await SnapshotKeeper().latest(
        CAMERA, cut_short(whole_jpeg(1)), Blink(answer)
    )

    assert served is None


async def test_nothing_cached_yet_downloads_the_thumbnail_once() -> None:
    keeper = SnapshotKeeper()
    fresh = whole_jpeg()
    blink = Blink(fresh)

    assert await keeper.latest(CAMERA, None, blink) == fresh
    assert await keeper.latest(CAMERA, None, blink) == fresh

    assert blink.asked == 1


async def test_nothing_cached_and_nothing_downloaded_is_no_picture(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing cached is not a damaged picture, so it is not warned about as one."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        served = await SnapshotKeeper().latest(CAMERA, None, Blink(None))

    assert served is None
    assert "not a whole picture" not in caplog.text


async def test_blink_is_asked_again_only_once_the_interval_has_passed() -> None:
    clock = Clock()
    keeper = SnapshotKeeper(clock)
    damaged = cut_short(whole_jpeg())
    blink = Blink(None, None, None)

    await keeper.latest(CAMERA, damaged, blink)
    clock.pass_time(RECOVERY_INTERVAL_SECONDS - 1)
    await keeper.latest(CAMERA, damaged, blink)
    assert blink.asked == 1

    clock.pass_time(1)
    await keeper.latest(CAMERA, damaged, blink)
    assert blink.asked == 2


@pytest.mark.parametrize("held", [True, False], ids=["a picture kept", "none kept"])
async def test_blink_is_asked_only_a_few_times_then_the_wait_is_for_something_new(
    held: bool, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    keeper = SnapshotKeeper(clock)
    earlier = whole_jpeg(1)
    if held:
        await keeper.latest(CAMERA, earlier, Blink())
    damaged = cut_short(whole_jpeg(2))
    blink = Blink()

    served = None
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(RECOVERY_ATTEMPTS + 3):
            served = await keeper.latest(CAMERA, damaged, blink)
            clock.pass_time(RECOVERY_INTERVAL_SECONDS)

    assert blink.asked == RECOVERY_ATTEMPTS
    assert served == (earlier if held else None)
    giving_up = [r for r in caplog.records if "still has no whole" in r.message]
    assert len(giving_up) == 1
    assert ("keeping the last whole one" in giving_up[0].message) is held
    assert ("there is none to show" in giving_up[0].message) is not held


async def test_a_whole_answer_from_blink_ends_the_asking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    keeper = SnapshotKeeper(clock)
    damaged = cut_short(whole_jpeg(1))
    fresh = whole_jpeg(2)
    blink = Blink(fresh)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(3):
            assert await keeper.latest(CAMERA, damaged, blink) == fresh
            clock.pass_time(RECOVERY_INTERVAL_SECONDS)

    assert blink.asked == 1
    assert "got a whole snapshot" in caplog.text


async def test_a_new_cached_picture_replaces_a_recovered_one_and_restarts_the_asking() -> (
    None
):
    clock = Clock()
    keeper = SnapshotKeeper(clock)
    recovered = whole_jpeg(2)
    blink = Blink(recovered, whole_jpeg(4))
    assert await keeper.latest(CAMERA, cut_short(whole_jpeg(1)), blink) == recovered

    newer = whole_jpeg(3)
    assert await keeper.latest(CAMERA, newer, blink) is newer
    assert blink.asked == 1

    # The next damaged one is asked about straight away, not after the interval
    # the previous one set: it is a different picture.
    served = await keeper.latest(CAMERA, cut_short(whole_jpeg(5)), blink)
    assert blink.asked == 2
    assert served == whole_jpeg(4)


async def test_cameras_are_kept_apart_and_a_name_matches_in_any_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoded: list[bytes] = []
    real = snapshots.image_defect

    def spy(data: bytes) -> str | None:
        decoded.append(data)
        return real(data)

    monkeypatch.setattr(snapshots, "image_defect", spy)
    keeper = SnapshotKeeper()
    door, yard = whole_jpeg(1), whole_jpeg(2)

    assert await keeper.latest("Front Door", door, Blink()) is door
    assert await keeper.latest("front door", door, Blink()) is door
    assert await keeper.latest("Backyard", yard, Blink()) is yard

    assert decoded == [door, yard]


async def test_simultaneous_requests_for_one_camera_share_one_download() -> None:
    keeper = SnapshotKeeper()
    damaged = cut_short(whole_jpeg(1))
    fresh = whole_jpeg(2)
    release = asyncio.Event()
    asked = 0

    async def slow_blink() -> bytes | None:
        nonlocal asked
        asked += 1
        await release.wait()
        return fresh

    first = asyncio.create_task(keeper.latest(CAMERA, damaged, slow_blink))
    second = asyncio.create_task(keeper.latest(CAMERA, damaged, slow_blink))
    await until(lambda: asked > 0)
    release.set()

    assert await first == fresh
    assert await second == fresh
    assert asked == 1


async def test_a_request_cancelled_while_decoding_leaves_the_picture_unexamined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marking a picture as examined before its decode finished would make the
    next request trust a verdict that was never reached."""
    real_to_thread = asyncio.to_thread
    calls = 0

    async def cancelled_the_first_time(func: Callable[..., Any], /, *args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return await real_to_thread(func, *args)

    monkeypatch.setattr(snapshots.asyncio, "to_thread", cancelled_the_first_time)
    keeper = SnapshotKeeper()
    picture = whole_jpeg()
    blink = Blink()

    with pytest.raises(asyncio.CancelledError):
        await keeper.latest(CAMERA, picture, blink)

    assert await keeper.latest(CAMERA, picture, Blink()) is picture
    assert calls == 2
