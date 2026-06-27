"""Tests for ClipAnalyzer and the multi-provider AI analysis system."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader.analyzer import (
    AnthropicAnalyzer,
    ClipAnalyzer,
    MoondreamCloudAnalyzer,
    MoondreamLocalAnalyzer,
    OllamaCloudAnalyzer,
    _ANTHROPIC_FALLBACK_MODELS,
    _vision_model_score,
    create_analyzer,
    is_moondream_installed,
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


# ------------------------------------------------------------------
# OllamaCloudAnalyzer
# ------------------------------------------------------------------


async def test_ollama_cloud_health_check_no_key() -> None:
    a = OllamaCloudAnalyzer(api_key="", model="llava:7b", prompt="test")
    assert await a.health_check() is False
    await a.close()


async def test_ollama_cloud_health_check_online() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = OllamaCloudAnalyzer(api_key="test-key", model="llava:7b", prompt="test")
    a._session = _mock_session(get=MagicMock(return_value=mock_resp))
    assert await a.health_check() is True


async def test_ollama_cloud_health_check_offline() -> None:
    import aiohttp

    a = OllamaCloudAnalyzer(api_key="test-key", model="llava:7b", prompt="test")
    a._session = _mock_session(
        get=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await a.health_check() is False


def test_ollama_cloud_provider_name() -> None:
    a = OllamaCloudAnalyzer(api_key="key", model="llava:7b", prompt="test")
    assert a.provider_name == "ollama_cloud"
    assert a.model_name() == "llava:7b"


async def test_ollama_cloud_session_has_auth_header() -> None:
    """The aiohttp session is created with the Authorization header."""
    a = OllamaCloudAnalyzer(api_key="my-secret-key", model="llava:7b", prompt="test")
    session = await a._get_session()
    assert "Authorization" in dict(session.headers)
    assert "my-secret-key" in dict(session.headers)["Authorization"]
    await a.close()


async def test_ollama_cloud_session_no_auth_header_when_no_key() -> None:
    a = OllamaCloudAnalyzer(api_key="", model="llava:7b", prompt="test")
    session = await a._get_session()
    assert "Authorization" not in dict(session.headers)
    await a.close()


async def test_ollama_cloud_call_model_success() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": "All clear"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = OllamaCloudAnalyzer(api_key="key", model="llava:7b", prompt="Analyze.")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))
    result = await a.call_ollama([_FAKE_JPEG], "Analyze")
    assert result == "All clear"
    # Verify it posted to the cloud URL
    call_args = a._session.post.call_args
    assert "api.ollama.com" in str(call_args)


async def test_ollama_cloud_uses_cloud_base_url() -> None:
    """OllamaCloudAnalyzer always targets api.ollama.com."""
    a = OllamaCloudAnalyzer(api_key="k", model="llava:7b", prompt="p")
    assert a._ollama_url == "https://api.ollama.com"
    await a.close()


async def test_ollama_cloud_fetch_models() -> None:
    models_data = {
        "models": [
            {"name": "llava:7b", "size": 4_000_000_000},
            {"name": "llama3:8b", "size": 5_000_000_000},
        ]
    }
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=models_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = OllamaCloudAnalyzer(api_key="key", model="llava:7b", prompt="test")
    a._session = _mock_session(get=MagicMock(return_value=mock_resp))
    models = await a.fetch_models()
    # Should filter to vision-only
    names = [m["name"] for m in models]
    assert "llava:7b" in names
    assert "llama3:8b" not in names


# ------------------------------------------------------------------
# create_analyzer factory
# ------------------------------------------------------------------


def test_create_analyzer_ollama() -> None:
    a = create_analyzer(
        "ollama", "prompt", ollama_url="http://localhost:11434", ollama_model="llava"
    )
    assert isinstance(a, ClipAnalyzer)


def test_create_analyzer_ollama_no_url() -> None:
    a = create_analyzer("ollama", "prompt")
    assert a is None


def test_create_analyzer_ollama_cloud() -> None:
    a = create_analyzer("ollama_cloud", "prompt", ollama_cloud_api_key="key123")
    assert isinstance(a, OllamaCloudAnalyzer)


def test_create_analyzer_ollama_cloud_no_key() -> None:
    a = create_analyzer("ollama_cloud", "prompt")
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


async def test_moondream_local_health_check_no_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """health_check returns False when the moondream package is missing."""
    import sys

    monkeypatch.delitem(sys.modules, "moondream", raising=False)

    a = MoondreamLocalAnalyzer(prompt="test")

    with patch.object(a, "_load_model_sync", side_effect=ImportError("no module")):
        result = await a.health_check()

    assert result is False


async def test_moondream_local_health_check_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


# ------------------------------------------------------------------
# _vision_model_score
# ------------------------------------------------------------------


def test_vision_model_score_llama32_vision_is_highest() -> None:
    score = _vision_model_score("llama3.2-vision:11b")
    assert score == 100


def test_vision_model_score_llava7b() -> None:
    score = _vision_model_score("llava:7b")
    assert score > 0
    # llava:7b should score higher than moondream
    assert score > _vision_model_score("moondream:latest")


def test_vision_model_score_unknown_model() -> None:
    score = _vision_model_score("some-future-vision-model:99b")
    assert score == 30  # default for unrecognized models


def test_vision_model_score_case_insensitive() -> None:
    assert _vision_model_score("LLaVA:7B") == _vision_model_score("llava:7b")


# ------------------------------------------------------------------
# Ollama fetch_models ranking
# ------------------------------------------------------------------


async def test_fetch_models_sorted_best_first(analyzer: ClipAnalyzer) -> None:
    models_data = {
        "models": [
            {"name": "moondream:latest", "size": 1_500_000_000},
            {"name": "llama3.2-vision:11b", "size": 8_000_000_000},
            {"name": "llava:7b", "size": 4_000_000_000},
        ]
    }
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=models_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(get=MagicMock(return_value=mock_resp))
    models = await analyzer.fetch_models()

    assert len(models) == 3
    # Best model should be first
    assert models[0]["name"] == "llama3.2-vision:11b"
    assert models[0]["score"] == 100
    # Worst should be last
    assert models[-1]["name"] == "moondream:latest"


async def test_fetch_models_score_field_present(analyzer: ClipAnalyzer) -> None:
    models_data = {
        "models": [
            {"name": "llava:7b", "size": 4_000_000_000},
        ]
    }
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=models_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(get=MagicMock(return_value=mock_resp))
    models = await analyzer.fetch_models()

    assert "score" in models[0]
    assert models[0]["score"] > 0


# ------------------------------------------------------------------
# is_moondream_installed
# ------------------------------------------------------------------


def test_is_moondream_installed_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    fake_md = MagicMock()
    monkeypatch.setitem(sys.modules, "moondream", fake_md)
    assert is_moondream_installed() is True


def test_is_moondream_installed_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.delitem(sys.modules, "moondream", raising=False)

    with patch(
        "builtins.__import__", side_effect=ImportError("no module named moondream")
    ):
        result = is_moondream_installed()
    assert result is False


# ------------------------------------------------------------------
# Token usage tracking
# ------------------------------------------------------------------


async def test_call_ollama_extracts_token_counts(analyzer: ClipAnalyzer) -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "response": "All clear",
            "prompt_eval_count": 128,
            "eval_count": 64,
        }
    )
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))
    await analyzer.call_ollama([_FAKE_JPEG], "Analyze")

    assert analyzer._last_prompt_tokens == 128
    assert analyzer._last_completion_tokens == 64


async def test_call_ollama_missing_token_counts(analyzer: ClipAnalyzer) -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": "All clear"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))
    await analyzer.call_ollama([_FAKE_JPEG], "Analyze")

    assert analyzer._last_prompt_tokens == 0
    assert analyzer._last_completion_tokens == 0


async def test_analyze_clip_includes_token_counts(analyzer: ClipAnalyzer) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    ollama_response = json.dumps(
        {"suspicious": False, "confidence": 0.1, "description": "Empty driveway"}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "response": ollama_response,
            "prompt_eval_count": 200,
            "eval_count": 50,
        }
    )
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.tokens_prompt == 200
    assert result.tokens_completion == 50
    assert result.to_dict()["tokens_prompt"] == 200
    assert result.to_dict()["tokens_completion"] == 50


# ------------------------------------------------------------------
# AnthropicAnalyzer
# ------------------------------------------------------------------


def _make_anthropic_response(
    text: str = '{"suspicious": false, "confidence": 0.1, "description": "Empty scene"}',
    input_tokens: int = 150,
    output_tokens: int = 45,
) -> MagicMock:
    """Return a mock Anthropic messages.create() response."""
    block = MagicMock()
    block.text = text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    resp = MagicMock()
    resp.content = [block]
    resp.usage = usage
    return resp


class _MockAPIStatusError(Exception):
    """Minimal stand-in for anthropic.APIStatusError with typed instance attrs."""

    def __init__(
        self, msg: str = "", status_code: int = 500, message: str = "err"
    ) -> None:
        super().__init__(msg)
        self.status_code = status_code
        self.message = message


def _make_anthropic_module(
    response: MagicMock | None = None,
    auth_error: bool = False,
    permission_error: bool = False,
    api_status_error: bool = False,
    models_data: list | None = None,
) -> MagicMock:
    """Return a mock anthropic module with AsyncAnthropic client."""
    mod = MagicMock()

    # Error classes
    mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
    mod.PermissionDeniedError = type("PermissionDeniedError", (Exception,), {})
    mod.APIStatusError = _MockAPIStatusError
    mod.RateLimitError = type("RateLimitError", (Exception,), {})
    mod.BadRequestError = type(
        "BadRequestError", (Exception,), {"message": "bad request"}
    )
    mod.APIConnectionError = type("APIConnectionError", (Exception,), {})

    # AsyncAnthropic client
    client = MagicMock()
    mod.AsyncAnthropic.return_value = client

    # messages.create
    if auth_error:
        client.messages.create = AsyncMock(
            side_effect=mod.AuthenticationError("bad key")
        )
    elif permission_error:
        client.messages.create = AsyncMock(
            side_effect=mod.PermissionDeniedError("no perm")
        )
    elif api_status_error:
        err = _MockAPIStatusError("api error", status_code=429, message="rate limited")
        client.messages.create = AsyncMock(side_effect=err)
    elif response is not None:
        client.messages.create = AsyncMock(return_value=response)

    # models.list
    if models_data is not None:
        page = MagicMock()
        page.data = models_data
        client.models.list = AsyncMock(return_value=page)
    else:
        client.models.list = AsyncMock(return_value=MagicMock(data=[]))

    # close
    client.close = AsyncMock()

    return mod


async def test_anthropic_provider_name() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    assert a.provider_name == "anthropic"
    assert a.model_name() == "claude-haiku-4-5"


async def test_anthropic_model_default() -> None:
    a = AnthropicAnalyzer(api_key="key", model="", prompt="test")
    assert a.model_name() == "claude-haiku-4-5"


def test_anthropic_model_pricing_haiku() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 1.00
    assert out == 5.00


def test_anthropic_model_pricing_opus() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-opus-4-8", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 5.00
    assert out == 25.00


def test_anthropic_model_pricing_sonnet() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-sonnet-4-6", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 3.00
    assert out == 15.00


def test_anthropic_model_pricing_unknown_falls_back_to_sonnet() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-future-99b", prompt="test")
    inp, out = a.model_pricing()
    # Unknown model falls back to Sonnet-level pricing
    assert inp == 3.00
    assert out == 15.00


async def test_anthropic_health_check_no_key() -> None:
    a = AnthropicAnalyzer(api_key="", model="claude-haiku-4-5", prompt="test")
    assert await a.health_check() is False


async def test_anthropic_health_check_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """health_check returns False when the anthropic package is missing."""
    import sys

    monkeypatch.delitem(sys.modules, "anthropic", raising=False)

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    with patch("builtins.__import__", side_effect=ImportError("no module anthropic")):
        result = await a.health_check()
    assert result is False


async def test_anthropic_health_check_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mock_mod = _make_anthropic_module()
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="valid-key", model="claude-haiku-4-5", prompt="test")
    # Reset any cached client so it picks up the mocked module
    a._client = None
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        # health_check calls _get_client which imports anthropic
        a._client = mock_mod.AsyncAnthropic.return_value
        result = await a.health_check()
    assert result is True


async def test_anthropic_health_check_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """health_check returns False and logs clearly for invalid API keys."""
    import sys

    mock_mod = _make_anthropic_module()
    # Make models.list raise AuthenticationError
    mock_mod.AsyncAnthropic.return_value.models.list = AsyncMock(
        side_effect=mock_mod.AuthenticationError("invalid key")
    )
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="bad-key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        result = await a.health_check()
    assert result is False


async def test_anthropic_call_model_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    resp = _make_anthropic_response(
        '{"suspicious": true, "confidence": 0.85, "description": "Intruder"}',
        input_tokens=300,
        output_tokens=60,
    )
    mock_mod = _make_anthropic_module(response=resp)
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="Analyze.")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        result = await a._call_model([_FAKE_JPEG, _FAKE_JPEG], "Analyze this scene.")

    assert "Intruder" in result or "suspicious" in result.lower()
    assert a._last_prompt_tokens == 300
    assert a._last_completion_tokens == 60


async def test_anthropic_call_model_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mock_mod = _make_anthropic_module(auth_error=True)
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="bad-key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "Analyze")

    assert result == ""


async def test_anthropic_call_model_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_anthropic_module(permission_error=True)
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="key", model="claude-opus-4-8", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "Analyze")

    assert result == ""


async def test_anthropic_call_model_api_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_anthropic_module(api_status_error=True)
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "Analyze")

    assert result == ""


async def test_anthropic_call_model_no_frames() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    result = await a._call_model([], "Analyze")
    assert result == ""


async def test_anthropic_call_model_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.messages.create = AsyncMock(
        side_effect=asyncio.TimeoutError
    )
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "Analyze")

    assert result == ""


async def test_anthropic_fetch_models_from_api(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    m1 = MagicMock()
    m1.id = "claude-haiku-4-5"
    m1.display_name = "Claude Haiku 4.5"

    m2 = MagicMock()
    m2.id = "claude-sonnet-4-6"
    m2.display_name = "Claude Sonnet 4.6"

    mock_mod = _make_anthropic_module(models_data=[m1, m2])
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        models = await a.fetch_models()

    assert len(models) == 2
    assert models[0]["name"] == "claude-haiku-4-5"
    assert "display_name" in models[0]


async def test_anthropic_fetch_models_fallback_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.models.list = AsyncMock(
        side_effect=mock_mod.AuthenticationError("bad key")
    )
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="bad-key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        models = await a.fetch_models()

    # Should fall back to the hardcoded list
    assert len(models) == len(_ANTHROPIC_FALLBACK_MODELS)
    assert any(m["name"] == "claude-haiku-4-5" for m in models)


async def test_anthropic_fetch_models_fallback_no_key() -> None:
    a = AnthropicAnalyzer(api_key="", model="claude-haiku-4-5", prompt="test")
    models = await a.fetch_models()
    assert len(models) == len(_ANTHROPIC_FALLBACK_MODELS)


async def test_anthropic_full_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full analyze_clip pipeline: ffmpeg → Anthropic API → AnalysisResult."""
    import sys

    resp = _make_anthropic_response(
        '{"suspicious": true, "confidence": 0.9, "description": "Suspicious person"}',
        input_tokens=400,
        output_tokens=80,
    )
    mock_mod = _make_anthropic_module(response=resp)
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="Analyze.")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await a.analyze_clip(
                "/clips/test.mp4", "clip-anthr-1", "Front Door"
            )

    assert result.clip_id == "clip-anthr-1"
    assert result.is_suspicious is True
    assert result.confidence == 0.9
    assert result.tokens_prompt == 400
    assert result.tokens_completion == 80
    assert result.frame_count == 1


