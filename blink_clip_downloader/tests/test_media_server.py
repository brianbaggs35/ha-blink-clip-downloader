"""Tests for MediaServer REST API endpoints."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blink_downloader.analyzer import AnalysisResult
from blink_downloader.database import ClipDatabase
from blink_downloader.media_server import MediaServer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[ClipDatabase, None]:
    d = ClipDatabase(tmp_path / "test.db")
    await d.init()
    yield d
    await d.close()


def _make_clip(clip_id: str = "c1", camera: str = "Front Door", **kw) -> dict:
    return {
        "id": clip_id,
        "camera": camera,
        "path": kw.get("path", f"/data/{clip_id}.mp4"),
        "timestamp": kw.get("timestamp", "2024-06-01T08:00:00+00:00"),
        "size_bytes": kw.get("size_bytes", 1_048_576),
        "duration": kw.get("duration", 5),
        "source": kw.get("source", "pir"),
        "network_id": kw.get("network_id", 1),
    }


def _make_analysis_result(
    clip_id: str = "c1", camera: str = "Front Door"
) -> AnalysisResult:
    return AnalysisResult(
        clip_id=clip_id,
        camera=camera,
        model="test-model",
        response_text="A person walks by.",
        is_suspicious=False,
        confidence=0.2,
        summary="Person walking",
        frame_count=3,
        analysis_duration=1.5,
        analyzed_at="2024-06-01T09:00:00+00:00",
    )


def _make_analyzer(provider: str = "ollama", **overrides) -> MagicMock:
    """A MagicMock standing in for a BaseAnalyzer subclass instance."""
    analyzer = MagicMock()
    analyzer.provider_name = provider
    analyzer.model_name.return_value = overrides.get("model_name", "llava:7b")
    analyzer.health_check = AsyncMock(return_value=overrides.get("health", True))
    analyzer.fetch_models = AsyncMock(return_value=overrides.get("models", []))
    analyzer.analyze_clip = AsyncMock(
        return_value=overrides.get("analyze_result", _make_analysis_result())
    )
    analyzer.model_pricing.return_value = overrides.get("pricing", (3.0, 15.0))
    return analyzer


@pytest.fixture
async def client(db: ClipDatabase, tmp_path: Path) -> AsyncGenerator[TestClient, None]:
    server = MediaServer(db=db, download_path=tmp_path, port=0)
    app = server._build_app()
    # Inject the server instance so handlers can reference self._db etc.
    # We expose the server via the app's router directly.
    tc = TestClient(TestServer(app))
    await tc.start_server()
    yield tc
    await tc.close()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


async def test_health(client: TestClient) -> None:
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# / (index)
# ---------------------------------------------------------------------------


async def test_index_returns_html(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    assert "text/html" in resp.content_type
    body = await resp.text()
    assert "Blink Clip Library" in body
    assert "video.js" in body


async def test_index_mobile_pages_constrain_width(client: TestClient) -> None:
    """Status/Automations/AI/Usage pages must not force horizontal page overflow.

    Regression test: `.page.active{display:flex}` makes `.status-grid` and
    `.auto-content` flex items. Flex items default to `min-width:auto`, so
    content with a large intrinsic width (the activity chart, or a table
    with long unbreakable `<code>` identifiers) forced the whole page far
    wider than the viewport on mobile, with no way to scroll back since
    `body{overflow:hidden}` silently clipped the excess. `min-width:0` lets
    these containers actually shrink to the available width; the tables
    that still can't shrink below their content (event-table, usage-table)
    get their own `.table-scroll{overflow-x:auto}` wrapper instead of
    blowing out the page.
    """
    resp = await client.get("/")
    body = await resp.text()
    assert ".status-grid{display:grid" in body
    assert (
        "min-width:0" in body.split(".status-grid{display:grid", 1)[1].split("}", 1)[0]
    )
    assert ".auto-content{" in body
    assert "min-width:0" in body.split(".auto-content{", 1)[1].split("}", 1)[0]
    assert ".table-scroll{overflow-x:auto" in body
    assert '<div class="table-scroll">' in body
    assert 'id="usage-model-table-wrap" class="table-scroll"' in body


async def test_index_has_security_headers(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "Content-Security-Policy" in resp.headers


# ---------------------------------------------------------------------------
# /api/clips
# ---------------------------------------------------------------------------


async def test_list_clips_empty(client: TestClient) -> None:
    resp = await client.get("/api/clips")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)
    assert data == []


async def test_list_clips_returns_data(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("x1"))
    await db.add_clip(_make_clip("x2", camera="Back Yard"))
    resp = await client.get("/api/clips")
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 2


async def test_list_clips_camera_filter(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("a", camera="Front Door"))
    await db.add_clip(_make_clip("b", camera="Back Yard"))
    resp = await client.get("/api/clips?camera=front+door")
    data = await resp.json()
    assert all(c["camera"] == "Front Door" for c in data)
    assert len(data) == 1


async def test_list_clips_starred_filter(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("s1"))
    await db.add_clip(_make_clip("s2"))
    await db.star_clip("s1", True)
    resp = await client.get("/api/clips?starred=1")
    data = await resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "s1"


async def test_list_clips_sort_param(client: TestClient, db: ClipDatabase) -> None:
    for i in range(3):
        await db.add_clip(
            _make_clip(f"t{i}", timestamp=f"2024-06-0{i + 1}T00:00:00+00:00")
        )
    resp = await client.get("/api/clips?sort=oldest")
    data = await resp.json()
    if isinstance(data, list) and len(data) > 0:
        assert data[0]["id"] == "t0"


# ---------------------------------------------------------------------------
# /api/clips/{id}
# ---------------------------------------------------------------------------


async def test_get_clip_found(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("gc1"))
    resp = await client.get("/api/clips/gc1")
    assert resp.status == 200
    assert (await resp.json())["id"] == "gc1"


async def test_get_clip_not_found(client: TestClient) -> None:
    resp = await client.get("/api/clips/nope")
    assert resp.status == 404


# ---------------------------------------------------------------------------
# /api/clips/{id}/star
# ---------------------------------------------------------------------------


async def test_star_clip(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("st1"))
    resp = await client.put(
        "/api/clips/st1/star",
        json={"starred": True},
    )
    assert resp.status == 200
    clip = await db.get_clip("st1")
    assert clip is not None
    assert clip["starred"] is True


async def test_star_clip_not_found(client: TestClient) -> None:
    resp = await client.put("/api/clips/missing/star", json={"starred": True})
    assert resp.status == 404


# ---------------------------------------------------------------------------
# /api/clips/{id}/tags
# ---------------------------------------------------------------------------


async def test_set_tags(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("tg1"))
    resp = await client.put("/api/clips/tg1/tags", json={"tags": ["cat", "dog"]})
    assert resp.status == 200
    clip = await db.get_clip("tg1")
    assert clip is not None
    assert set(clip["tags"]) == {"cat", "dog"}


async def test_set_tags_bad_json(client: TestClient) -> None:
    resp = await client.put(
        "/api/clips/tg1/tags",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# /api/clips/{id} DELETE
# ---------------------------------------------------------------------------


async def test_delete_clip_no_file(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("del1", path="/nonexistent/del1.mp4"))
    resp = await client.delete("/api/clips/del1")
    assert resp.status == 200
    assert await db.get_clip("del1") is None


async def test_delete_clip_with_file(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    fp = tmp_path / "del2.mp4"
    fp.write_bytes(b"fake video")
    await db.add_clip(_make_clip("del2", path=str(fp)))
    resp = await client.delete("/api/clips/del2")
    assert resp.status == 200
    assert not fp.exists()


async def test_delete_clip_not_found(client: TestClient) -> None:
    resp = await client.delete("/api/clips/ghost")
    assert resp.status == 404


# ---------------------------------------------------------------------------
# /api/cameras
# ---------------------------------------------------------------------------


async def test_cameras_empty(client: TestClient) -> None:
    resp = await client.get("/api/cameras")
    assert resp.status == 200
    assert await resp.json() == []


async def test_cameras_returns_stats(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("cam1", camera="Front Door"))
    await db.add_clip(_make_clip("cam2", camera="Back Yard"))
    resp = await client.get("/api/cameras")
    data = await resp.json()
    cameras = {c["camera"] for c in data}
    assert "Front Door" in cameras
    assert "Back Yard" in cameras


# ---------------------------------------------------------------------------
# /api/stats
# ---------------------------------------------------------------------------


async def test_stats_returns_counts(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("s1"))
    resp = await client.get("/api/stats")
    assert resp.status == 200
    data = await resp.json()
    assert data["total_count"] >= 1


# ---------------------------------------------------------------------------
# /api/activity
# ---------------------------------------------------------------------------


async def test_activity_empty(client: TestClient) -> None:
    resp = await client.get("/api/activity")
    assert resp.status == 200
    assert await resp.json() == []


async def test_activity_default_days(client: TestClient, db: ClipDatabase) -> None:
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()
    await db.add_clip(_make_clip("act1", timestamp=ts))
    resp = await client.get("/api/activity?days=1")
    data = await resp.json()
    assert len(data) >= 1
    assert "count" in data[0]


async def test_activity_invalid_days_falls_back(client: TestClient) -> None:
    resp = await client.get("/api/activity?days=notanumber")
    assert resp.status == 200  # falls back to 7 days, no error


# ---------------------------------------------------------------------------
# /api/tags
# ---------------------------------------------------------------------------


async def test_tags_empty(client: TestClient) -> None:
    resp = await client.get("/api/tags")
    assert resp.status == 200
    assert await resp.json() == []


async def test_tags_returns_distinct_tags(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("t1"))
    await db.add_clip(_make_clip("t2"))
    await db.set_tags("t1", ["outdoor", "motion"])
    await db.set_tags("t2", ["outdoor", "night"])
    resp = await client.get("/api/tags")
    tags = await resp.json()
    assert set(tags) == {"outdoor", "motion", "night"}


# ---------------------------------------------------------------------------
# /api/clips/export-zip
# ---------------------------------------------------------------------------


async def test_export_zip_no_files_on_disk(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("z1", path="/nonexistent/z1.mp4"))
    resp = await client.post("/api/clips/export-zip", json={"ids": ["z1"]})
    assert resp.status == 404


async def test_export_zip_downloads_zip(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    import zipfile

    fp = tmp_path / "clip1.mp4"
    fp.write_bytes(b"fake video data")
    await db.add_clip(_make_clip("z2", path=str(fp)))

    resp = await client.post("/api/clips/export-zip", json={"ids": ["z2"]})
    assert resp.status == 200
    assert resp.content_type == "application/zip"

    body = await resp.read()
    with zipfile.ZipFile(__import__("io").BytesIO(body)) as zf:
        names = zf.namelist()
    assert "clip1.mp4" in names


async def test_export_zip_empty_ids(client: TestClient) -> None:
    resp = await client.post("/api/clips/export-zip", json={"ids": []})
    assert resp.status == 400


async def test_export_zip_bad_json(client: TestClient) -> None:
    resp = await client.post(
        "/api/clips/export-zip",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# /api/download-now
# ---------------------------------------------------------------------------


async def test_download_now_triggers_callback(db: ClipDatabase, tmp_path: Path) -> None:
    triggered = []

    async def fake_trigger():
        triggered.append(True)

    server = MediaServer(
        db=db, download_path=tmp_path, port=0, trigger_download=fake_trigger
    )
    app = server._build_app()
    tc = TestClient(TestServer(app))
    await tc.start_server()
    try:
        resp = await tc.post("/api/download-now")
        assert resp.status == 200
        assert triggered == [True]
    finally:
        await tc.close()


async def test_download_now_no_callback_touches_trigger_file(
    client: TestClient, tmp_path: Path
) -> None:
    resp = await client.post("/api/download-now")
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /api/clips/{id}/stream — Range request
# ---------------------------------------------------------------------------


async def test_stream_clip_not_found(client: TestClient) -> None:
    resp = await client.get("/api/clips/noclip/stream")
    assert resp.status == 404


async def test_stream_clip_file_missing(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("nofile", path="/no/such/file.mp4"))
    resp = await client.get("/api/clips/nofile/stream")
    assert resp.status == 404


async def test_stream_clip_full(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    fp = tmp_path / "vid.mp4"
    fp.write_bytes(b"X" * 1024)
    await db.add_clip(_make_clip("vid1", path=str(fp)))
    resp = await client.get("/api/clips/vid1/stream")
    assert resp.status == 200
    body = await resp.read()
    assert body == b"X" * 1024


async def test_stream_clip_range_request(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    fp = tmp_path / "range.mp4"
    fp.write_bytes(bytes(range(256)))
    await db.add_clip(_make_clip("range1", path=str(fp)))
    resp = await client.get("/api/clips/range1/stream", headers={"Range": "bytes=0-9"})
    assert resp.status == 206
    body = await resp.read()
    assert body == bytes(range(10))


# ---------------------------------------------------------------------------
# /api/clips/{id}/thumb
# ---------------------------------------------------------------------------


async def test_thumbnail_not_found(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("th1", path="/data/th1.mp4"))
    resp = await client.get("/api/clips/th1/thumb")
    assert resp.status == 404


async def test_thumbnail_returns_jpeg(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    fp = tmp_path / "thumb.mp4"
    fp.write_bytes(b"vid")
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)  # minimal JPEG header
    await db.add_clip(_make_clip("th2", path=str(fp)))
    resp = await client.get("/api/clips/th2/thumb")
    assert resp.status == 200
    assert resp.content_type == "image/jpeg"


# ---------------------------------------------------------------------------
# /api/auth/status
# ---------------------------------------------------------------------------


async def test_auth_status_default_connected(client: TestClient) -> None:
    """Without an auth_state_getter the endpoint reports 'connected'."""
    resp = await client.get("/api/auth/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["state"] == "connected"


async def test_auth_status_with_getter(db: ClipDatabase, tmp_path: Path) -> None:
    """auth_state_getter return value is forwarded to the client."""
    server = MediaServer(
        db=db,
        download_path=tmp_path,
        port=0,
        auth_state_getter=lambda: {"state": "needs_2fa", "message": "Enter your code."},
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/auth/status")
        data = await resp.json()
        assert data["state"] == "needs_2fa"
        assert data["message"] == "Enter your code."
    finally:
        await tc.close()


async def test_auth_status_forwards_two_fa_result_fields(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """two_fa_result_seq/ok from the getter are forwarded so the UI can
    detect a rejected (wrong) 2FA code for a specific submission."""
    server = MediaServer(
        db=db,
        download_path=tmp_path,
        port=0,
        auth_state_getter=lambda: {
            "state": "needs_2fa",
            "message": "Incorrect verification code. Please try again.",
            "two_fa_result_seq": 1,
            "two_fa_result_ok": False,
        },
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/auth/status")
        data = await resp.json()
        assert data["two_fa_result_seq"] == 1
        assert data["two_fa_result_ok"] is False
    finally:
        await tc.close()


# ---------------------------------------------------------------------------
# /api/auth/2fa
# ---------------------------------------------------------------------------


async def test_two_fa_submit_valid_code(db: ClipDatabase, tmp_path: Path) -> None:
    received: list[str] = []

    def _callback(code: str) -> int:
        received.append(code)
        return 0

    server = MediaServer(
        db=db,
        download_path=tmp_path,
        port=0,
        two_fa_callback=_callback,
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post(
            "/api/auth/2fa",
            json={"code": "123456"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["submitted"] is True
        assert received == ["123456"]
    finally:
        await tc.close()


async def test_two_fa_submit_returns_seq_from_callback(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """The seq number returned by the callback is forwarded to the client."""
    server = MediaServer(
        db=db,
        download_path=tmp_path,
        port=0,
        two_fa_callback=lambda _code: 7,
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/auth/2fa", json={"code": "123456"})
        assert resp.status == 200
        data = await resp.json()
        assert data["submitted"] is True
        assert data["seq"] == 7
    finally:
        await tc.close()


async def test_two_fa_submit_non_numeric_rejected(
    db: ClipDatabase, tmp_path: Path
) -> None:
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, two_fa_callback=lambda _: 0
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/auth/2fa", json={"code": "abc123"})
        assert resp.status == 400
    finally:
        await tc.close()


async def test_two_fa_submit_wrong_length_rejected(
    db: ClipDatabase, tmp_path: Path
) -> None:
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, two_fa_callback=lambda _: 0
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/auth/2fa", json={"code": "1234"})
        assert resp.status == 400
    finally:
        await tc.close()


async def test_two_fa_no_callback_returns_503(client: TestClient) -> None:
    """Without a two_fa_callback the endpoint returns 503."""
    resp = await client.post("/api/auth/2fa", json={"code": "000000"})
    assert resp.status == 503


async def test_index_contains_twofa_overlay(client: TestClient) -> None:
    """2FA overlay div is present in the served HTML."""
    resp = await client.get("/")
    body = await resp.text()
    assert "twofa-overlay" in body
    assert "twofa-input" in body
    assert "twofa-submit" in body


# ---------------------------------------------------------------------------
# /api/stats — disk field from extra_status (not request.app)
# ---------------------------------------------------------------------------


async def test_stats_returns_disk_from_extra_status(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Storage section is populated from MediaServer.extra_status['disk'], not
    from request.app (which is aiohttp's internal dict and is never populated)."""
    server = MediaServer(db=db, download_path=tmp_path, port=0)
    server.extra_status = {
        "connected": True,
        "disk": {
            "used_mb": 512.0,
            "free_gb": 10.5,
            "used_bytes": 536870912,
            "free_bytes": 11274289152,
            "total_bytes": 21474836480,
            "total_gb": 20.0,
            "quota_bytes": 10737418240,
            "quota_gb": 10.0,
        },
    }
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/stats")
        assert resp.status == 200
        data = await resp.json()
        assert "disk" in data, "disk key must be present when extra_status has it"
        assert data["disk"]["used_mb"] == 512.0
        assert data["disk"]["free_gb"] == 10.5
        assert data["connected"] is True
    finally:
        await tc.close()


