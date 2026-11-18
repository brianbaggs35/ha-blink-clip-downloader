"""The Sync Module tab's schedule list (media_server/sync_module.py)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blink_downloader.arming_schedule import ArmingScheduler
from blink_downloader.database import ClipDatabase
from blink_downloader.media_server import MediaServer


def _schedule(**extra: Any) -> dict[str, Any]:
    return {
        "name": "Night mode",
        "enabled": True,
        "action": "arm",
        "time": "22:30",
        "days": [0, 1, 2, 3, 4],
        "scope": "system",
        **extra,
    }


async def _arm(*_args: Any, **_kwargs: Any) -> bool:
    return True


@pytest.fixture
async def scheduler(tmp_path: Path) -> ArmingScheduler:
    return ArmingScheduler(
        list,
        _arm,
        schedules_file=tmp_path / "schedules.json",
        state_file=tmp_path / "state.json",
    )


@pytest.fixture
async def client(
    db: ClipDatabase, scheduler: ArmingScheduler
) -> AsyncGenerator[TestClient]:
    server = MediaServer(db=db, port=0, arming_scheduler=scheduler)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        yield tc
    finally:
        await tc.close()


async def test_the_list_starts_empty_and_says_which_clock_it_uses(
    client: TestClient,
) -> None:
    resp = await client.get("/api/sync-modules/schedules")
    assert resp.status == 200
    body = await resp.json()
    assert body["schedules"] == [] and body["outcomes"] == {}
    assert len(body["clock"]["now"]) == 5 and ":" in body["clock"]["now"]
    assert isinstance(body["clock"]["timezone"], str)


async def test_a_saved_list_comes_back_with_ids_and_is_kept(
    client: TestClient, scheduler: ArmingScheduler
) -> None:
    resp = await client.put(
        "/api/sync-modules/schedules",
        json={"schedules": [_schedule(days=[4, 0])]},
    )
    assert resp.status == 200
    saved = (await resp.json())["schedules"]
    assert saved[0]["days"] == [0, 4]
    assert len(saved[0]["id"]) == 12
    assert scheduler.schedules == saved
    again = await (await client.get("/api/sync-modules/schedules")).json()
    assert again["schedules"] == saved


async def test_an_empty_list_clears_them(client: TestClient) -> None:
    await client.put("/api/sync-modules/schedules", json={"schedules": [_schedule()]})
    resp = await client.put("/api/sync-modules/schedules", json={"schedules": []})
    assert resp.status == 200
    assert (await resp.json())["schedules"] == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"schedules": [_schedule(time="25:00")]}, 'Schedule 1: "time"'),
        ({"schedules": [_schedule(days=[])]}, '"days"'),
        ({"schedules": "nope"}, "must be a list"),
        ({}, "must be a list"),
    ],
)
async def test_a_wrong_list_is_a_400_that_says_why_and_changes_nothing(
    client: TestClient, scheduler: ArmingScheduler, body: dict[str, Any], message: str
) -> None:
    await client.put("/api/sync-modules/schedules", json={"schedules": [_schedule()]})
    before = scheduler.schedules
    resp = await client.put("/api/sync-modules/schedules", json=body)
    assert resp.status == 400
    assert message in await resp.text()
    assert scheduler.schedules == before


async def test_a_body_that_is_not_an_object_is_a_400(client: TestClient) -> None:
    resp = await client.put("/api/sync-modules/schedules", json=[1])
    assert resp.status == 400


async def test_a_failed_write_is_a_500_not_a_saved_looking_answer(
    client: TestClient, scheduler: ArmingScheduler, tmp_path: Path
) -> None:
    scheduler._schedules_file = tmp_path / "missing-dir" / "s.json"
    resp = await client.put(
        "/api/sync-modules/schedules", json={"schedules": [_schedule()]}
    )
    assert resp.status == 500
    assert "Could not save" in await resp.text()
    assert scheduler.schedules == []


async def test_without_a_scheduler_the_list_is_empty_and_saving_is_unavailable(
    db: ClipDatabase,
) -> None:
    tc = TestClient(TestServer(MediaServer(db=db, port=0)._build_app()))
    await tc.start_server()
    try:
        body = await (await tc.get("/api/sync-modules/schedules")).json()
        assert body["schedules"] == [] and body["outcomes"] == {}
        put = await tc.put("/api/sync-modules/schedules", json={"schedules": []})
        assert put.status == 503
    finally:
        await tc.close()


async def test_the_schedules_are_not_reachable_with_the_access_token() -> None:
    from blink_downloader.media_server.access_control import TOKEN_ROUTES

    assert not [route for route in TOKEN_ROUTES if "schedules" in str(route)]