async def test_anthropic_close() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    mock_client = AsyncMock()
    a._client = mock_client
    await a.close()
    mock_client.close.assert_called_once()
    assert a._client is None


async def test_anthropic_close_no_client() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    await a.close()  # Should not raise


def test_create_analyzer_anthropic() -> None:
    a = create_analyzer("anthropic", "prompt", anthropic_api_key="sk-ant-test")
    assert isinstance(a, AnthropicAnalyzer)
    assert a.model_name() == "claude-haiku-4-5"  # default


def test_create_analyzer_anthropic_with_model() -> None:
    a = create_analyzer(
        "anthropic",
        "prompt",
        anthropic_api_key="sk-ant-test",
        anthropic_model="claude-opus-4-8",
    )
    assert isinstance(a, AnthropicAnalyzer)
    assert a.model_name() == "claude-opus-4-8"


def test_create_analyzer_anthropic_no_key() -> None:
    a = create_analyzer("anthropic", "prompt")
    assert a is None


async def test_anthropic_tokens_reset_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token counts from a previous call must not bleed into the next."""
    import sys

    resp1 = _make_anthropic_response(input_tokens=500, output_tokens=100)
    resp2 = _make_anthropic_response(input_tokens=200, output_tokens=40)

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.messages.create = AsyncMock(
        side_effect=[resp1, resp2]
    )
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="Analyze.")
    a._client = mock_mod.AsyncAnthropic.return_value

    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            r1 = await a.analyze_clip("/clips/a.mp4", "c1", "Cam1")
            r2 = await a.analyze_clip("/clips/b.mp4", "c2", "Cam2")

    assert r1.tokens_prompt == 500
    assert r1.tokens_completion == 100
    assert r2.tokens_prompt == 200
    assert r2.tokens_completion == 40


async def test_moondream_cloud_tokens_are_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"answer": "All clear"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await a.analyze_clip("/clips/test.mp4", "c1", "Camera")

    assert result.tokens_prompt == 0
    assert result.tokens_completion == 0


# ------------------------------------------------------------------
# Additional coverage: _split_jpeg_frames edge cases
# ------------------------------------------------------------------


def test_split_jpeg_frames_no_eoi() -> None:
    """SOI found but no EOI — should return no frames."""
    data = b"\xff\xd8\xff\xe0"  # SOI + bytes but no EOI
    assert ClipAnalyzer._split_jpeg_frames(data) == []


# ------------------------------------------------------------------
# Additional coverage: parse_response / _try_parse_json
# ------------------------------------------------------------------


def test_parse_response_long_text_truncated(analyzer: ClipAnalyzer) -> None:
    """Responses longer than 200 chars without JSON/keywords get truncated with ellipsis."""
    response = "x" * 250
    _, _, summary = analyzer.parse_response(response)
    assert summary.endswith("…")


def test_try_parse_json_malformed_json() -> None:
    """Braces present but content is not valid JSON — returns empty tuple."""
    assert ClipAnalyzer._try_parse_json("{not: valid json!!!}") == (False, 0.0, "")


# ------------------------------------------------------------------
# Additional coverage: ClipAnalyzer internals
# ------------------------------------------------------------------


def test_clip_analyzer_provider_name(analyzer: ClipAnalyzer) -> None:
    assert analyzer.provider_name == "ollama"


async def test_clip_analyzer_get_session_creates_session(
    analyzer: ClipAnalyzer,
) -> None:
    """_get_session creates a real ClientSession when _session is None."""
    analyzer._session = None
    session = await analyzer._get_session()
    assert session is not None
    await session.close()


async def test_fetch_models_returns_empty_on_http_error(analyzer: ClipAnalyzer) -> None:
    """fetch_models returns [] when Ollama returns a non-200 status."""
    mock_resp = AsyncMock()
    mock_resp.status = 503
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(get=MagicMock(return_value=mock_resp))
    assert await analyzer.fetch_models() == []


# ------------------------------------------------------------------
# Additional coverage: MoondreamCloudAnalyzer
# ------------------------------------------------------------------


async def test_moondream_cloud_get_session_creates_session() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = None
    session = await a._get_session()
    assert session is not None
    await session.close()


async def test_moondream_cloud_close_open_session() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    a._session = mock_session
    await a.close()
    mock_session.close.assert_called_once()


async def test_moondream_cloud_call_api_frame_401() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 401
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))
    assert await a._call_api_frame(_FAKE_JPEG, "prompt") == ""


async def test_moondream_cloud_call_api_frame_500() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))
    assert await a._call_api_frame(_FAKE_JPEG, "prompt") == ""


async def test_moondream_cloud_call_api_frame_timeout() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = _mock_session(post=MagicMock(side_effect=asyncio.TimeoutError))
    assert await a._call_api_frame(_FAKE_JPEG, "prompt") == ""


async def test_moondream_cloud_call_api_frame_client_error() -> None:
    import aiohttp

    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await a._call_api_frame(_FAKE_JPEG, "prompt") == ""


async def test_moondream_cloud_call_model_empty_frames() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    assert await a._call_model([], "prompt") == ""


# ------------------------------------------------------------------
# Additional coverage: MoondreamLocalAnalyzer
# ------------------------------------------------------------------


async def test_moondream_local_ensure_model_generic_exception() -> None:
    """Non-ImportError during model load is caught and returns False."""
    a = MoondreamLocalAnalyzer(prompt="test")
    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            side_effect=RuntimeError("load failed")
        )
        assert await a._ensure_model() is False


async def test_moondream_local_call_model_no_frames_after_ready() -> None:
    a = MoondreamLocalAnalyzer(prompt="test")
    a._model_ready = True
    assert await a._call_model([], "prompt") == ""


async def test_moondream_local_call_model_inference_exception() -> None:
    a = MoondreamLocalAnalyzer(prompt="test")
    a._model_ready = True
    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            side_effect=RuntimeError("CUDA error")
        )
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


# ------------------------------------------------------------------
# Additional coverage: AnthropicAnalyzer internals
# ------------------------------------------------------------------


def test_anthropic_get_client_creates_client() -> None:
    """_get_client instantiates AsyncAnthropic when _client is None."""
    import sys

    mock_mod = _make_anthropic_module()
    a = AnthropicAnalyzer(
        api_key="sk-ant-test", model="claude-haiku-4-5", prompt="test"
    )
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        client = a._get_client()
    assert client is not None
    mock_mod.AsyncAnthropic.assert_called_once_with(api_key="sk-ant-test")


async def test_anthropic_health_check_permission_denied() -> None:
    import sys

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.models.list = AsyncMock(
        side_effect=mock_mod.PermissionDeniedError("no permission")
    )
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        assert await a.health_check() is False


async def test_anthropic_health_check_generic_exception() -> None:
    import sys

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.models.list = AsyncMock(
        side_effect=RuntimeError("connection refused")
    )
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        assert await a.health_check() is False


async def test_anthropic_fetch_models_import_error() -> None:
    """fetch_models falls back to hardcoded list when anthropic is not importable."""
    import sys

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    with patch.dict(sys.modules, {"anthropic": None}):
        result = await a.fetch_models()
    assert len(result) == len(_ANTHROPIC_FALLBACK_MODELS)


async def test_anthropic_fetch_models_generic_exception() -> None:
    """fetch_models falls back to hardcoded list on unexpected API errors."""
    import sys

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.models.list = AsyncMock(
        side_effect=RuntimeError("API unavailable")
    )
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        result = await a.fetch_models()
    assert len(result) == len(_ANTHROPIC_FALLBACK_MODELS)


def test_anthropic_resize_frame_resizes_large_image() -> None:
    """Frames wider/taller than max_dimension are resized down."""
    import io

    from PIL import Image

    img = Image.new("RGB", (2000, 1000), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    result = AnthropicAnalyzer._resize_frame(buf.getvalue(), max_dimension=1568)
    resized = Image.open(io.BytesIO(result))
    assert max(resized.width, resized.height) <= 1568


def test_anthropic_resize_frame_skips_small_image() -> None:
    """Frames within max_dimension are returned byte-for-byte unchanged."""
    import io

    from PIL import Image

    img = Image.new("RGB", (640, 480), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    original = buf.getvalue()
    assert AnthropicAnalyzer._resize_frame(original) == original


async def test_anthropic_call_model_import_error() -> None:
    """_call_model returns '' when the anthropic package is not importable."""
    import sys

    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    with patch.dict(sys.modules, {"anthropic": None}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


async def test_anthropic_call_model_rate_limit_error() -> None:
    import sys

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.messages.create = AsyncMock(
        side_effect=mock_mod.RateLimitError("rate limited")
    )
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


async def test_anthropic_call_model_bad_request_error() -> None:
    import sys

    mock_mod = _make_anthropic_module()
    bad_req = mock_mod.BadRequestError("model has no vision support")
    bad_req.message = "model has no vision support"
    mock_mod.AsyncAnthropic.return_value.messages.create = AsyncMock(
        side_effect=bad_req
    )
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


async def test_anthropic_call_model_api_connection_error() -> None:
    import sys

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.messages.create = AsyncMock(
        side_effect=mock_mod.APIConnectionError("connection refused")
    )
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


async def test_anthropic_call_model_generic_exception() -> None:
    import sys

    mock_mod = _make_anthropic_module()
    mock_mod.AsyncAnthropic.return_value.messages.create = AsyncMock(
        side_effect=ValueError("unexpected error")
    )
    a = AnthropicAnalyzer(api_key="key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""
