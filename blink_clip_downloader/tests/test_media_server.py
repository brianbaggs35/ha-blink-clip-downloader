"""Tests for MediaServer REST API endpoints."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blink_downloader import media_server
from blink_downloader.analyzer import AnalysisResult
from blink_downloader.database import ClipDatabase
from blink_downloader.media_server import MediaServer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    analyzer.car_protection_active = overrides.get("car_protection_active", False)
    # A bare MagicMock auto-vivifies any attribute access (including this
    # one) as a non-None child mock, which would make _handle_ai_status
    # think escalation is always configured. Default to None (escalation
    # disabled) like a real BaseAnalyzer with no tier 2 attached; tests that
    # want to exercise the escalation-status path pass one explicitly.
    analyzer.escalation_analyzer = overrides.get("escalation_analyzer", None)
    return analyzer


@pytest.fixture
async def client(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[TestClient, None]:
    # Stand in for the Vue build's output (see vite.config.ts's outDir and
    # the Dockerfile's frontend-builder stage, neither of which run as part
    # of the Python test suite) so _handle_index/_handle_favicon and the
    # /assets static route have something real to serve, without requiring
    # `npm run build` to have been run first. Mirrors frontend/index.html's
    # actual __HAROOT__ placeholder convention exactly, since the ingress-path
    # substitution tests below depend on it.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><html><head><script>window.__HA_INGRESS_ROOT__ = "
        "'__HAROOT__'</script></head><body><div id=\"app\"></div></body></html>"
    )
    (static_dir / "favicon.svg").write_text("<svg></svg>")
    assets_dir = static_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "index.js").write_text("// built JS bundle stand-in")
    monkeypatch.setattr(media_server, "_STATIC_DIR", static_dir)

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
# / (index) — serves the Vue build's static/index.html (see _STATIC_DIR).
#
# Content-level regression tests against the page markup (mobile-layout CSS,
# stored-XSS escaping of camera/tag names in innerHTML templates, etc.) were
# removed as part of the Vue migration: that content now lives in
# frontend/src and is covered by frontend/src/**/*.spec.ts instead, and the
# stored-XSS bug class this used to guard against is now structurally
# impossible rather than something to keep re-verifying — Vue's default
# template interpolation always escapes, and frontend/eslint.config.js sets
# `vue/no-v-html: error` so nothing can opt back into raw HTML injection.
# What's left here is what's still actually Python-side behavior: serving
# the right file/headers and safely escaping the ingress-path header.
# ---------------------------------------------------------------------------


async def test_index_returns_html(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    assert "text/html" in resp.content_type
    body = await resp.text()
    assert '<div id="app">' in body


async def test_index_missing_build_returns_clear_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media_server, "_STATIC_DIR", tmp_path / "no-such-dir")
    resp = await client.get("/")
    assert resp.status == 500
    assert "npm run build" in await resp.text()


async def test_favicon_served(client: TestClient) -> None:
    resp = await client.get("/favicon.svg")
    assert resp.status == 200


async def test_favicon_missing_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media_server, "_STATIC_DIR", tmp_path / "no-such-dir")
    resp = await client.get("/favicon.svg")
    assert resp.status == 404


async def test_assets_are_statically_served(client: TestClient) -> None:
    resp = await client.get("/assets/index.js")
    assert resp.status == 200


async def test_index_has_security_headers(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "Content-Security-Policy" in resp.headers
    # Video.js is bundled into the Vue build's own JS now (see
    # frontend/src/components/library/ClipModal.vue), not loaded from a CDN,
    # so no third-party origin should be allow-listed anymore.
    assert "cdn.jsdelivr.net" not in resp.headers["Content-Security-Policy"]


async def test_index_ingress_path_header_is_used_when_present(
    client: TestClient,
) -> None:
    resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    body = await resp.text()
    assert 'window.__HA_INGRESS_ROOT__ = "/api/hassio_ingress/abc"' in body


async def test_index_ingress_path_header_escapes_script_breakout(
    client: TestClient,
) -> None:
    """A malicious X-Ingress-Path must not be able to break out of the
    surrounding JS string literal or close the <script> tag early."""
    malicious = "</script><script>alert(1)</script>"
    resp = await client.get("/", headers={"X-Ingress-Path": malicious})
    body = await resp.text()
    assert "<script>alert(1)</script>" not in body
    assert "<\\/script><script>alert(1)<\\/script>" in body


async def test_index_ingress_path_header_escapes_quote_breakout(
    client: TestClient,
) -> None:
    # Note: no trailing "/" in the payload - the handler's legitimate
    # .rstrip("/") normalization (for a real ingress path like "/foo/") would
    # otherwise strip it and mask what this test is actually checking.
    malicious = "'; alert(1); x"
    resp = await client.get("/", headers={"X-Ingress-Path": malicious})
    body = await resp.text()
    assert "window.__HA_INGRESS_ROOT__ = '';" not in body
    assert "alert(1); x" in body  # present, but safely inside a JSON string
    assert 'window.__HA_INGRESS_ROOT__ = "\'; alert(1); x"' in body


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


async def test_list_clips_notified_filter(db: ClipDatabase, tmp_path: Path) -> None:
    await db.add_clip(_make_clip("n1"))
    await db.add_clip(_make_clip("n2"))
    await db.add_analysis_result(
        {
            "clip_id": "n1",
            "camera": "Front Door",
            "model": "test",
            "is_suspicious": True,
            "confidence": 0.9,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    queue = MagicMock()
    queue.min_confidence = 0.5
    server = MediaServer(db=db, download_path=tmp_path, port=0, analysis_queue=queue)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/clips?notified=1")
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "n1"
        assert data[0]["notified"] is True

        resp_all = await tc.get("/api/clips")
        data_all = await resp_all.json()
        by_id = {c["id"]: c for c in data_all}
        assert by_id["n1"]["notified"] is True
        assert by_id["n2"]["notified"] is False
    finally:
        await tc.close()


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


async def test_activity_zero_or_negative_days_clamped_to_one(
    client: TestClient, db: ClipDatabase
) -> None:
    """A non-positive `days` must not shift get_activity_data()'s cutoff to
    today/the future and silently return nothing — it should be clamped up
    to 1 (today), matching how limit/offset are clamped elsewhere in this
    file, so a clip recorded today still shows up."""
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()
    await db.add_clip(_make_clip("act-clamped", timestamp=ts))

    resp_zero = await client.get("/api/activity?days=0")
    assert resp_zero.status == 200
    assert len(await resp_zero.json()) >= 1

    resp_negative = await client.get("/api/activity?days=-5")
    assert resp_negative.status == 200
    assert len(await resp_negative.json()) >= 1


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

    def fake_trigger():
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
# /api/clips/{id}/frames — multi-frame extraction for Biometrics enrollment
# ---------------------------------------------------------------------------


def _concat_jpegs(*frames: bytes) -> bytes:
    return b"".join(b"\xff\xd8" + f + b"\xff\xd9" for f in frames)


async def test_clip_frames_not_found(client: TestClient) -> None:
    resp = await client.get("/api/clips/missing/frames")
    assert resp.status == 404


async def test_clip_frames_returns_base64_frames(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("f1", path="/data/f1.mp4", duration=10))
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(_concat_jpegs(b"frame-a", b"frame-b"), b"")
    )
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/clips/f1/frames?count=2")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["frames"]) == 2
    assert data["frames"][0].startswith("data:image/jpeg;base64,")
    import base64 as _b64

    decoded = _b64.b64decode(data["frames"][0].split(",", 1)[1])
    assert decoded == b"\xff\xd8frame-a\xff\xd9"


async def test_clip_frames_clamps_count(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("f2", path="/data/f2.mp4", duration=10))
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await client.get("/api/clips/f2/frames?count=999")
    cmd = mock_exec.call_args.args
    assert "16" in cmd  # clamped to the max


async def test_clip_frames_bad_count_falls_back_to_default(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("f3", path="/data/f3.mp4", duration=10))
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        resp = await client.get("/api/clips/f3/frames?count=notanumber")
    assert resp.status == 200
    cmd = mock_exec.call_args.args
    assert "8" in cmd  # default


async def test_clip_frames_ffmpeg_not_available(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("f4", path="/data/f4.mp4", duration=10))
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("no ffmpeg")):
        resp = await client.get("/api/clips/f4/frames")
    assert resp.status == 200
    assert (await resp.json())["frames"] == []


async def test_clip_frames_ffmpeg_timeout(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("f5", path="/data/f5.mp4", duration=10))
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/clips/f5/frames")
    assert resp.status == 200
    assert (await resp.json())["frames"] == []
    mock_proc.kill.assert_called_once()


async def test_clip_frames_ffmpeg_nonzero_exit(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("f6", path="/data/f6.mp4", duration=10))
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"bad input"))
    mock_proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/clips/f6/frames")
    assert resp.status == 200
    assert (await resp.json())["frames"] == []


async def test_clip_frames_ignores_truncated_trailing_data(
    client: TestClient, db: ClipDatabase
) -> None:
    """A well-formed frame followed by a truncated/incomplete one (missing
    EOI) must not raise — the split just stops there."""
    await db.add_clip(_make_clip("f8", path="/data/f8.mp4", duration=10))
    truncated = _concat_jpegs(b"frame-a") + b"\xff\xd8no-eoi-here"
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(truncated, b""))
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/clips/f8/frames")
    assert resp.status == 200
    assert len((await resp.json())["frames"]) == 1


async def test_clip_frames_stops_when_no_further_soi_marker(
    client: TestClient, db: ClipDatabase
) -> None:
    """A well-formed frame followed by trailing bytes with no JPEG start
    marker at all must not raise — the split just stops there."""
    await db.add_clip(_make_clip("f9", path="/data/f9.mp4", duration=10))
    trailing_garbage = _concat_jpegs(b"frame-a") + b"not-a-jpeg-at-all"
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(trailing_garbage, b""))
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/clips/f9/frames")
    assert resp.status == 200
    assert len((await resp.json())["frames"]) == 1


async def test_clip_frames_zero_duration_uses_fallback_interval(
    client: TestClient, db: ClipDatabase
) -> None:
    """A clip with an unknown/zero duration (e.g. local-storage clips) must
    still produce a sane, non-zero ffmpeg sampling interval rather than
    dividing by zero."""
    await db.add_clip(_make_clip("f7", path="/data/f7.mp4", duration=0))
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.get("/api/clips/f7/frames")
    assert resp.status == 200


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


async def test_ai_usage_computes_cost_for_known_model(
    client: TestClient, db: ClipDatabase
) -> None:
    """by_model rows are priced from the real per-model pricing table.

    "llava:7b" isn't a priced model (Ollama), so its row gets cost=None and
    doesn't contribute to the total; "gpt-4o-mini" is priced at
    $0.15/$0.60 per 1M tokens regardless of which analyzer is currently active.
    """
    await db.add_clip(_make_clip("u1"))
    await db.add_analysis_result(
        {
            "clip_id": "u1",
            "camera": "Front Door",
            "model": "gpt-4o-mini",
            "response_text": "",
            "is_suspicious": False,
            "confidence": 0.1,
            "summary": "ok",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
            "tokens_prompt": 1_000_000,
            "tokens_completion": 1_000_000,
        }
    )
    resp = await client.get("/api/ai/usage")
    data = await resp.json()

    row = next(m for m in data["by_model"] if m["model"] == "gpt-4o-mini")
    assert row["cost"] == pytest.approx(0.15 + 0.60)
    assert data["total_estimated_cost"] == pytest.approx(0.15 + 0.60)


async def test_ai_usage_unpriced_model_cost_is_none(
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
            "tokens_prompt": 100,
            "tokens_completion": 40,
        }
    )
    resp = await client.get("/api/ai/usage")
    data = await resp.json()

    row = next(m for m in data["by_model"] if m["model"] == "llava:7b")
    assert row["cost"] is None
    assert data["total_estimated_cost"] is None


async def test_ai_usage_escalation_row_priced_separately(
    client: TestClient, db: ClipDatabase
) -> None:
    """An OpenAI escalation shows up as its own by_model row, priced at the
    escalation model's own rate rather than folded into tier 1's row."""
    await db.add_clip(_make_clip("u1"))
    await db.add_analysis_result(
        {
            "clip_id": "u1",
            "camera": "Front Door",
            "model": "gpt-4o-mini",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.9,
            "summary": "Escalated",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
            "tokens_prompt": 1_000_000,
            "tokens_completion": 1_000_000,
            "escalation_model": "gpt-4o",
            "escalation_tokens_prompt": 1_000_000,
            "escalation_tokens_completion": 1_000_000,
        }
    )
    resp = await client.get("/api/ai/usage")
    data = await resp.json()

    assert data["total_escalations"] == 1
    assert data["total_escalation_tokens"] == 2_000_000
    tier1 = next(m for m in data["by_model"] if m["model"] == "gpt-4o-mini")
    escalated = next(m for m in data["by_model"] if m["model"] == "gpt-4o")
    assert tier1["escalated"] is False
    assert escalated["escalated"] is True
    assert tier1["cost"] == pytest.approx(0.15 + 0.60)
    assert escalated["cost"] == pytest.approx(2.50 + 10.00)
    assert data["total_estimated_cost"] == pytest.approx(0.15 + 0.60 + 2.50 + 10.00)


async def test_ai_usage_escalation_dedupes_across_provider_values(
    client: TestClient, db: ClipDatabase
) -> None:
    """Regression test for the AI Usage tab showing the same escalation
    model twice: rows from before ``escalation_provider`` existed backfill
    to ``''``, which used to produce a second duplicate-looking ``by_model``
    row alongside newer rows carrying the real provider value."""
    await db.add_clip(_make_clip("u1"))
    await db.add_clip(_make_clip("u2"))
    await db.add_analysis_result(
        {
            "clip_id": "u1",
            "camera": "Front Door",
            "model": "gpt-4o-mini",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.9,
            "summary": "Escalated",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
            "tokens_prompt": 100,
            "tokens_completion": 20,
            "escalation_model": "gpt-4o",
            "escalation_tokens_prompt": 300,
            "escalation_tokens_completion": 60,
            "escalation_provider": "",
        }
    )
    await db.add_analysis_result(
        {
            "clip_id": "u2",
            "camera": "Front Door",
            "model": "gpt-4o-mini",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.9,
            "summary": "Escalated",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-02T09:00:00+00:00",
            "tokens_prompt": 100,
            "tokens_completion": 20,
            "escalation_model": "gpt-4o",
            "escalation_tokens_prompt": 300,
            "escalation_tokens_completion": 60,
            "escalation_provider": "openai",
        }
    )

    resp = await client.get("/api/ai/usage")
    data = await resp.json()

    escalated_rows = [m for m in data["by_model"] if m["model"] == "gpt-4o"]
    assert len(escalated_rows) == 1
    assert escalated_rows[0]["analyses"] == 2
    assert escalated_rows[0]["tokens_prompt"] == 600
    assert escalated_rows[0]["tokens_completion"] == 120


async def test_ai_usage_daily_empty(client: TestClient) -> None:
    resp = await client.get("/api/ai/usage")
    data = await resp.json()
    assert data["daily"] == []


async def test_ai_usage_daily_totals_for_today(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("u1"))
    now = datetime.now(timezone.utc).isoformat()
    await db.add_analysis_result(
        {
            "clip_id": "u1",
            "camera": "Front Door",
            "model": "gpt-4o-mini",
            "response_text": "",
            "is_suspicious": False,
            "confidence": 0.1,
            "summary": "ok",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": now,
            "tokens_prompt": 1_000_000,
            "tokens_completion": 1_000_000,
        }
    )

    resp = await client.get("/api/ai/usage")
    data = await resp.json()

    assert len(data["daily"]) == 1
    row = data["daily"][0]
    assert row["day"] == datetime.now(timezone.utc).date().isoformat()
    assert row["analyses"] == 1
    assert row["tokens_total"] == 2_000_000
    assert row["cost"] == pytest.approx(0.15 + 0.60)


async def test_ai_usage_daily_sums_multiple_models_same_day(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("u1"))
    await db.add_clip(_make_clip("u2"))
    now = datetime.now(timezone.utc).isoformat()
    for clip_id, model in (("u1", "gpt-4o-mini"), ("u2", "llava:7b")):
        await db.add_analysis_result(
            {
                "clip_id": clip_id,
                "camera": "Front Door",
                "model": model,
                "response_text": "",
                "is_suspicious": False,
                "confidence": 0.1,
                "summary": "ok",
                "frame_count": 1,
                "analysis_duration": 1.0,
                "analyzed_at": now,
                "tokens_prompt": 100,
                "tokens_completion": 20,
            }
        )

    resp = await client.get("/api/ai/usage")
    data = await resp.json()

    assert len(data["daily"]) == 1
    row = data["daily"][0]
    assert row["analyses"] == 2
    assert row["tokens_total"] == 240  # (100+20) * 2 clips
    # llava:7b is unpriced (Ollama) — only gpt-4o-mini contributes cost.
    assert row["cost"] == pytest.approx((100 * 0.15 + 20 * 0.60) / 1_000_000)


async def test_ai_usage_clear_endpoint(client: TestClient, db: ClipDatabase) -> None:
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
    assert (await resp.json())["total_analyses"] == 1

    clear_resp = await client.delete("/api/ai/usage")
    assert clear_resp.status == 200
    assert (await clear_resp.json())["cleared"] is True

    resp = await client.get("/api/ai/usage")
    data = await resp.json()
    assert data["total_analyses"] == 0
    assert data["by_model"] == []


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


async def test_ai_camera_configs_get_returns_car_zone_field(
    client: TestClient, tmp_path: Path
) -> None:
    """GET includes a normalised car_zone dict for cameras that have one."""
    import json

    cfg_file = tmp_path / "camera_configs.json"
    cfg_file.write_text(
        json.dumps(
            [
                {
                    "camera": "Driveway",
                    "description": "",
                    "custom_prompt": "",
                    "is_car_camera": True,
                    "car_zone": {
                        "x_min": 0.2,
                        "y_min": 0.3,
                        "x_max": 0.8,
                        "y_max": 0.9,
                    },
                }
            ]
        )
    )

    with patch(
        "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
        new=cfg_file,
    ):
        resp = await client.get("/api/ai/camera-configs")

    assert resp.status == 200
    data = await resp.json()
    driveway = next((c for c in data if c["camera"] == "Driveway"), None)
    if driveway:
        assert driveway["car_zone"] == {
            "x_min": 0.2,
            "y_min": 0.3,
            "x_max": 0.8,
            "y_max": 0.9,
        }


async def test_ai_camera_configs_get_malformed_car_zone_becomes_none(
    client: TestClient, tmp_path: Path
) -> None:
    """A malformed stored car_zone (min >= max) must not be echoed back as-is."""
    import json

    cfg_file = tmp_path / "camera_configs.json"
    cfg_file.write_text(
        json.dumps(
            [
                {
                    "camera": "Driveway",
                    "description": "",
                    "custom_prompt": "",
                    "is_car_camera": True,
                    "car_zone": {
                        "x_min": 0.8,
                        "y_min": 0.3,
                        "x_max": 0.2,
                        "y_max": 0.9,
                    },
                }
            ]
        )
    )

    with patch(
        "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
        new=cfg_file,
    ):
        resp = await client.get("/api/ai/camera-configs")

    data = await resp.json()
    driveway = next((c for c in data if c["camera"] == "Driveway"), None)
    if driveway:
        assert driveway["car_zone"] is None


def test_normalize_car_zone_valid() -> None:
    assert MediaServer._normalize_car_zone(
        {"x_min": 0.1, "y_min": "0.2", "x_max": 0.9, "y_max": 0.95}
    ) == {"x_min": 0.1, "y_min": 0.2, "x_max": 0.9, "y_max": 0.95}


def test_normalize_car_zone_not_a_dict() -> None:
    assert MediaServer._normalize_car_zone("not a dict") is None
    assert MediaServer._normalize_car_zone(None) is None


def test_normalize_car_zone_missing_key() -> None:
    assert MediaServer._normalize_car_zone({"x_min": 0.1, "y_min": 0.2}) is None


def test_normalize_car_zone_non_numeric_value() -> None:
    assert (
        MediaServer._normalize_car_zone(
            {"x_min": "abc", "y_min": 0.2, "x_max": 0.9, "y_max": 0.95}
        )
        is None
    )


def test_normalize_car_zone_inverted_rectangle() -> None:
    assert (
        MediaServer._normalize_car_zone(
            {"x_min": 0.9, "y_min": 0.2, "x_max": 0.1, "y_max": 0.95}
        )
        is None
    )


async def test_ai_camera_configs_put_saves_and_normalizes_car_zone(
    client: TestClient, tmp_path: Path
) -> None:
    """PUT persists a valid car_zone and drops an invalid one to null."""
    payload = [
        {
            "camera": "Driveway",
            "description": "",
            "custom_prompt": "",
            "is_car_camera": True,
            "car_zone": {"x_min": "0.1", "y_min": 0.2, "x_max": 0.9, "y_max": 0.95},
        },
        {
            "camera": "Front Door",
            "description": "",
            "custom_prompt": "",
            "is_car_camera": False,
            "car_zone": {"x_min": 0.9, "y_min": 0.2, "x_max": 0.1, "y_max": 0.95},
        },
    ]
    cfg_file = tmp_path / "camera_configs.json"

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
    saved = json.loads(cfg_file.read_text())
    driveway = next(c for c in saved if c["camera"] == "Driveway")
    assert driveway["car_zone"] == {
        "x_min": 0.1,
        "y_min": 0.2,
        "x_max": 0.9,
        "y_max": 0.95,
    }
    front = next(c for c in saved if c["camera"] == "Front Door")
    assert front["car_zone"] is None


async def test_ai_camera_configs_put_bad_json(client: TestClient) -> None:
    """PUT with invalid JSON returns 400."""
    resp = await client.put(
        "/api/ai/camera-configs",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_ai_camera_configs_put_dict_body_rejected(
    client: TestClient, tmp_path: Path
) -> None:
    """A syntactically-valid JSON body of the wrong shape (a dict, not a
    list) must be rejected with 400 rather than silently iterating to zero
    entries and wiping every camera's saved settings with an empty array."""
    cfg_file = tmp_path / "camera_configs.json"
    existing = [
        {
            "camera": "Driveway",
            "description": "existing description",
            "custom_prompt": "",
            "is_car_camera": True,
            "car_zone": None,
        }
    ]
    cfg_file.write_text(json.dumps(existing))

    with patch(
        "blink_downloader.media_server.MediaServer._CAMERA_CONFIGS_FILE",
        new=cfg_file,
    ):
        resp = await client.put(
            "/api/ai/camera-configs",
            data=json.dumps({}),
            headers={"Content-Type": "application/json"},
        )

    assert resp.status == 400
    assert json.loads(cfg_file.read_text()) == existing


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