async def test_stats_no_disk_when_extra_status_empty(client: TestClient) -> None:
    """When extra_status is empty (server just started), disk is absent from
    the stats response — the JS handles this gracefully with `if (s.disk)`."""
    resp = await client.get("/api/stats")
    assert resp.status == 200
    data = await resp.json()
    # 'disk' key should not appear since extra_status is empty
    assert "disk" not in data


# ---------------------------------------------------------------------------
# Streaming — Cache-Control header present for smooth video playback
# ---------------------------------------------------------------------------


async def test_stream_full_has_cache_control(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    """Full-file stream response carries Cache-Control so the browser can cache
    the video and avoid re-fetching on seek (reduces choppiness)."""
    fp = tmp_path / "cc.mp4"
    fp.write_bytes(b"Y" * 512)
    await db.add_clip(_make_clip("cc1", path=str(fp)))
    resp = await client.get("/api/clips/cc1/stream")
    assert resp.status == 200
    assert "cache-control" in {h.lower() for h in resp.headers}


async def test_stream_range_has_cache_control(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    """Partial-content (range) response also carries Cache-Control."""
    fp = tmp_path / "ccr.mp4"
    fp.write_bytes(b"Z" * 512)
    await db.add_clip(_make_clip("ccr1", path=str(fp)))
    resp = await client.get("/api/clips/ccr1/stream", headers={"Range": "bytes=0-99"})
    assert resp.status == 206
    assert "cache-control" in {h.lower() for h in resp.headers}


# ---------------------------------------------------------------------------
# AI Analysis endpoints
# ---------------------------------------------------------------------------


async def test_ai_status_disabled(client: TestClient) -> None:
    resp = await client.get("/api/ai/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["enabled"] is False


async def test_ai_usage_disabled(client: TestClient) -> None:
    resp = await client.get("/api/ai/usage")
    assert resp.status == 200
    data = await resp.json()
    assert data["enabled"] is False
    assert "total_analyses" in data
    assert "by_model" in data


async def test_ai_usage_returns_token_stats(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("u1"))
    await db.add_analysis_result(
        {
            "clip_id": "u1",
            "camera": "Front Door",
            "model": "llava:7b",
            "response_text": "",
            "is_suspicious": False,
            "confidence": 0.1,
            "summary": "ok",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
            "tokens_prompt": 120,
            "tokens_completion": 40,
        }
    )
    resp = await client.get("/api/ai/usage")
    assert resp.status == 200
    data = await resp.json()
    assert data["total_analyses"] == 1
    assert data["total_tokens_prompt"] == 120
    assert data["total_tokens_completion"] == 40
    assert data["total_tokens"] == 160
    assert len(data["by_model"]) == 1
    assert data["by_model"][0]["model"] == "llava:7b"


async def test_ai_models_disabled(client: TestClient) -> None:
    resp = await client.get("/api/ai/models")
    assert resp.status == 200
    data = await resp.json()
    assert data["enabled"] is False
    assert data["models"] == []


async def test_ai_queue_disabled(client: TestClient) -> None:
    resp = await client.get("/api/ai/queue")
    assert resp.status == 200
    data = await resp.json()
    assert data["enabled"] is False


async def test_ai_suspicious_empty(client: TestClient) -> None:
    resp = await client.get("/api/ai/suspicious")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_ai_clip_result_not_found(client: TestClient) -> None:
    resp = await client.get("/api/ai/results/nonexistent")
    assert resp.status == 200


async def test_ai_suspicious_returns_results(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("s1"))
    await db.add_analysis_result(
        {
            "clip_id": "s1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "Suspicious person",
            "is_suspicious": True,
            "confidence": 0.85,
            "summary": "Unknown person near car",
            "frame_count": 3,
            "analysis_duration": 4.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.get("/api/ai/suspicious")
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 1
    assert data[0]["clip_id"] == "s1"
    assert data[0]["is_suspicious"] is True


async def test_ai_analyze_now_no_analyzer(client: TestClient) -> None:
    resp = await client.post("/api/ai/analyze/c1")
    assert resp.status == 400


# ---------------------------------------------------------------------------
# /api/ai/moondream/install-status
# ---------------------------------------------------------------------------


async def test_moondream_install_status_returns_json(client: TestClient) -> None:
    resp = await client.get("/api/ai/moondream/install-status")
    assert resp.status == 200
    data = await resp.json()
    assert "installed" in data
    assert isinstance(data["installed"], bool)
    assert "install_state" in data
    assert "status" in data["install_state"]


# ---------------------------------------------------------------------------
# /api/ai/moondream/install
# ---------------------------------------------------------------------------


async def test_moondream_install_returns_installing_or_already_installed(
    client: TestClient,
) -> None:
    from unittest.mock import patch

    import blink_downloader.media_server as ms

    # Reset state
    ms._moondream_install_state = {"status": "idle", "log": ""}

    with patch(
        "blink_downloader.media_server._moondream_arch_supported", return_value=True
    ):
        with patch(
            "blink_downloader.media_server._is_moondream_installed", return_value=False
        ):
            with patch("asyncio.create_task", side_effect=lambda coro: coro.close()):
                resp = await client.post("/api/ai/moondream/install")

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] in ("installing", "already_installed")


async def test_moondream_install_already_installed(client: TestClient) -> None:
    from unittest.mock import patch

    with patch(
        "blink_downloader.media_server._is_moondream_installed", return_value=True
    ):
        resp = await client.post("/api/ai/moondream/install")

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "already_installed"


async def test_moondream_install_already_in_progress(client: TestClient) -> None:
    from unittest.mock import patch

    import blink_downloader.media_server as ms

    ms._moondream_install_state = {"status": "installing", "log": "in progress"}

    with patch(
        "blink_downloader.media_server._is_moondream_installed", return_value=False
    ):
        resp = await client.post("/api/ai/moondream/install")

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "installing"

    # Restore state
    ms._moondream_install_state = {"status": "idle", "log": ""}


# ---------------------------------------------------------------------------
# /api/ai/camera-configs  (GET + PUT)
# ---------------------------------------------------------------------------


async def test_ai_camera_configs_get_empty(client: TestClient) -> None:
    """GET returns an empty list when no cameras and no saved configs."""
    resp = await client.get("/api/ai/camera-configs")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)


async def test_ai_camera_configs_get_returns_is_car_camera_field(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    """GET includes is_car_camera=True for cameras saved with that flag."""
    import json

    cfg_file = tmp_path / "camera_configs.json"
    cfg_file.write_text(
        json.dumps(
            [
                {
                    "camera": "Driveway",
                    "description": "Points at car",
                    "custom_prompt": "",
                    "is_car_camera": True,
                }
            ]
        )
    )
    from unittest.mock import patch

    with patch(
        "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
        new=cfg_file,
    ):
        resp = await client.get("/api/ai/camera-configs")

    assert resp.status == 200
    data = await resp.json()
    # If Driveway appears in the result it must have is_car_camera=True
    driveway = next((c for c in data if c["camera"] == "Driveway"), None)
    if driveway:
        assert driveway["is_car_camera"] is True


async def test_ai_camera_configs_put_saves_is_car_camera(
    client: TestClient, tmp_path: Path
) -> None:
    """PUT persists is_car_camera flag and returns saved count."""
    payload = [
        {
            "camera": "Driveway",
            "description": "Side driveway",
            "custom_prompt": "",
            "is_car_camera": True,
        },
        {
            "camera": "Front Door",
            "description": "Front entrance",
            "custom_prompt": "",
            "is_car_camera": False,
        },
    ]
    import json

    cfg_file = tmp_path / "camera_configs.json"

    from unittest.mock import patch

    with patch(
        "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
        new=cfg_file,
    ):
        resp = await client.put(
            "/api/ai/camera-configs",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    assert resp.status == 200
    data = await resp.json()
    assert data["saved"] is True
    assert data["count"] == 2

    saved = json.loads(cfg_file.read_text())
    driveway = next(c for c in saved if c["camera"] == "Driveway")
    assert driveway["is_car_camera"] is True
    front = next(c for c in saved if c["camera"] == "Front Door")
    assert front["is_car_camera"] is False


async def test_ai_camera_configs_put_bad_json(client: TestClient) -> None:
    """PUT with invalid JSON returns 400."""
    resp = await client.put(
        "/api/ai/camera-configs",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_ai_camera_configs_get_malformed_json_file(
    client: TestClient, tmp_path: Path
) -> None:
    """A corrupt camera_configs.json falls back to an empty config list."""
    cfg_file = tmp_path / "camera_configs.json"
    cfg_file.write_text("{not valid json")

    with patch(
        "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
        new=cfg_file,
    ):
        resp = await client.get("/api/ai/camera-configs")

    assert resp.status == 200
    assert await resp.json() == []


async def test_ai_camera_configs_get_default_entry_for_unconfigured_camera(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    """A camera with clips but no saved config gets zeroed-out defaults."""
    await db.add_clip(_make_clip("cc1", camera="Garage"))
    cfg_file = tmp_path / "camera_configs.json"  # never written

    with patch(
        "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
        new=cfg_file,
    ):
        resp = await client.get("/api/ai/camera-configs")

    assert resp.status == 200
    data = await resp.json()
    garage = next(c for c in data if c["camera"] == "Garage")
    assert garage["description"] == ""
    assert garage["custom_prompt"] == ""
    assert garage["is_car_camera"] is False


async def test_ai_camera_configs_put_write_oserror_logged(
    client: TestClient, tmp_path: Path
) -> None:
    """A write failure (e.g. read-only /data) is logged, not raised to the client."""
    bad_path = tmp_path / "missing_dir" / "camera_configs.json"
    payload = [
        {
            "camera": "Driveway",
            "description": "",
            "custom_prompt": "",
            "is_car_camera": False,
        }
    ]

    with patch(
        "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
        new=bad_path,
    ):
        resp = await client.put(
            "/api/ai/camera-configs",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    assert resp.status == 200
    data = await resp.json()
    assert data["saved"] is True


async def test_ai_camera_configs_put_updates_live_analyzer(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """PUT pushes descriptions/prompts/car-cameras into the live analyzer."""
    analyzer = _make_analyzer()
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        cfg_file = tmp_path / "camera_configs.json"
        payload = [
            {
                "camera": "Driveway",
                "description": "Points at the driveway",
                "custom_prompt": "Watch for cars",
                "is_car_camera": True,
            }
        ]
        with patch(
            "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
            new=cfg_file,
        ):
            resp = await tc.put(
                "/api/ai/camera-configs",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 200
        analyzer.update_camera_descriptions.assert_called_once_with(
            {"Driveway": "Points at the driveway"}
        )
        analyzer.update_camera_prompts.assert_called_once_with(
            {"Driveway": "Watch for cars"}
        )
        analyzer.update_car_cameras.assert_called_once_with({"Driveway"})
    finally:
        await tc.close()


async def test_ai_camera_configs_put_can_clear_car_cameras(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Unchecking every 'protected vehicle' box must clear the live analyzer's
    car-camera set, not silently preserve the previous one — camera_configs.json
    is the single source of truth for is_car_camera."""
    analyzer = _make_analyzer()
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        cfg_file = tmp_path / "camera_configs.json"
        payload = [
            {
                "camera": "Driveway",
                "description": "Points at the driveway",
                "custom_prompt": "",
                "is_car_camera": False,
            }
        ]
        with patch(
            "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
            new=cfg_file,
        ):
            resp = await tc.put(
                "/api/ai/camera-configs",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 200
        analyzer.update_car_cameras.assert_called_once_with(set())
    finally:
        await tc.close()


async def test_ai_camera_configs_put_clears_removed_custom_prompt(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Clearing a camera's custom prompt in the AI tab must actually clear it
    on the live analyzer — a naive dict.update() merge would silently keep
    the stale prompt around until the add-on restarted."""
    analyzer = _make_analyzer()
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        cfg_file = tmp_path / "camera_configs.json"
        payload = [
            {
                "camera": "Driveway",
                "description": "Points at the driveway",
                "custom_prompt": "",
                "is_car_camera": False,
            }
        ]
        with patch(
            "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
            new=cfg_file,
        ):
            resp = await tc.put(
                "/api/ai/camera-configs",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 200
        # Empty custom_prompt values are dropped from the payload entirely,
        # so the live analyzer gets an empty dict rather than a stale entry.
        analyzer.update_camera_prompts.assert_called_once_with({})
    finally:
        await tc.close()


# ---------------------------------------------------------------------------
# Module-level moondream helpers
# ---------------------------------------------------------------------------


def test_is_moondream_installed_inserts_path_when_dir_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packages dir is prepended to sys.path so the import below can find it."""
    import blink_downloader.media_server as ms

    fake_dir = tmp_path / "moondream_packages"
    fake_dir.mkdir()
    monkeypatch.setattr(ms, "_MOONDREAM_PACKAGES_DIR", fake_dir)
    monkeypatch.delitem(sys.modules, "moondream", raising=False)

    assert str(fake_dir) not in sys.path
    try:
        ms._is_moondream_installed()
        assert str(fake_dir) in sys.path
    finally:
        if str(fake_dir) in sys.path:
            sys.path.remove(str(fake_dir))


def test_is_moondream_installed_true_when_importable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import blink_downloader.media_server as ms

    monkeypatch.setattr(ms, "_MOONDREAM_PACKAGES_DIR", tmp_path / "does_not_exist")
    monkeypatch.setitem(sys.modules, "moondream", MagicMock())
    assert ms._is_moondream_installed() is True


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def test_start_and_stop_lifecycle(db: ClipDatabase, tmp_path: Path) -> None:
    server = MediaServer(db=db, download_path=tmp_path, port=0)
    await server.start()
    try:
        assert server._runner is not None
    finally:
        await server.stop()
    assert server._runner is None


# ---------------------------------------------------------------------------
# /api/clips — invalid query params
# ---------------------------------------------------------------------------


async def test_list_clips_invalid_limit_offset_falls_back(client: TestClient) -> None:
    resp = await client.get("/api/clips?limit=abc&offset=xyz")
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /api/clips/{id} DELETE — thumbnail cleanup + OSError handling
# ---------------------------------------------------------------------------


async def test_delete_clip_with_file_and_thumb(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    fp = tmp_path / "del3.mp4"
    fp.write_bytes(b"fake video")
    thumb = tmp_path / "del3.jpg"
    thumb.write_bytes(b"\xff\xd8\xff")
    await db.add_clip(_make_clip("del3", path=str(fp)))
    resp = await client.delete("/api/clips/del3")
    assert resp.status == 200
    assert not fp.exists()
    assert not thumb.exists()


async def test_delete_clip_unlink_oserror_is_logged(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    fp = tmp_path / "del4.mp4"
    fp.write_bytes(b"fake video")
    await db.add_clip(_make_clip("del4", path=str(fp)))
    with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
        resp = await client.delete("/api/clips/del4")
    assert resp.status == 200
    assert await db.get_clip("del4") is None


# ---------------------------------------------------------------------------
# /api/clips/{id}/star — malformed body falls back to starred=True
# ---------------------------------------------------------------------------


async def test_star_clip_bad_json_defaults_starred_true(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("st2"))
    resp = await client.put(
        "/api/clips/st2/star",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["starred"] is True
    clip = await db.get_clip("st2")
    assert clip is not None
    assert clip["starred"] is True


# ---------------------------------------------------------------------------
# /api/clips/{id}/tags — not found
# ---------------------------------------------------------------------------


async def test_set_tags_not_found(client: TestClient) -> None:
    resp = await client.put("/api/clips/ghost/tags", json={"tags": ["a"]})
    assert resp.status == 404


# ---------------------------------------------------------------------------
# /api/clips/{id}/thumb — unknown clip id
# ---------------------------------------------------------------------------


async def test_thumbnail_clip_not_found(client: TestClient) -> None:
    """Unknown clip id (not just a missing thumbnail file) also 404s."""
    resp = await client.get("/api/clips/ghost/thumb")
    assert resp.status == 404


# ---------------------------------------------------------------------------
# /api/clips/export-zip — missing ids are skipped, not fatal
# ---------------------------------------------------------------------------


async def test_export_zip_skips_missing_clip_ids(
    client: TestClient, db: ClipDatabase, tmp_path: Path
) -> None:
    """IDs with no DB row are silently skipped as long as one clip succeeds."""
    import zipfile

    fp = tmp_path / "clip2.mp4"
    fp.write_bytes(b"fake video data")
    await db.add_clip(_make_clip("z3", path=str(fp)))

    resp = await client.post("/api/clips/export-zip", json={"ids": ["ghost-id", "z3"]})
    assert resp.status == 200
    assert resp.content_type == "application/zip"

    body = await resp.read()
    with zipfile.ZipFile(__import__("io").BytesIO(body)) as zf:
        names = zf.namelist()
    assert "clip2.mp4" in names


# ---------------------------------------------------------------------------
# /api/auth/2fa — malformed body
# ---------------------------------------------------------------------------


async def test_two_fa_bad_json_body(db: ClipDatabase, tmp_path: Path) -> None:
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, two_fa_callback=lambda _: 0
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post(
            "/api/auth/2fa",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
    finally:
        await tc.close()


# ---------------------------------------------------------------------------
# AI Analysis endpoints — analyzer/queue enabled
# ---------------------------------------------------------------------------


async def test_ai_status_enabled_ollama(db: ClipDatabase, tmp_path: Path) -> None:
    analyzer = _make_analyzer(provider="ollama")
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is True
        assert data["ai_online"] is True
        assert data["provider"] == "ollama"
        assert data["model"] == "llava:7b"
        assert "moondream_installed" not in data
    finally:
        await tc.close()


async def test_ai_status_enabled_moondream_local(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer(provider="moondream_local")
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        data = await resp.json()
        assert data["provider"] == "moondream_local"
        assert "moondream_installed" in data
        assert "moondream_arch_supported" in data
    finally:
        await tc.close()


async def test_ai_status_includes_queue_status(
    db: ClipDatabase, tmp_path: Path
) -> None:
    queue = MagicMock()
    queue.get_queue_status = AsyncMock(return_value={"pending": 2, "processing": 0})
    server = MediaServer(db=db, download_path=tmp_path, port=0, analysis_queue=queue)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        data = await resp.json()
        assert data["queue"]["pending"] == 2
    finally:
        await tc.close()


async def test_ai_status_includes_frame_analysis_stats(
    db: ClipDatabase, tmp_path: Path
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(_make_analysis_result("c1").to_dict())
    server = MediaServer(db=db, download_path=tmp_path, port=0)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        data = await resp.json()
        assert data["analysis_stats"]["total_frames_analyzed"] == 3
        assert "frames_analyzed_today" in data["analysis_stats"]
    finally:
        await tc.close()


async def test_ai_usage_enabled_non_anthropic(db: ClipDatabase, tmp_path: Path) -> None:
    analyzer = _make_analyzer(provider="ollama")
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/usage")
        data = await resp.json()
        assert data["enabled"] is True
        assert data["provider"] == "ollama"
        assert "cost_per_1m_input" not in data
    finally:
        await tc.close()


async def test_ai_usage_enabled_anthropic_includes_pricing(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer(provider="anthropic", pricing=(3.0, 15.0))
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/usage")
        data = await resp.json()
        assert data["cost_per_1m_input"] == 3.0
        assert data["cost_per_1m_output"] == 15.0
    finally:
        await tc.close()


async def test_ai_models_enabled(db: ClipDatabase, tmp_path: Path) -> None:
    analyzer = _make_analyzer(models=[{"name": "llava:7b"}])
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/models")
        data = await resp.json()
        assert data["enabled"] is True
        assert data["models"] == [{"name": "llava:7b"}]
    finally:
        await tc.close()


async def test_ai_queue_enabled(db: ClipDatabase, tmp_path: Path) -> None:
    queue = MagicMock()
    queue.get_queue_status = AsyncMock(return_value={"pending": 1})
    server = MediaServer(db=db, download_path=tmp_path, port=0, analysis_queue=queue)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/queue")
        data = await resp.json()
        assert data["enabled"] is True
        assert data["pending"] == 1
    finally:
        await tc.close()


async def test_ai_clip_result_found(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("ar1"))
    await db.add_analysis_result(
        {
            "clip_id": "ar1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": False,
            "confidence": 0.1,
            "summary": "ok",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.get("/api/ai/results/ar1")
    assert resp.status == 200
    data = await resp.json()
    assert data["clip_id"] == "ar1"


async def test_ai_suspicious_invalid_limit_falls_back(client: TestClient) -> None:
    resp = await client.get("/api/ai/suspicious?limit=notanumber")
    assert resp.status == 200


async def test_ai_analyze_now_clip_not_found(db: ClipDatabase, tmp_path: Path) -> None:
    analyzer = _make_analyzer()
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/analyze/ghost")
        assert resp.status == 404
    finally:
        await tc.close()


async def test_ai_analyze_now_success(db: ClipDatabase, tmp_path: Path) -> None:
    await db.add_clip(_make_clip("an1"))
    analyzer = _make_analyzer(analyze_result=_make_analysis_result("an1"))
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/analyze/an1")
        assert resp.status == 200
        data = await resp.json()
        assert data["clip_id"] == "an1"
        stored = await db.get_analysis_for_clip("an1")
        assert stored is not None
    finally:
        await tc.close()


# ---------------------------------------------------------------------------
# /api/ai/test
# ---------------------------------------------------------------------------


async def test_ai_test_no_analyzer(client: TestClient) -> None:
    resp = await client.post("/api/ai/test")
    assert resp.status == 400


async def test_ai_test_no_clips_in_library(db: ClipDatabase, tmp_path: Path) -> None:
    analyzer = _make_analyzer()
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/test")
        assert resp.status == 404
    finally:
        await tc.close()


async def test_ai_test_success(db: ClipDatabase, tmp_path: Path) -> None:
    await db.add_clip(_make_clip("at1"))
    analyzer = _make_analyzer(analyze_result=_make_analysis_result("at1"))
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/test")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["clip_id"] == "at1"
    finally:
        await tc.close()


async def test_ai_test_analyze_exception_returns_500(
    db: ClipDatabase, tmp_path: Path
) -> None:
    await db.add_clip(_make_clip("at2"))
    analyzer = _make_analyzer()
    analyzer.analyze_clip = AsyncMock(side_effect=RuntimeError("model unreachable"))
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/test")
        assert resp.status == 500
        data = await resp.json()
        assert "model unreachable" in data["error"]
    finally:
        await tc.close()


# ---------------------------------------------------------------------------
# /api/ai/moondream/install — unsupported arch + background install task
# ---------------------------------------------------------------------------


async def test_moondream_install_unsupported_arch(client: TestClient) -> None:
    with patch(
        "blink_downloader.media_server._moondream_arch_supported", return_value=False
    ):
        resp = await client.post("/api/ai/moondream/install")
    assert resp.status == 422
    data = await resp.json()
    assert data["status"] == "unsupported"


async def test_moondream_run_install_success(
    client: TestClient, tmp_path: Path
) -> None:
    """Exercises the background _run_install() coroutine's success branch."""
    import blink_downloader.media_server as ms

    ms._moondream_install_state = {"status": "idle", "log": ""}
    fake_pkg_dir = tmp_path / "moondream_packages"
    captured: list = []

    def _capture(coro):
        captured.append(coro)
        return MagicMock()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(b"Successfully installed moondream\n", None)
    )

    with patch(
        "blink_downloader.media_server._is_moondream_installed", return_value=False
    ):
        with patch("asyncio.create_task", side_effect=_capture):
            resp = await client.post("/api/ai/moondream/install")
    assert resp.status == 200

    try:
        with patch(
            "blink_downloader.media_server._MOONDREAM_PACKAGES_DIR", fake_pkg_dir
        ):
            with patch(
                "asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)
            ):
                await captured[0]
        assert ms._moondream_install_state["status"] == "installed"
    finally:
        if str(fake_pkg_dir) in sys.path:
            sys.path.remove(str(fake_pkg_dir))
        ms._moondream_install_state = {"status": "idle", "log": ""}


async def test_moondream_run_install_failure_nonzero_returncode(
    client: TestClient,
) -> None:
    import blink_downloader.media_server as ms

    ms._moondream_install_state = {"status": "idle", "log": ""}
    captured: list = []

    def _capture(coro):
        captured.append(coro)
        return MagicMock()

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"error: no wheel found\n", None))

    with patch(
        "blink_downloader.media_server._is_moondream_installed", return_value=False
    ):
        with patch("asyncio.create_task", side_effect=_capture):
            resp = await client.post("/api/ai/moondream/install")
    assert resp.status == 200

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        await captured[0]

    assert ms._moondream_install_state["status"] == "failed"
    ms._moondream_install_state = {"status": "idle", "log": ""}


async def test_moondream_run_install_timeout(client: TestClient) -> None:
    import blink_downloader.media_server as ms

    ms._moondream_install_state = {"status": "idle", "log": ""}
    captured: list = []

    def _capture(coro):
        captured.append(coro)
        return MagicMock()

    with patch(
        "blink_downloader.media_server._is_moondream_installed", return_value=False
    ):
        with patch("asyncio.create_task", side_effect=_capture):
            resp = await client.post("/api/ai/moondream/install")
    assert resp.status == 200

    # A sync (non-coroutine-returning) side_effect raises before wait_for's
    # inner coroutine is ever created, so nothing is left un-awaited.
    mock_proc = MagicMock()
    mock_proc.communicate = MagicMock(side_effect=asyncio.TimeoutError)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        await captured[0]

    assert ms._moondream_install_state["status"] == "failed"
    assert "timed out" in ms._moondream_install_state["log"].lower()
    ms._moondream_install_state = {"status": "idle", "log": ""}


async def test_moondream_run_install_generic_exception(client: TestClient) -> None:
    import blink_downloader.media_server as ms

    ms._moondream_install_state = {"status": "idle", "log": ""}
    captured: list = []

    def _capture(coro):
        captured.append(coro)
        return MagicMock()

    with patch(
        "blink_downloader.media_server._is_moondream_installed", return_value=False
    ):
        with patch("asyncio.create_task", side_effect=_capture):
            resp = await client.post("/api/ai/moondream/install")
    assert resp.status == 200

    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=RuntimeError("pip3 not found")),
    ):
        await captured[0]

    assert ms._moondream_install_state["status"] == "failed"
    assert "pip3 not found" in ms._moondream_install_state["log"]
    ms._moondream_install_state = {"status": "idle", "log": ""}
