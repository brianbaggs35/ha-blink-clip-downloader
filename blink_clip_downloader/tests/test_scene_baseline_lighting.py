"""A camera's two learned backgrounds: daylight colour, and infrared night.

A Blink camera switches to monochrome infrared after dark, and the same
view by day and under infrared is two different pictures. With one running
average of both, a night clip read as "differs from its usual background"
against a mostly-daylight average, the deviation streak then snapped the
average to night, and the next morning snapped it back — the signal mostly
said what time it was. These tests pin the split: each lighting learns and
is compared on its own, and neither disturbs the other.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from blink_downloader import frame_motion
from blink_downloader.analyzer import ClipAnalyzer
from blink_downloader.database import ClipDatabase

DAY = [0.8] * 256
NIGHT = [0.2] * 256


def _jpeg(colour: tuple[int, int, int] | None) -> bytes:
    """A small frame: a colour gradient, or its greyscale (infrared) twin."""
    img = Image.new("RGB", (64, 48))
    for x in range(64):
        for y in range(48):
            img.putpixel((x, y), (x * 4, 120, 255 - y * 5) if colour else (x * 4,) * 3)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


async def _learn(
    db: ClipDatabase, thumb: list[float], night: bool, n: int = 20
) -> None:
    for _ in range(n):
        await db.record_scene_baseline("Porch", thumb, night=night)


async def test_day_and_night_each_learn_their_own_background(db: ClipDatabase) -> None:
    await _learn(db, DAY, night=False)
    await _learn(db, NIGHT, night=True)
    assert await db.get_scene_deviation("Porch", DAY) == pytest.approx(0.0)
    assert await db.get_scene_deviation("Porch", NIGHT, night=True) == pytest.approx(
        0.0
    )
    # A night frame against the daylight background is what used to happen
    # on every clip after dark.
    assert await db.get_scene_deviation("Porch", NIGHT) == pytest.approx(0.6)


async def test_nights_never_snap_the_daylight_background(db: ClipDatabase) -> None:
    """The streak that absorbs a persistent change must not mistake the
    nightly switch to infrared for one."""
    await _learn(db, DAY, night=False)
    await _learn(db, NIGHT, night=True, n=30)
    assert await db.get_scene_deviation("Porch", DAY) == pytest.approx(0.0)


async def test_a_cameras_first_clip_at_night_starts_only_the_night_background(
    db: ClipDatabase,
) -> None:
    await db.record_scene_baseline("Porch", NIGHT, night=True)
    assert await db.get_scene_deviation("Porch", DAY) is None
    # The daylight one then begins from its own first sample.
    await _learn(db, DAY, night=False, n=19)
    assert await db.get_scene_deviation("Porch", DAY) is None
    await db.record_scene_baseline("Porch", DAY)
    assert await db.get_scene_deviation("Porch", DAY) == pytest.approx(0.0)


async def test_each_background_waits_for_its_own_history(db: ClipDatabase) -> None:
    await _learn(db, DAY, night=False)
    await _learn(db, NIGHT, night=True, n=19)
    assert await db.get_scene_deviation("Porch", NIGHT, night=True) is None


async def test_a_rename_carries_both_backgrounds(db: ClipDatabase) -> None:
    await _learn(db, DAY, night=False)
    await _learn(db, NIGHT, night=True)
    await db.rename_camera("Porch", "Front Porch")
    assert await db.get_scene_deviation("Front Porch", DAY) == pytest.approx(0.0)
    assert await db.get_scene_deviation(
        "Front Porch", NIGHT, night=True
    ) == pytest.approx(0.0)


async def test_replacing_a_camera_forgets_both(db: ClipDatabase) -> None:
    await _learn(db, DAY, night=False)
    await _learn(db, NIGHT, night=True)
    await db.reset_camera_baselines("Porch")
    assert await db.get_scene_deviation("Porch", DAY) is None
    assert await db.get_scene_deviation("Porch", NIGHT, night=True) is None


def test_is_infrared_tells_monochrome_from_colour() -> None:
    assert frame_motion.is_infrared(_jpeg(None)) is True
    assert frame_motion.is_infrared(_jpeg((1, 1, 1))) is False
    assert frame_motion.is_infrared(b"not a frame") is None


async def test_the_analyzer_compares_and_learns_under_the_clips_own_lighting(
    db: ClipDatabase,
) -> None:
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434", model="llava", prompt="p"
    )
    analyzer.attach_database(db)
    night_frame = _jpeg(None)
    thumb, deviation, night = await analyzer._lookup_scene_baseline(
        "Porch", [night_frame]
    )
    assert thumb is not None and deviation is None and night is True
    await analyzer._maybe_update_scene_baseline("Porch", thumb, False, 0.1, night=night)
    assert db._pool is not None
    row = await db._pool.fetchrow(
        "SELECT thumbnail, sample_count, night_sample_count FROM camera_scene_baselines"
        " WHERE camera='Porch'"
    )
    assert (row["thumbnail"], row["sample_count"], row["night_sample_count"]) == (
        "",
        0,
        1,
    )

    day_frame = _jpeg((1, 1, 1))
    thumb, _deviation, night = await analyzer._lookup_scene_baseline(
        "Porch", [day_frame]
    )
    assert night is False
