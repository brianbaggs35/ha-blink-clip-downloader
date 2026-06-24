"""Tests for ClipAnalyzer and the multi-provider AI analysis system."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader.analyzer import (
    ClipAnalyzer,
    MoondreamCloudAnalyzer,
    MoondreamLocalAnalyzer,
    create_analyzer,
    is_vision_model,
)


def _mock_session(**overrides: object) -> MagicMock:
    """Create a mock aiohttp.ClientSession with closed=False."""
    s = MagicMock()
    s.closed = False
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def analyzer() -> ClipAnalyzer:
    return ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava:7b",
        prompt="Analyze this frame.",
        car_description="",
        max_frames=3,
        frame_interval=2.0,
        suspicious_keywords=["suspicious", "intruder", "theft"],
    )


# ------------------------------------------------------------------
# Frame extraction
# ------------------------------------------------------------------

# Minimal valid JPEG: SOI + EOI markers
_FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\xff\xd9"
_TWO_JPEGS = _FAKE_JPEG + _FAKE_JPEG


async def test_extract_frames_calls_ffmpeg(analyzer: ClipAnalyzer) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_TWO_JPEGS, b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        frames = await analyzer.extract_frames("/clips/test.mp4")

    mock_exec.assert_called_once()
    args = mock_exec.call_args[0]
    assert args[0] == "ffmpeg"
    assert "/clips/test.mp4" in args
    assert len(frames) == 2


async def test_extract_frames_ffmpeg_failure(analyzer: ClipAnalyzer) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        frames = await analyzer.extract_frames("/clips/test.mp4")

    assert frames == []


async def test_extract_frames_ffmpeg_timeout(analyzer: ClipAnalyzer) -> None:
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=asyncio.TimeoutError,
    ):
        frames = await analyzer.extract_frames("/clips/test.mp4")

    assert frames == []


async def test_extract_frames_ffmpeg_not_found(analyzer: ClipAnalyzer) -> None:
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=OSError("ffmpeg not found"),
    ):
        frames = await analyzer.extract_frames("/clips/test.mp4")

    assert frames == []


# ------------------------------------------------------------------
# split_jpeg_frames
# ------------------------------------------------------------------


def test_split_jpeg_frames_single() -> None:
    frames = ClipAnalyzer._split_jpeg_frames(_FAKE_JPEG)
    assert len(frames) == 1


def test_split_jpeg_frames_multiple() -> None:
    frames = ClipAnalyzer._split_jpeg_frames(_TWO_JPEGS)
    assert len(frames) == 2


def test_split_jpeg_frames_empty() -> None:
    assert ClipAnalyzer._split_jpeg_frames(b"") == []


def test_split_jpeg_frames_garbage() -> None:
    assert ClipAnalyzer._split_jpeg_frames(b"\x00\x01\x02") == []


# ------------------------------------------------------------------
# Ollama API
# ------------------------------------------------------------------


async def test_call_ollama_success(analyzer: ClipAnalyzer) -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": "Person at door"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = _mock_session(post=MagicMock(return_value=mock_resp))
    analyzer._session = session

    result = await analyzer.call_ollama([_FAKE_JPEG], "Analyze")
    assert result == "Person at door"

    call_kwargs = session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert payload["model"] == "llava:7b"
    assert payload["stream"] is False
    assert len(payload["images"]) == 1


async def test_call_ollama_timeout(analyzer: ClipAnalyzer) -> None:
    analyzer._session = _mock_session(post=MagicMock(side_effect=asyncio.TimeoutError))

    result = await analyzer.call_ollama([_FAKE_JPEG], "Analyze")
    assert result == ""


async def test_call_ollama_connection_error(analyzer: ClipAnalyzer) -> None:
    import aiohttp

    analyzer._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )

    result = await analyzer.call_ollama([_FAKE_JPEG], "Analyze")
    assert result == ""


async def test_call_ollama_http_error(analyzer: ClipAnalyzer) -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    result = await analyzer.call_ollama([_FAKE_JPEG], "Analyze")
    assert result == ""


# ------------------------------------------------------------------
# Response parsing
# ------------------------------------------------------------------


def test_parse_response_json_suspicious(analyzer: ClipAnalyzer) -> None:
    response = json.dumps(
        {"suspicious": True, "confidence": 0.85, "description": "Person near car"}
    )
    is_suspicious, confidence, summary = analyzer.parse_response(response)
    assert is_suspicious is True
    assert confidence == 0.85
    assert summary == "Person near car"


def test_parse_response_json_clean(analyzer: ClipAnalyzer) -> None:
    response = json.dumps(
        {"suspicious": False, "confidence": 0.1, "description": "Empty driveway"}
    )
    is_suspicious, confidence, summary = analyzer.parse_response(response)
    assert is_suspicious is False
    assert confidence == 0.1
    assert summary == "Empty driveway"


def test_parse_response_json_embedded_in_text(analyzer: ClipAnalyzer) -> None:
    response = (
        "Here is my analysis:\n"
        '{"suspicious": true, "confidence": 0.7, "description": "Unknown person"}\n'
        "That is my conclusion."
    )
    is_suspicious, confidence, summary = analyzer.parse_response(response)
    assert is_suspicious is True
    assert confidence == 0.7


def test_parse_response_keyword_fallback(analyzer: ClipAnalyzer) -> None:
    response = "I see a suspicious person lurking near the vehicle. Possible intruder."
    is_suspicious, confidence, summary = analyzer.parse_response(response)
    assert is_suspicious is True
    assert confidence > 0


def test_parse_response_no_keywords(analyzer: ClipAnalyzer) -> None:
    response = "The driveway is empty. A car is parked. Nothing unusual."
    is_suspicious, confidence, summary = analyzer.parse_response(response)
    assert is_suspicious is False


def test_parse_response_empty(analyzer: ClipAnalyzer) -> None:
    is_suspicious, confidence, summary = analyzer.parse_response("")
    assert is_suspicious is False
    assert confidence == 0.0
    assert summary == ""


def test_parse_response_confidence_clamped(analyzer: ClipAnalyzer) -> None:
    response = json.dumps(
        {"suspicious": True, "confidence": 5.0, "description": "test"}
    )
    _, confidence, _ = analyzer.parse_response(response)
    assert confidence == 1.0


# ------------------------------------------------------------------
# Prompt building
# ------------------------------------------------------------------


def test_build_prompt_no_car(analyzer: ClipAnalyzer) -> None:
    prompt = analyzer._build_prompt("Front Door")
    assert "Front Door" in prompt
    assert "homeowner's car" not in prompt


def test_build_prompt_with_car() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Silver Honda Civic",
    )
    prompt = a._build_prompt("Driveway")
    assert "Silver Honda Civic" in prompt
    assert "Driveway" in prompt


# ------------------------------------------------------------------
# Health check / fetch models
# ------------------------------------------------------------------


async def test_health_check_online(analyzer: ClipAnalyzer) -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(get=MagicMock(return_value=mock_resp))
    assert await analyzer.health_check() is True


async def test_health_check_offline(analyzer: ClipAnalyzer) -> None:
    import aiohttp

    analyzer._session = _mock_session(
        get=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await analyzer.health_check() is False


async def test_fetch_models_success(analyzer: ClipAnalyzer) -> None:
    models_data = {
        "models": [
            {"name": "llava:7b", "size": 4_000_000_000},
            {"name": "moondream:latest", "size": 1_500_000_000},
        ]
    }
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=models_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(get=MagicMock(return_value=mock_resp))
    models = await analyzer.fetch_models()
    assert len(models) == 2
    assert models[0]["name"] == "llava:7b"


async def test_fetch_models_offline(analyzer: ClipAnalyzer) -> None:
    import aiohttp

    analyzer._session = _mock_session(
        get=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    models = await analyzer.fetch_models()
    assert models == []


# ------------------------------------------------------------------
# Full pipeline
# ------------------------------------------------------------------


async def test_analyze_clip_full_pipeline(analyzer: ClipAnalyzer) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    ollama_response = json.dumps(
        {"suspicious": True, "confidence": 0.9, "description": "Intruder detected"}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": ollama_response})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip("/clips/test.mp4", "clip1", "Front Door")

    assert result.clip_id == "clip1"
    assert result.camera == "Front Door"
    assert result.is_suspicious is True
    assert result.confidence == 0.9
    assert result.frame_count == 1
    assert result.analysis_duration > 0


async def test_analyze_clip_no_frames(analyzer: ClipAnalyzer) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip("/clips/bad.mp4", "clip2", "Backyard")

    assert result.is_suspicious is False
    assert result.frame_count == 0
    assert "No frames" in result.summary


# ------------------------------------------------------------------
# is_vision_model
# ------------------------------------------------------------------


def test_is_vision_model_known_vision() -> None:
    assert is_vision_model("llava:7b") is True
    assert is_vision_model("LLaVA-Phi3:latest") is True
    assert is_vision_model("moondream2:latest") is True
    assert is_vision_model("bakllava:13b") is True
    assert is_vision_model("minicpm-v:latest") is True
    assert is_vision_model("llama3.2-vision:11b") is True


def test_is_vision_model_not_vision() -> None:
    assert is_vision_model("llama3:8b") is False
    assert is_vision_model("gemma:2b") is False
    assert is_vision_model("mistral:latest") is False
    assert is_vision_model("codellama:7b") is False


# ------------------------------------------------------------------
# fetch_models vision filter
# ------------------------------------------------------------------


async def test_fetch_models_filters_non_vision(analyzer: ClipAnalyzer) -> None:
    models_data = {
        "models": [
            {"name": "llava:7b", "size": 4_000_000_000},
            {"name": "llama3:8b", "size": 6_000_000_000},
            {"name": "moondream:latest", "size": 1_500_000_000},
            {"name": "gemma:2b", "size": 2_000_000_000},
        ]
    }
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=models_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(get=MagicMock(return_value=mock_resp))
    models = await analyzer.fetch_models()
    names = [m["name"] for m in models]
    assert "llava:7b" in names
    assert "moondream:latest" in names
    assert "llama3:8b" not in names
    assert "gemma:2b" not in names


# ------------------------------------------------------------------
# create_analyzer factory
# ------------------------------------------------------------------


def test_create_analyzer_ollama() -> None:
    a = create_analyzer("ollama", "prompt", ollama_url="http://localhost:11434", ollama_model="llava")
    assert isinstance(a, ClipAnalyzer)


def test_create_analyzer_ollama_no_url() -> None:
    a = create_analyzer("ollama", "prompt")
    assert a is None


def test_create_analyzer_moondream_cloud() -> None:
    a = create_analyzer("moondream_cloud", "prompt", moondream_api_key="key123")
    assert isinstance(a, MoondreamCloudAnalyzer)


def test_create_analyzer_moondream_cloud_no_key() -> None:
    a = create_analyzer("moondream_cloud", "prompt")
    assert a is None


def test_create_analyzer_moondream_local() -> None:
    a = create_analyzer("moondream_local", "prompt")
    assert isinstance(a, MoondreamLocalAnalyzer)


def test_create_analyzer_unknown_provider() -> None:
    a = create_analyzer("unknown_ai", "prompt")
    assert a is None


# ------------------------------------------------------------------
# MoondreamCloudAnalyzer
# ------------------------------------------------------------------


async def test_moondream_cloud_health_check_no_key() -> None:
    a = MoondreamCloudAnalyzer(api_key="", prompt="test")
    assert await a.health_check() is False
    await a.close()


async def test_moondream_cloud_health_check_online() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = MoondreamCloudAnalyzer(api_key="test-key", prompt="test")
    a._session = _mock_session(get=MagicMock(return_value=mock_resp))
    assert await a.health_check() is True


async def test_moondream_cloud_health_check_offline() -> None:
    import aiohttp

    a = MoondreamCloudAnalyzer(api_key="test-key", prompt="test")
    a._session = _mock_session(
        get=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await a.health_check() is False


async def test_moondream_cloud_call_model_success() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"answer": "No suspicious activity"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = MoondreamCloudAnalyzer(api_key="key", prompt="Analyze this frame.")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))

    result = await a._call_model([_FAKE_JPEG, _FAKE_JPEG, _FAKE_JPEG], "Analyze.")
    assert "No suspicious activity" in result

    # Should have posted to the Cloud API
    call_kwargs = a._session.post.call_args
    assert "moondream.ai" in str(call_kwargs)


async def test_moondream_cloud_call_model_rate_limit() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 429
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))
    result = await a._call_model([_FAKE_JPEG], "test")
    assert result == ""


async def test_moondream_cloud_fetch_models() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    models = await a.fetch_models()
    assert len(models) == 1
    assert models[0]["name"] == "moondream-cloud"


def test_moondream_cloud_provider_name() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    assert a.provider_name == "moondream_cloud"
    assert a.model_name() == "moondream-cloud"


# ------------------------------------------------------------------
# MoondreamLocalAnalyzer
# ------------------------------------------------------------------


def test_moondream_local_provider_name() -> None:
    a = MoondreamLocalAnalyzer(prompt="test")
    assert a.provider_name == "moondream_local"
    assert a.model_name() == "moondream-0_5b-int8"


async def test_moondream_local_fetch_models() -> None:
    a = MoondreamLocalAnalyzer(prompt="test")
    models = await a.fetch_models()
    assert len(models) == 1
    assert "0_5b" in models[0]["name"]


async def test_moondream_local_health_check_no_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """health_check returns False when the moondream package is missing."""
    import sys

    monkeypatch.delitem(sys.modules, "moondream", raising=False)

    a = MoondreamLocalAnalyzer(prompt="test")

    with patch.object(a, "_load_model_sync", side_effect=ImportError("no module")):
        result = await a.health_check()

    assert result is False


async def test_moondream_local_health_check_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """health_check returns True once the model is loaded."""
    mock_md = MagicMock()
    mock_md.vl.return_value = MagicMock()

    import sys

    monkeypatch.setitem(sys.modules, "moondream", mock_md)

    a = MoondreamLocalAnalyzer(prompt="test")
    result = await a.health_check()

    assert result is True
    assert a._model_ready is True


async def test_moondream_local_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """_call_model runs inference in a thread executor."""
    mock_model = MagicMock()
    mock_model.query.return_value = {"answer": "A car is parked"}

    a = MoondreamLocalAnalyzer(prompt="Analyze.")
    a._md_model = mock_model
    a._model_ready = True

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = await a._call_model([_FAKE_JPEG, _FAKE_JPEG], "Analyze this scene.")

    assert "car" in result.lower()


async def test_moondream_local_call_model_no_frames() -> None:
    a = MoondreamLocalAnalyzer(prompt="test")
    a._model_ready = True
    result = await a._call_model([], "prompt")
    assert result == ""


async def test_moondream_local_close_resets_state() -> None:
    a = MoondreamLocalAnalyzer(prompt="test")
    a._model_ready = True
    a._md_model = MagicMock()
    await a.close()
    assert a._model_ready is False
    assert a._md_model is None