async def test_ai_camera_configs_put_updates_live_analyzer_car_zones(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """PUT pushes a valid car_zone into the live analyzer, keyed by camera,
    and omits cameras with no (or an invalid) zone configured."""
    analyzer = _make_analyzer()
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        cfg_file = tmp_path / "camera_configs.json"
        payload = [
            {
                "camera": "Driveway",
                "description": "",
                "custom_prompt": "",
                "is_car_camera": True,
                "car_zone": {"x_min": 0.2, "y_min": 0.3, "x_max": 0.8, "y_max": 0.9},
            },
            {
                "camera": "Front Door",
                "description": "",
                "custom_prompt": "",
                "is_car_camera": False,
            },
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
        analyzer.update_car_zones.assert_called_once_with(
            {"Driveway": {"x_min": 0.2, "y_min": 0.3, "x_max": 0.8, "y_max": 0.9}}
        )
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


async def test_list_clips_negative_limit_and_offset_are_clamped(
    client: TestClient, db: ClipDatabase
) -> None:
    """A negative SQLite LIMIT means "no limit" - clamp to 0 so a crafted
    query string can't bypass pagination and dump the whole table."""
    with patch.object(db, "get_clips", AsyncMock(return_value=[])) as mock_get_clips:
        resp = await client.get("/api/clips?limit=-1&offset=-5")
    assert resp.status == 200
    assert mock_get_clips.call_args.kwargs["limit"] == 0
    assert mock_get_clips.call_args.kwargs["offset"] == 0


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
# /api/clips/{id}/star — malformed body is rejected, matching /tags' sibling
# behavior (regression test: this used to silently fall back to
# starred=True instead of surfacing the client bug)
# ---------------------------------------------------------------------------


async def test_star_clip_bad_json_returns_400(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("st2"))
    resp = await client.put(
        "/api/clips/st2/star",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
    clip = await db.get_clip("st2")
    assert clip is not None
    assert clip["starred"] is False


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
        assert data["car_protection_active"] is False
        assert data["smtp_configured"] is False
        assert "moondream_installed" not in data
    finally:
        await tc.close()


async def test_ai_status_smtp_configured_true(db: ClipDatabase, tmp_path: Path) -> None:
    from blink_downloader.notification_channels import NotificationDispatcher

    analyzer = _make_analyzer(provider="ollama")
    dispatcher = NotificationDispatcher(
        smtp_host="smtp.example.com", smtp_recipients=["a@b.com"]
    )
    server = MediaServer(
        db=db,
        download_path=tmp_path,
        port=0,
        analyzer=analyzer,
        notification_dispatcher=dispatcher,
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        data = await resp.json()
        assert data["smtp_configured"] is True
    finally:
        await tc.close()


async def test_ai_status_car_protection_active_true(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer(provider="openai", car_protection_active=True)
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        data = await resp.json()
        assert data["car_protection_active"] is True
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


async def test_ai_status_includes_escalation_info_when_configured(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """v4.0.0: cross-provider escalation status is surfaced so a
    misconfigured tier 2 is visible before it silently falls back."""
    analyzer = _make_analyzer(provider="openai")
    escalation = _make_analyzer(provider="moondream_cloud", health=True)
    analyzer.escalation_analyzer = escalation
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        data = await resp.json()
        assert data["escalation_provider"] == "moondream_cloud"
        assert data["escalation_model"] == "llava:7b"
        assert data["escalation_online"] is True
    finally:
        await tc.close()


async def test_ai_status_omits_escalation_info_when_not_configured(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer(provider="openai")
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        data = await resp.json()
        assert "escalation_provider" not in data
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
    """Ollama has no model_pricing() at all (it's free/local) — the cost
    header must be omitted based on that capability, not a provider-name
    allowlist (see test_ai_usage_moondream_cloud_gets_cost_fields for the
    v4.0.0 fix that made this hasattr()-based rather than name-based)."""
    analyzer = _make_analyzer(provider="ollama")
    del analyzer.model_pricing
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


async def test_ai_usage_enabled_openai_includes_pricing(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer(provider="openai", pricing=(2.5, 10.0))
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/usage")
        data = await resp.json()
        assert data["cost_per_1m_input"] == 2.5
        assert data["cost_per_1m_output"] == 10.0
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


async def test_ai_clip_result_omits_prompt_text_when_debug_disabled(
    client: TestClient, db: ClipDatabase
) -> None:
    """Off means fully hidden, even if a prompt happens to be stored from
    when ai_prompt_debug_enabled was previously on (v4.0.0)."""
    await db.add_clip(_make_clip("ar2"))
    await db.add_analysis_result(
        {
            "clip_id": "ar2",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": False,
            "confidence": 0.1,
            "summary": "ok",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
            "prompt_text": "the exact prompt",
        }
    )
    resp = await client.get("/api/ai/results/ar2")
    data = await resp.json()
    assert "prompt_text" not in data


async def test_ai_clip_result_includes_prompt_text_when_debug_enabled(
    db: ClipDatabase, tmp_path: Path
) -> None:
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, prompt_debug_enabled=True
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        await db.add_clip(_make_clip("ar3"))
        await db.add_analysis_result(
            {
                "clip_id": "ar3",
                "camera": "Front Door",
                "model": "llava",
                "response_text": "",
                "is_suspicious": False,
                "confidence": 0.1,
                "summary": "ok",
                "frame_count": 1,
                "analysis_duration": 1.0,
                "analyzed_at": "2024-06-01T09:00:00+00:00",
                "prompt_text": "the exact prompt",
            }
        )
        resp = await tc.get("/api/ai/results/ar3")
        data = await resp.json()
        assert data["prompt_text"] == "the exact prompt"
    finally:
        await tc.close()


async def test_ai_status_reports_prompt_debug_enabled(
    db: ClipDatabase, tmp_path: Path
) -> None:
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, prompt_debug_enabled=True
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/status")
        data = await resp.json()
        assert data["prompt_debug_enabled"] is True
    finally:
        await tc.close()


async def test_ai_status_prompt_debug_disabled_by_default(
    client: TestClient,
) -> None:
    resp = await client.get("/api/ai/status")
    data = await resp.json()
    assert data["prompt_debug_enabled"] is False


async def test_ai_suspicious_invalid_limit_falls_back(client: TestClient) -> None:
    resp = await client.get("/api/ai/suspicious?limit=notanumber")
    assert resp.status == 200


async def test_ai_suspicious_negative_limit_and_offset_are_clamped(
    client: TestClient, db: ClipDatabase
) -> None:
    with patch.object(
        db, "get_suspicious_clips", AsyncMock(return_value=[])
    ) as mock_get_suspicious:
        resp = await client.get("/api/ai/suspicious?limit=-10&offset=-1")
    assert resp.status == 200
    assert mock_get_suspicious.call_args.kwargs["limit"] == 0
    assert mock_get_suspicious.call_args.kwargs["offset"] == 0


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
    await db.add_clip(_make_clip("an1", duration=47))
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
        # Ground-truth clip duration (from the Blink API metadata already in
        # the DB) must reach the analyzer so long-clip frame doubling is
        # driven by the real duration, not an estimate.
        assert analyzer.analyze_clip.call_args.kwargs["clip_duration"] == 47.0
    finally:
        await tc.close()


async def test_ai_analyze_now_exception_returns_500(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """An unexpected analyze_clip() failure must return a clean {"error": ...}
    JSON response (mirroring /api/ai/test's error handling) rather than
    propagating and surfacing as aiohttp's generic HTML 500 page."""
    await db.add_clip(_make_clip("an2"))
    analyzer = _make_analyzer()
    analyzer.analyze_clip = AsyncMock(side_effect=RuntimeError("model unreachable"))
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/analyze/an2")
        assert resp.status == 500
        data = await resp.json()
        assert "model unreachable" in data["error"]
        # The failed analysis must not be persisted to the DB.
        assert await db.get_analysis_for_clip("an2") is None
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
    await db.add_clip(_make_clip("at1", duration=52))
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
        assert analyzer.analyze_clip.call_args.kwargs["clip_duration"] == 52.0
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
# /api/notifications/test-email
# ---------------------------------------------------------------------------


async def test_test_email_no_dispatcher(client: TestClient) -> None:
    resp = await client.post("/api/notifications/test-email")
    assert resp.status == 400
    data = await resp.json()
    assert data["success"] is False


async def test_test_email_success(db: ClipDatabase, tmp_path: Path) -> None:
    from blink_downloader.notification_channels import NotificationDispatcher

    dispatcher = NotificationDispatcher(
        smtp_host="smtp.example.com", smtp_recipients=["a@b.com"]
    )
    dispatcher.send_test_email = AsyncMock(
        return_value=(True, "Test email sent to a@b.com.")
    )
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, notification_dispatcher=dispatcher
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/notifications/test-email")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert "a@b.com" in data["message"]
    finally:
        await tc.close()


async def test_test_email_failure(db: ClipDatabase, tmp_path: Path) -> None:
    from blink_downloader.notification_channels import NotificationDispatcher

    dispatcher = NotificationDispatcher(smtp_host="", smtp_recipients=[])
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, notification_dispatcher=dispatcher
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/notifications/test-email")
        assert resp.status == 400
        data = await resp.json()
        assert data["success"] is False
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


# ===========================================================================
# v4.0.0 — Adaptive learning feedback endpoints
# ===========================================================================


async def test_ai_feedback_get_missing(client: TestClient) -> None:
    resp = await client.get("/api/ai/feedback/ghost")
    assert resp.status == 200
    assert await resp.json() is None


async def test_ai_feedback_submit_requires_existing_analysis(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("c1"))
    resp = await client.post("/api/ai/feedback/c1", json={"correct": False})
    assert resp.status == 400
    data = await resp.json()
    assert "not been analyzed" in data["error"]


async def test_ai_feedback_submit_bad_json(client: TestClient) -> None:
    resp = await client.post(
        "/api/ai/feedback/c1", data="not json", headers={"Content-Type": "text/plain"}
    )
    assert resp.status == 400


async def test_ai_feedback_submit_and_get(client: TestClient, db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.8,
            "summary": "Person at door",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.post(
        "/api/ai/feedback/c1",
        json={
            "correct": False,
            "correction_note": "Just the mail carrier.",
            "corrected_suspicious": False,
        },
    )
    assert resp.status == 200
    assert (await resp.json())["saved"] is True

    resp = await client.get("/api/ai/feedback/c1")
    data = await resp.json()
    assert data["camera"] == "Front Door"
    assert data["original_suspicious"] is True
    assert data["correct"] is False
    assert data["correction_note"] == "Just the mail carrier."
    assert data["corrected_suspicious"] is False


async def test_ai_feedback_submit_derives_corrected_suspicious_for_false_positive(
    client: TestClient, db: ClipDatabase
) -> None:
    """correct=False always means the single is_suspicious boolean was wrong
    — there's no third option, so when the caller omits corrected_suspicious
    entirely it must be derived as the opposite of the original verdict, not
    left null. A clip flagged suspicious (True) and marked incorrect must
    derive corrected_suspicious=False (it should NOT have been flagged) —
    the correction direction that was previously impossible to express and
    silently fell back to the *original* (wrong) label at fine-tune time."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.8,
            "summary": "Person at door",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.post(
        "/api/ai/feedback/c1",
        json={"correct": False, "correction_note": "Just the mail carrier."},
    )
    assert resp.status == 200

    data = await (await client.get("/api/ai/feedback/c1")).json()
    assert data["corrected_suspicious"] is False


async def test_ai_feedback_submit_derives_corrected_suspicious_for_false_negative(
    client: TestClient, db: ClipDatabase
) -> None:
    """The reverse direction: a clip cleared by the AI (is_suspicious=False)
    and marked incorrect must derive corrected_suspicious=True when omitted."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Driveway",
            "model": "llava",
            "response_text": "",
            "is_suspicious": False,
            "confidence": 0.89,
            "summary": "Person pauses near the car",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.post(
        "/api/ai/feedback/c1",
        json={"correct": False, "correction_note": "Actually suspicious."},
    )
    assert resp.status == 200

    data = await (await client.get("/api/ai/feedback/c1")).json()
    assert data["corrected_suspicious"] is True


async def test_ai_feedback_submit_correct_true_leaves_corrected_suspicious_null(
    client: TestClient, db: ClipDatabase
) -> None:
    """When the reviewer confirms the verdict was correct, there is nothing
    to correct — corrected_suspicious must stay null, not be derived."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.8,
            "summary": "Person at door",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.post("/api/ai/feedback/c1", json={"correct": True})
    assert resp.status == 200

    data = await (await client.get("/api/ai/feedback/c1")).json()
    assert data["corrected_suspicious"] is None


async def test_ai_feedback_submit_explicit_corrected_suspicious_is_respected(
    client: TestClient, db: ClipDatabase
) -> None:
    """An explicit corrected_suspicious from the caller is still honored
    rather than always overridden by the derivation."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.8,
            "summary": "Person at door",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.post(
        "/api/ai/feedback/c1",
        json={"correct": False, "corrected_suspicious": False},
    )
    assert resp.status == 200
    data = await (await client.get("/api/ai/feedback/c1")).json()
    assert data["corrected_suspicious"] is False


async def test_ai_feedback_delete_removes_row(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        corrected_suspicious=False,
    )
    resp = await client.delete("/api/ai/feedback/c1")
    assert resp.status == 200
    assert (await resp.json())["deleted"] is True
    assert await db.get_feedback_for_clip("c1") is None


async def test_ai_feedback_delete_missing_returns_false(client: TestClient) -> None:
    resp = await client.delete("/api/ai/feedback/ghost")
    assert resp.status == 200
    assert (await resp.json())["deleted"] is False


async def test_ai_feedback_submit_autofills_note_for_false_positive(
    client: TestClient, db: ClipDatabase
) -> None:
    """A bare thumbs-down with no typed note must still get a usable
    correction_note — get_prompt_corrections() only folds in rows with a
    non-empty note, so a silent thumbs-down would otherwise teach the
    prompt nothing."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.85,
            "summary": "Person handles the door before leaving",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.post(
        "/api/ai/feedback/c1",
        json={"correct": False, "correction_note": "", "corrected_suspicious": False},
    )
    assert resp.status == 200

    resp = await client.get("/api/ai/feedback/c1")
    data = await resp.json()
    assert data["correction_note"]
    assert "incorrectly flagged suspicious" in data["correction_note"]


async def test_ai_feedback_submit_autofills_note_for_false_negative(
    client: TestClient, db: ClipDatabase
) -> None:
    """The reverse direction — a clip cleared by the AI but marked incorrect
    with no note — also gets an auto-generated note, so a missed detection
    (e.g. the protected-vehicle proximity miss) still feeds back into future
    prompts even without free text."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Driveway",
            "model": "llava",
            "response_text": "",
            "is_suspicious": False,
            "confidence": 0.89,
            "summary": "Person pauses near the car",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.post(
        "/api/ai/feedback/c1",
        json={"correct": False, "correction_note": "", "corrected_suspicious": True},
    )
    assert resp.status == 200

    resp = await client.get("/api/ai/feedback/c1")
    data = await resp.json()
    assert data["correction_note"]
    assert "incorrectly cleared" in data["correction_note"]


async def test_ai_feedback_submit_keeps_typed_note(
    client: TestClient, db: ClipDatabase
) -> None:
    """A note the user actually typed is never overwritten by the
    auto-generated fallback."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.85,
            "summary": "Person at door",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    resp = await client.post(
        "/api/ai/feedback/c1",
        json={"correct": False, "correction_note": "Just the mail carrier."},
    )
    assert resp.status == 200

    resp = await client.get("/api/ai/feedback/c1")
    data = await resp.json()
    assert data["correction_note"] == "Just the mail carrier."


# ---------------------------------------------------------------------------
# /api/ai/faces — local-only face-recognition enrollment
# ---------------------------------------------------------------------------


async def _real_jpeg_base64() -> str:
    import base64
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (10, 10), color=(100, 100, 100)).save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def test_faces_list_empty(client: TestClient) -> None:
    resp = await client.get("/api/ai/faces")
    assert resp.status == 200
    data = await resp.json()
    assert data["faces"] == []


async def test_faces_enroll_requires_name(client: TestClient) -> None:
    resp = await client.post(
        "/api/ai/faces",
        json={"name": "", "image_base64": await _real_jpeg_base64()},
    )
    assert resp.status == 400


async def test_faces_enroll_requires_image(client: TestClient) -> None:
    resp = await client.post("/api/ai/faces", json={"name": "Brian"})
    assert resp.status == 400


async def test_faces_enroll_rejects_invalid_base64(client: TestClient) -> None:
    resp = await client.post(
        "/api/ai/faces", json={"name": "Brian", "image_base64": "not-base64!!"}
    )
    assert resp.status == 400


async def test_faces_enroll_bad_json(client: TestClient) -> None:
    resp = await client.post(
        "/api/ai/faces", data="not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


async def test_faces_enroll_unavailable_when_dependency_missing(
    client: TestClient,
) -> None:
    with patch(
        "blink_downloader.media_server.is_face_recognition_available",
        return_value=False,
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={"name": "Brian", "image_base64": await _real_jpeg_base64()},
        )
    assert resp.status == 400


async def test_faces_enroll_no_face_detected(client: TestClient) -> None:
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={"name": "Brian", "image_base64": await _real_jpeg_base64()},
        )
    assert resp.status == 400


async def test_faces_enroll_multiple_faces_rejected(client: TestClient) -> None:
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(return_value=[[0.1, 0.2], [0.3, 0.4]]),
        ),
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={"name": "Brian", "image_base64": await _real_jpeg_base64()},
        )
    assert resp.status == 400


async def test_faces_enroll_success_then_list_then_delete(client: TestClient) -> None:
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
        ),
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={"name": "Brian", "image_base64": await _real_jpeg_base64()},
        )
        assert resp.status == 200
        enrolled = await resp.json()
        assert enrolled["name"] == "Brian"

    resp = await client.get("/api/ai/faces")
    data = await resp.json()
    assert len(data["faces"]) == 1
    face_id = data["faces"][0]["id"]

    resp = await client.delete(f"/api/ai/faces/{face_id}")
    assert resp.status == 200

    resp = await client.get("/api/ai/faces")
    data = await resp.json()
    assert data["faces"] == []


async def test_faces_enroll_accepts_realistic_photo_size(client: TestClient) -> None:
    """A real phone photo, base64-encoded, routinely exceeds aiohttp's
    default 1 MB request-body limit — _build_app() raises client_max_size
    specifically so a legitimate enrollment photo isn't rejected with an
    opaque 413 before the handler even runs."""
    import base64

    large_payload = base64.b64encode(b"\xff" * (2 * 1024 * 1024)).decode()
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
        ),
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={"name": "Brian", "image_base64": large_payload},
        )
    assert resp.status != 413
    assert resp.status == 200


async def test_faces_delete_invalid_id(client: TestClient) -> None:
    resp = await client.delete("/api/ai/faces/not-a-number")
    assert resp.status == 400


async def test_faces_enroll_defaults_to_approved(client: TestClient) -> None:
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
        ),
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={"name": "Brian", "image_base64": await _real_jpeg_base64()},
        )
        assert resp.status == 200
        assert (await resp.json())["approved"] is True

    resp = await client.get("/api/ai/faces")
    data = await resp.json()
    assert data["faces"][0]["approved"] is True


async def test_faces_enroll_explicitly_unapproved(client: TestClient) -> None:
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
        ),
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={
                "name": "Nanny",
                "image_base64": await _real_jpeg_base64(),
                "approved": False,
            },
        )
        assert resp.status == 200
        assert (await resp.json())["approved"] is False

    resp = await client.get("/api/ai/faces")
    data = await resp.json()
    assert data["faces"][0]["approved"] is False


async def test_faces_patch_updates_approved(client: TestClient) -> None:
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
        ),
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={"name": "Brian", "image_base64": await _real_jpeg_base64()},
        )
    face_id = (await resp.json())["id"]

    resp = await client.patch(f"/api/ai/faces/{face_id}", json={"approved": False})
    assert resp.status == 200

    resp = await client.get("/api/ai/faces")
    data = await resp.json()
    assert data["faces"][0]["approved"] is False


async def test_faces_patch_updates_name(client: TestClient) -> None:
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(return_value=[[0.1, 0.2, 0.3]]),
        ),
    ):
        resp = await client.post(
            "/api/ai/faces",
            json={"name": "Brain", "image_base64": await _real_jpeg_base64()},
        )
    face_id = (await resp.json())["id"]

    resp = await client.patch(f"/api/ai/faces/{face_id}", json={"name": "Brian"})
    assert resp.status == 200

    resp = await client.get("/api/ai/faces")
    data = await resp.json()
    assert data["faces"][0]["name"] == "Brian"


async def test_faces_patch_rejects_empty_name(client: TestClient) -> None:
    resp = await client.patch("/api/ai/faces/1", json={"name": "   "})
    assert resp.status == 400


async def test_faces_patch_requires_at_least_one_field(client: TestClient) -> None:
    resp = await client.patch("/api/ai/faces/1", json={})
    assert resp.status == 400


async def test_faces_patch_invalid_id(client: TestClient) -> None:
    resp = await client.patch("/api/ai/faces/not-a-number", json={"approved": True})
    assert resp.status == 400


async def test_faces_patch_bad_json(client: TestClient) -> None:
    resp = await client.patch(
        "/api/ai/faces/1", data="not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# /api/ai/faces/by-name/{name} — bulk multi-frame-enrollment management
# ---------------------------------------------------------------------------


async def _enroll_two_photos(client: TestClient, name: str = "Brian") -> None:
    with (
        patch(
            "blink_downloader.media_server.is_face_recognition_available",
            return_value=True,
        ),
        patch(
            "blink_downloader.media_server.FaceEmbedder.embed",
            new=AsyncMock(side_effect=[[[0.1, 0.2]], [[0.3, 0.4]]]),
        ),
    ):
        for _ in range(2):
            resp = await client.post(
                "/api/ai/faces",
                json={"name": name, "image_base64": await _real_jpeg_base64()},
            )
            assert resp.status == 200


async def test_faces_patch_by_name_updates_approved_for_all_photos(
    client: TestClient,
) -> None:
    await _enroll_two_photos(client)
    resp = await client.patch("/api/ai/faces/by-name/Brian", json={"approved": False})
    assert resp.status == 200

    data = await (await client.get("/api/ai/faces")).json()
    assert len(data["faces"]) == 2
    assert all(f["approved"] is False for f in data["faces"])


async def test_faces_patch_by_name_renames_all_photos(client: TestClient) -> None:
    await _enroll_two_photos(client, name="Brain")
    resp = await client.patch("/api/ai/faces/by-name/Brain", json={"name": "Brian"})
    assert resp.status == 200

    data = await (await client.get("/api/ai/faces")).json()
    assert all(f["name"] == "Brian" for f in data["faces"])


async def test_faces_patch_by_name_rejects_empty_name(client: TestClient) -> None:
    resp = await client.patch("/api/ai/faces/by-name/Brian", json={"name": "   "})
    assert resp.status == 400


async def test_faces_patch_by_name_requires_at_least_one_field(
    client: TestClient,
) -> None:
    resp = await client.patch("/api/ai/faces/by-name/Brian", json={})
    assert resp.status == 400


async def test_faces_patch_by_name_bad_json(client: TestClient) -> None:
    resp = await client.patch(
        "/api/ai/faces/by-name/Brian",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_faces_delete_by_name_removes_all_photos(client: TestClient) -> None:
    await _enroll_two_photos(client)
    resp = await client.delete("/api/ai/faces/by-name/Brian")
    assert resp.status == 200
    assert (await resp.json())["deleted"] is True

    data = await (await client.get("/api/ai/faces")).json()
    assert data["faces"] == []


# ---------------------------------------------------------------------------
# Vehicle settings (Vehicles tab) — /api/vehicle/settings
# ---------------------------------------------------------------------------


async def test_vehicle_settings_get_falls_back_to_analyzer_when_no_file(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer()
    analyzer.car_description = "Silver Kia Forte"
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with patch(
            "blink_downloader.media_server.MediaServer._VEHICLE_SETTINGS_FILE",
            new=tmp_path / "no-such-file.json",
        ):
            resp = await tc.get("/api/vehicle/settings")
        assert resp.status == 200
        assert (await resp.json())["car_description"] == "Silver Kia Forte"
    finally:
        await tc.close()


async def test_vehicle_settings_get_empty_without_analyzer(
    db: ClipDatabase, tmp_path: Path
) -> None:
    server = MediaServer(db=db, download_path=tmp_path, port=0)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with patch(
            "blink_downloader.media_server.MediaServer._VEHICLE_SETTINGS_FILE",
            new=tmp_path / "no-such-file.json",
        ):
            resp = await tc.get("/api/vehicle/settings")
        assert resp.status == 200
        assert (await resp.json())["car_description"] == ""
    finally:
        await tc.close()


async def test_vehicle_settings_put_then_get_round_trips(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer()
    analyzer.car_description = ""
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    settings_file = tmp_path / "vehicle_settings.json"
    try:
        with patch(
            "blink_downloader.media_server.MediaServer._VEHICLE_SETTINGS_FILE",
            new=settings_file,
        ):
            resp = await tc.put(
                "/api/vehicle/settings", json={"car_description": "Silver Kia Forte"}
            )
            assert resp.status == 200
            assert (await resp.json())["saved"] is True

            resp = await tc.get("/api/vehicle/settings")
            assert (await resp.json())["car_description"] == "Silver Kia Forte"
        analyzer.update_car_description.assert_called_once_with("Silver Kia Forte")
    finally:
        await tc.close()


async def test_vehicle_settings_get_falls_back_on_corrupt_file(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer()
    analyzer.car_description = "Silver Kia Forte"
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    corrupt_file = tmp_path / "vehicle_settings.json"
    corrupt_file.write_text("not valid json")
    try:
        with patch(
            "blink_downloader.media_server.MediaServer._VEHICLE_SETTINGS_FILE",
            new=corrupt_file,
        ):
            resp = await tc.get("/api/vehicle/settings")
        assert resp.status == 200
        assert (await resp.json())["car_description"] == "Silver Kia Forte"
    finally:
        await tc.close()


async def test_vehicle_settings_put_survives_write_failure(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """A filesystem error saving to disk must not crash the request — the
    live analyzer still gets updated for this run, matching the
    camera_configs.json precedent (_handle_ai_camera_configs_put)."""
    analyzer = _make_analyzer()
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    # A directory used as the settings "file" makes write_text() raise OSError.
    unwritable = tmp_path / "not-a-file"
    unwritable.mkdir()
    try:
        with patch(
            "blink_downloader.media_server.MediaServer._VEHICLE_SETTINGS_FILE",
            new=unwritable,
        ):
            resp = await tc.put(
                "/api/vehicle/settings", json={"car_description": "Silver Kia"}
            )
        assert resp.status == 200
        assert (await resp.json())["saved"] is True
        analyzer.update_car_description.assert_called_once_with("Silver Kia")
    finally:
        await tc.close()


async def test_vehicle_settings_put_bad_json(db: ClipDatabase, tmp_path: Path) -> None:
    server = MediaServer(db=db, download_path=tmp_path, port=0)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.put(
            "/api/vehicle/settings",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
    finally:
        await tc.close()


# ---------------------------------------------------------------------------
# Escalation model picker — /api/ai/models/escalation
# ---------------------------------------------------------------------------


async def test_ai_models_escalation_no_analyzer(client: TestClient) -> None:
    resp = await client.get("/api/ai/models/escalation")
    assert resp.status == 400
    assert (await resp.json())["enabled"] is False


async def test_ai_models_escalation_no_escalation_configured(
    db: ClipDatabase, tmp_path: Path
) -> None:
    analyzer = _make_analyzer(escalation_analyzer=None)
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/models/escalation")
        assert resp.status == 400
        data = await resp.json()
        assert data["enabled"] is False
        assert "error" in data
    finally:
        await tc.close()


async def test_ai_models_escalation_returns_models(
    db: ClipDatabase, tmp_path: Path
) -> None:
    escalation = _make_analyzer(models=["claude-haiku-4-5", "claude-opus-4-8"])
    analyzer = _make_analyzer(escalation_analyzer=escalation)
    server = MediaServer(db=db, download_path=tmp_path, port=0, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/models/escalation")
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is True
        assert data["models"] == ["claude-haiku-4-5", "claude-opus-4-8"]
        escalation.fetch_models.assert_awaited_once()
    finally:
        await tc.close()


# ---------------------------------------------------------------------------
# Notification test endpoints — /api/notifications/test-discord, test-mobile
# ---------------------------------------------------------------------------


async def test_test_discord_without_dispatcher(client: TestClient) -> None:
    resp = await client.post("/api/notifications/test-discord")
    assert resp.status == 400
    assert (await resp.json())["success"] is False


async def test_test_mobile_without_dispatcher(client: TestClient) -> None:
    resp = await client.post("/api/notifications/test-mobile")
    assert resp.status == 400
    assert (await resp.json())["success"] is False


async def test_test_discord_success(db: ClipDatabase, tmp_path: Path) -> None:
    dispatcher = MagicMock()
    dispatcher.send_test_discord = AsyncMock(return_value=(True, "Test message sent."))
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, notification_dispatcher=dispatcher
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/notifications/test-discord")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["message"] == "Test message sent."
    finally:
        await tc.close()


async def test_test_discord_failure_status(db: ClipDatabase, tmp_path: Path) -> None:
    dispatcher = MagicMock()
    dispatcher.send_test_discord = AsyncMock(return_value=(False, "Webhook not set."))
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, notification_dispatcher=dispatcher
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/notifications/test-discord")
        assert resp.status == 400
    finally:
        await tc.close()


async def test_test_mobile_success(db: ClipDatabase, tmp_path: Path) -> None:
    dispatcher = MagicMock()
    dispatcher.send_test_mobile = AsyncMock(return_value=(True, "Test sent."))
    server = MediaServer(
        db=db, download_path=tmp_path, port=0, notification_dispatcher=dispatcher
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/notifications/test-mobile")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
    finally:
        await tc.close()


async def test_ai_feedback_stats_empty(client: TestClient) -> None:
    resp = await client.get("/api/ai/feedback/stats")
    assert resp.status == 200
    data = await resp.json()
    assert data["total"] == 0


async def test_ai_feedback_stats_filters_by_camera(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_feedback(
        "c1",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=True,
    )
    resp = await client.get("/api/ai/feedback/stats?camera=Front Door")
    data = await resp.json()
    assert data["total"] == 1

    resp2 = await client.get("/api/ai/feedback/stats?camera=Nowhere")
    data2 = await resp2.json()
    assert data2["total"] == 0


async def test_ai_feedback_submit_db_failure_returns_500(
    client: TestClient, db: ClipDatabase
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": True,
            "confidence": 0.8,
            "summary": "Person at door",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    with patch.object(db, "add_feedback", side_effect=RuntimeError("db locked")):
        resp = await client.post("/api/ai/feedback/c1", json={"correct": False})
    assert resp.status == 500
    data = await resp.json()
    assert "db locked" in data["error"]


# ===========================================================================
# v4.0.0 — AI Usage cost header available for any priced provider
# ===========================================================================


async def test_ai_usage_moondream_cloud_gets_cost_fields(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Regression test: the cost_per_1m_* header used to be shown only for
    anthropic/openai even though MoondreamCloudAnalyzer also has
    model_pricing() — dropping that allowlist lets any priced provider's
    AI Usage tab show its own cost rate."""
    analyzer = _make_analyzer(provider="moondream_cloud", pricing=(3.0, 15.0))
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


# ===========================================================================
# v4.0.0 — Moondream Cloud fine-tuning endpoints
# ===========================================================================


async def test_finetune_list_not_configured(client: TestClient) -> None:
    resp = await client.get("/api/ai/finetune")
    assert resp.status == 200
    data = await resp.json()
    assert data == {"enabled": False, "finetunes": []}


async def test_finetune_list_wrong_provider(db: ClipDatabase, tmp_path: Path) -> None:
    analyzer = _make_analyzer(provider="openai")
    server = MediaServer(
        db=db,
        download_path=tmp_path,
        port=0,
        analyzer=analyzer,
        moondream_api_key="md-key",
    )
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/finetune")
        data = await resp.json()
        assert data["enabled"] is False
    finally:
        await tc.close()


def _make_finetune_server(db: ClipDatabase, tmp_path: Path, analyzer=None):
    from blink_downloader.analyzer import MoondreamCloudAnalyzer

    if analyzer is None:
        analyzer = MoondreamCloudAnalyzer(api_key="md-key", prompt="test")
    return MediaServer(
        db=db,
        download_path=tmp_path,
        port=0,
        analyzer=analyzer,
        moondream_api_key="md-key",
    )


async def test_finetune_list_success(db: ClipDatabase, tmp_path: Path) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.list_finetunes",
                new=AsyncMock(return_value=[{"finetune_id": "ft1", "name": "test"}]),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.get("/api/ai/finetune")
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is True
        assert data["finetunes"] == [{"finetune_id": "ft1", "name": "test"}]
    finally:
        await tc.close()


async def test_finetune_create_missing_name(db: ClipDatabase, tmp_path: Path) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with patch(
            "blink_downloader.analyzer.MoondreamFineTuneManager.close",
            new=AsyncMock(),
        ):
            resp = await tc.post("/api/ai/finetune", json={"name": "", "rank": 16})
        assert resp.status == 400
    finally:
        await tc.close()


async def test_finetune_create_success(db: ClipDatabase, tmp_path: Path) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.create_finetune",
                new=AsyncMock(return_value="ft-new"),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post(
                "/api/ai/finetune", json={"name": "my-tune", "rank": 16}
            )
        assert resp.status == 200
        data = await resp.json()
        assert data["finetune_id"] == "ft-new"
    finally:
        await tc.close()


async def test_finetune_create_not_configured(client: TestClient) -> None:
    resp = await client.post("/api/ai/finetune", json={"name": "x"})
    assert resp.status == 400


async def test_finetune_create_bad_json(db: ClipDatabase, tmp_path: Path) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with patch(
            "blink_downloader.analyzer.MoondreamFineTuneManager.close",
            new=AsyncMock(),
        ):
            resp = await tc.post(
                "/api/ai/finetune",
                data="not json",
                headers={"Content-Type": "text/plain"},
            )
        assert resp.status == 400
    finally:
        await tc.close()


async def test_finetune_create_manager_returns_none(
    db: ClipDatabase, tmp_path: Path
) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.create_finetune",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post("/api/ai/finetune", json={"name": "my-tune"})
        assert resp.status == 500
    finally:
        await tc.close()


async def test_finetune_create_raises_returns_500(
    db: ClipDatabase, tmp_path: Path
) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.create_finetune",
                new=AsyncMock(side_effect=RuntimeError("network error")),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post("/api/ai/finetune", json={"name": "my-tune"})
        assert resp.status == 500
        data = await resp.json()
        assert "network error" in data["error"]
    finally:
        await tc.close()


async def test_finetune_get_found(db: ClipDatabase, tmp_path: Path) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.get_finetune",
                new=AsyncMock(return_value={"finetune_id": "ft1"}),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.get("/api/ai/finetune/ft1")
        assert resp.status == 200
    finally:
        await tc.close()


async def test_finetune_get_not_found(db: ClipDatabase, tmp_path: Path) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.get_finetune",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.get("/api/ai/finetune/ghost")
        assert resp.status == 404
    finally:
        await tc.close()


async def test_finetune_get_not_configured(client: TestClient) -> None:
    resp = await client.get("/api/ai/finetune/ft1")
    assert resp.status == 400


async def test_finetune_delete(db: ClipDatabase, tmp_path: Path) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.delete_finetune",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.delete("/api/ai/finetune/ft1")
        assert resp.status == 200
        assert (await resp.json())["deleted"] is True
    finally:
        await tc.close()


async def test_finetune_delete_not_configured(client: TestClient) -> None:
    resp = await client.delete("/api/ai/finetune/ft1")
    assert resp.status == 400


async def test_finetune_checkpoints(db: ClipDatabase, tmp_path: Path) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.list_checkpoints",
                new=AsyncMock(return_value=[{"step": 50}]),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.get("/api/ai/finetune/ft1/checkpoints")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"enabled": True, "checkpoints": [{"step": 50}]}
    finally:
        await tc.close()


async def test_finetune_checkpoints_not_configured(client: TestClient) -> None:
    resp = await client.get("/api/ai/finetune/ft1/checkpoints")
    data = await resp.json()
    assert data == {"enabled": False, "checkpoints": []}


async def test_finetune_activate_success(db: ClipDatabase, tmp_path: Path) -> None:
    from blink_downloader.analyzer import MoondreamCloudAnalyzer

    analyzer = MoondreamCloudAnalyzer(api_key="md-key", prompt="test")
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/finetune/ft1/activate", json={"step": 50})
        assert resp.status == 200
        data = await resp.json()
        assert data["activated"] is True
        assert analyzer.model_name() == data["model"]
        assert "ft1" in analyzer.model_name()
    finally:
        await tc.close()


async def test_finetune_activate_wrong_provider(client: TestClient) -> None:
    resp = await client.post("/api/ai/finetune/ft1/activate", json={"step": 50})
    assert resp.status == 400


async def test_finetune_activate_bad_json(db: ClipDatabase, tmp_path: Path) -> None:
    from blink_downloader.analyzer import MoondreamCloudAnalyzer

    analyzer = MoondreamCloudAnalyzer(api_key="md-key", prompt="test")
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post(
            "/api/ai/finetune/ft1/activate",
            data="not json",
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status == 400
    finally:
        await tc.close()


# ===========================================================================
# v5.0.0 — training a Moondream fine-tune from stored human feedback
# ===========================================================================


async def _add_feedback_with_clip(
    db: ClipDatabase,
    clip_id: str = "c1",
    camera: str = "Front Door",
    corrected_suspicious: bool | None = False,
) -> None:
    await db.add_clip(_make_clip(clip_id, camera=camera))
    await db.add_feedback(
        clip_id=clip_id,
        camera=camera,
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        correction_note="It was just the mail carrier.",
        corrected_suspicious=corrected_suspicious,
    )


def _moondream_train_analyzer(**overrides) -> MagicMock:
    analyzer = _make_analyzer(provider="moondream_cloud", **overrides)
    analyzer.extract_frames = AsyncMock(
        return_value=overrides.get("frames", [b"f1", b"f2", b"f3"])
    )
    analyzer.base_prompt_for_camera = MagicMock(
        return_value="Is anything suspicious happening?"
    )
    return analyzer


async def test_finetune_train_not_configured(client: TestClient) -> None:
    resp = await client.post("/api/ai/finetune/ft1/train", json={})
    assert resp.status == 400


async def test_finetune_train_no_feedback(db: ClipDatabase, tmp_path: Path) -> None:
    analyzer = _moondream_train_analyzer()
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/finetune/ft1/train", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["trained"] == 0
    finally:
        await tc.close()


async def test_finetune_train_success(db: ClipDatabase, tmp_path: Path) -> None:
    await _add_feedback_with_clip(db)
    analyzer = _moondream_train_analyzer()
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.train_from_examples",
                new=AsyncMock(
                    return_value={
                        "steps_completed": 1,
                        "results": [{}],
                        "successful_indices": [0],
                    }
                ),
            ) as mock_train,
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post("/api/ai/finetune/ft1/train", json={"limit": 5})
        assert resp.status == 200
        data = await resp.json()
        assert data["trained"] == 1
        assert data["finetune_id"] == "ft1"

        # The example passed to train_from_examples used the corrected
        # verdict and the analyzer's base prompt as the training question.
        examples = mock_train.call_args.args[1]
        assert examples[0]["question"] == "Is anything suspicious happening?"
        ground_truth = json.loads(examples[0]["ground_truth"])
        assert ground_truth["suspicious"] is False

        # Trained feedback is not returned again.
        remaining = await db.get_untrained_feedback(limit=10)
        assert remaining == []
    finally:
        await tc.close()


async def test_finetune_train_failed_step_leaves_feedback_untrained(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """A feedback row behind a training step that didn't actually complete
    (e.g. a transient Moondream API error) must not be marked trained —
    otherwise that feedback's signal is silently and permanently lost with
    no way to retry it on a later run."""
    await _add_feedback_with_clip(db)
    analyzer = _moondream_train_analyzer()
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.train_from_examples",
                new=AsyncMock(
                    return_value={
                        "steps_completed": 0,
                        "results": [],
                        "successful_indices": [],
                    }
                ),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post("/api/ai/finetune/ft1/train", json={"limit": 5})
        assert resp.status == 200
        data = await resp.json()
        assert data["trained"] == 0

        # Left untrained so a future run can retry it.
        remaining = await db.get_untrained_feedback(limit=10)
        assert len(remaining) == 1
    finally:
        await tc.close()


async def test_finetune_train_skips_feedback_with_missing_clip(
    db: ClipDatabase, tmp_path: Path
) -> None:
    # A clip with no stored file path (e.g. archived/purged from disk but
    # its DB row and feedback survive) — get_clip() succeeds but there's
    # nothing to extract a training frame from.
    await db.add_clip(_make_clip("c1", path=""))
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        corrected_suspicious=False,
    )
    analyzer = _moondream_train_analyzer()
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/finetune/ft1/train", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["trained"] == 0
        # Left untrained so a future run (once the clip exists again) retries it.
        assert len(await db.get_untrained_feedback(limit=10)) == 1
    finally:
        await tc.close()


async def test_finetune_train_bad_json_falls_back_to_default_limit(
    db: ClipDatabase, tmp_path: Path
) -> None:
    await _add_feedback_with_clip(db)
    analyzer = _moondream_train_analyzer()
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.train_from_examples",
                new=AsyncMock(
                    return_value={
                        "steps_completed": 1,
                        "results": [{}],
                        "successful_indices": [0],
                    }
                ),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post(
                "/api/ai/finetune/ft1/train",
                data="not json",
                headers={"Content-Type": "text/plain"},
            )
        assert resp.status == 200
        data = await resp.json()
        assert data["trained"] == 1
    finally:
        await tc.close()


async def test_finetune_train_skips_feedback_with_no_extractable_frames(
    db: ClipDatabase, tmp_path: Path
) -> None:
    await _add_feedback_with_clip(db)
    analyzer = _moondream_train_analyzer(frames=[])
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.post("/api/ai/finetune/ft1/train", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["trained"] == 0
        assert data["message"] == "No usable clip frames for pending feedback"
    finally:
        await tc.close()


async def test_finetune_train_falls_back_to_original_suspicious_when_uncorrected(
    db: ClipDatabase, tmp_path: Path
) -> None:
    await _add_feedback_with_clip(db, corrected_suspicious=None)
    analyzer = _moondream_train_analyzer()
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.train_from_examples",
                new=AsyncMock(
                    return_value={
                        "steps_completed": 1,
                        "results": [{}],
                        "successful_indices": [0],
                    }
                ),
            ) as mock_train,
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post("/api/ai/finetune/ft1/train", json={})
        assert resp.status == 200
        examples = mock_train.call_args.args[1]
        ground_truth = json.loads(examples[0]["ground_truth"])
        # No explicit correction, so ground truth falls back to the
        # original (thumbs-up-confirmed) verdict, which was suspicious=True.
        assert ground_truth["suspicious"] is True
    finally:
        await tc.close()


async def test_finetune_train_manager_raises_returns_500(
    db: ClipDatabase, tmp_path: Path
) -> None:
    await _add_feedback_with_clip(db)
    analyzer = _moondream_train_analyzer()
    server = _make_finetune_server(db, tmp_path, analyzer=analyzer)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.train_from_examples",
                new=AsyncMock(side_effect=RuntimeError("api down")),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post("/api/ai/finetune/ft1/train", json={})
        assert resp.status == 500
    finally:
        await tc.close()


async def test_finetune_save_checkpoint_success(
    db: ClipDatabase, tmp_path: Path
) -> None:
    server = _make_finetune_server(db, tmp_path)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        with (
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.save_checkpoint",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "blink_downloader.analyzer.MoondreamFineTuneManager.close",
                new=AsyncMock(),
            ),
        ):
            resp = await tc.post("/api/ai/finetune/ft1/save-checkpoint")
        assert resp.status == 200
        data = await resp.json()
        assert data["saved"] is True
    finally:
        await tc.close()


async def test_finetune_save_checkpoint_not_configured(client: TestClient) -> None:
    resp = await client.post("/api/ai/finetune/ft1/save-checkpoint")
    assert resp.status == 400


async def test_feedback_untrained_count_zero(client: TestClient) -> None:
    resp = await client.get("/api/ai/feedback/untrained-count")
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] == 0


async def test_feedback_untrained_count_reflects_pending_rows(
    db: ClipDatabase, tmp_path: Path
) -> None:
    await _add_feedback_with_clip(db, clip_id="c1")
    await _add_feedback_with_clip(db, clip_id="c2")
    server = MediaServer(db=db, download_path=tmp_path, port=0)
    tc = TestClient(TestServer(server._build_app()))
    await tc.start_server()
    try:
        resp = await tc.get("/api/ai/feedback/untrained-count")
        data = await resp.json()
        assert data["count"] == 2
    finally:
        await tc.close()
