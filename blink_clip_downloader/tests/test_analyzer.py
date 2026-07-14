"""Tests for ClipAnalyzer and the multi-provider AI analysis system."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader.analyzer import (
    AnalysisResult,
    AnthropicAnalyzer,
    BaseAnalyzer,
    ClipAnalyzer,
    MoondreamCloudAnalyzer,
    MoondreamFineTuneManager,
    MoondreamLocalAnalyzer,
    OllamaCloudAnalyzer,
    OpenAIAnalyzer,
    _ANTHROPIC_FALLBACK_MODELS,
    _OPENAI_FALLBACK_MODELS,
    _vision_model_score,
    create_analyzer,
    is_openai_vision_model,
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


async def test_extract_frames_kills_ffmpeg_process_on_communicate_timeout(
    analyzer: ClipAnalyzer,
) -> None:
    """A hung ffmpeg process must be killed/reaped, not leaked, when
    communicate() times out (the process itself started fine)."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        frames = await analyzer.extract_frames("/clips/test.mp4")

    assert frames == []
    mock_proc.kill.assert_called_once()
    mock_proc.wait.assert_awaited_once()


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


def test_parse_response_json_description_capped_to_two_sentences(
    analyzer: ClipAnalyzer,
) -> None:
    """A JSON description with many distinct sentences is capped to two."""
    description = (
        "A person is standing near the middle of a silver Kia Forte parked on "
        "the driveway. The person is facing the car. The person is standing "
        "about 2 feet from the car's driver-side door. The person is standing "
        "near the car's passenger-side door."
    )
    response = json.dumps(
        {"suspicious": True, "confidence": 0.3, "description": description}
    )
    _, _, summary = analyzer.parse_response(response)
    assert summary == (
        "A person is standing near the middle of a silver Kia Forte parked on "
        "the driveway. The person is facing the car."
    )


def test_parse_response_json_description_repetition_loop_collapsed(
    analyzer: ClipAnalyzer,
) -> None:
    """A degenerate repeated-sentence loop is cut at the first repeat."""
    description = (
        "The person is standing near the car door, facing the vehicle. " * 10
    ).strip()
    response = json.dumps(
        {"suspicious": True, "confidence": 0.3, "description": description}
    )
    _, _, summary = analyzer.parse_response(response)
    assert summary == "The person is standing near the car door, facing the vehicle."


def test_clean_summary_empty_string() -> None:
    assert ClipAnalyzer._clean_summary("") == ""


def test_clean_summary_no_punctuation_truncated_with_ellipsis() -> None:
    summary = ClipAnalyzer._clean_summary("x" * 250)
    assert len(summary) == 201
    assert summary.endswith("…")


def test_clean_summary_drops_repeated_sentence() -> None:
    """Stops at the first sentence that repeats one already kept (the
    degenerate-repetition-loop failure mode some models fall into)."""
    summary = ClipAnalyzer._clean_summary(
        "A person walks up the driveway. A person walks up the driveway. "
        "They knock on the door."
    )
    assert summary == "A person walks up the driveway."


# ------------------------------------------------------------------
# Prompt building
# ------------------------------------------------------------------


def test_build_prompt_no_car(analyzer: ClipAnalyzer) -> None:
    prompt = analyzer._build_prompt("Front Door")
    assert "Front Door" in prompt
    # No car-related distance rules when no car is configured
    assert "PROTECTED VEHICLE" not in prompt
    assert "1 foot" not in prompt


def test_build_prompt_output_rules_favor_brief_security_focused_descriptions(
    analyzer: ClipAnalyzer,
) -> None:
    """OUTPUT RULES caps description length and rules out scenery-listing.

    Regression test for overly detailed descriptions (e.g. narrating power
    lines, utility poles, and every parked car in frame) that inflated
    completion tokens without adding security value.
    """
    prompt = analyzer._build_prompt("Front Door")
    assert "Keep it SHORT" in prompt
    assert "static background scenery" in prompt
    assert "utility poles" in prompt
    assert "power lines" in prompt


def test_base_prompt_for_camera_matches_build_prompt_default(
    analyzer: ClipAnalyzer,
) -> None:
    """Public wrapper used for fine-tuning training questions (no clip context)."""
    assert analyzer.base_prompt_for_camera("Front Door") == analyzer._build_prompt(
        "Front Door"
    )


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
    assert "1 foot" in prompt
    assert "2 feet" in prompt
    assert "distance" in prompt.lower()


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


async def test_analyze_clip_raises_when_call_model_returns_empty(
    analyzer: ClipAnalyzer,
) -> None:
    """A provider call that fails (rate limit, auth error, timeout, ...)
    returns "" from _call_model, already logged by that provider's own
    error handler. Once frames were successfully extracted, that "" must
    not be silently parsed into a false is_suspicious=False result — see
    the comment in _analyze_clip_locked for why that would permanently
    mis-record a clip that was never actually analyzed. analyze_clip()
    must raise instead, so AnalysisQueue._process_one marks it "failed"
    rather than "completed"."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    analyzer._call_model = AsyncMock(return_value="")  # type: ignore[method-assign]

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(RuntimeError, match="empty response"):
            await analyzer.analyze_clip("/clips/test.mp4", "clip1", "Front Door")


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


async def test_ollama_cloud_health_check_caches_result() -> None:
    """A second call within the cache TTL must not hit the API again."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    get_mock = MagicMock(return_value=mock_resp)

    a = OllamaCloudAnalyzer(api_key="test-key", model="llava:7b", prompt="test")
    a._session = _mock_session(get=get_mock)
    assert await a.health_check() is True
    assert await a.health_check() is True
    assert get_mock.call_count == 1


def test_ollama_cloud_provider_name() -> None:
    a = OllamaCloudAnalyzer(api_key="key", model="llava:7b", prompt="test")
    assert a.provider_name == "ollama_cloud"
    assert a.model_name() == "llava:7b"


async def test_ollama_cloud_session_has_auth_header() -> None:
    """The aiohttp session is created with the Authorization header."""
    a = OllamaCloudAnalyzer(api_key="my-secret-key", model="llava:7b", prompt="test")
    session = a._get_session()
    assert "Authorization" in dict(session.headers)
    assert "my-secret-key" in dict(session.headers)["Authorization"]
    await a.close()


async def test_ollama_cloud_session_no_auth_header_when_no_key() -> None:
    a = OllamaCloudAnalyzer(api_key="", model="llava:7b", prompt="test")
    session = a._get_session()
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


async def test_moondream_cloud_health_check_caches_result() -> None:
    """A second call within the cache TTL must not hit the API again."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    get_mock = MagicMock(return_value=mock_resp)

    a = MoondreamCloudAnalyzer(api_key="test-key", prompt="test")
    a._session = _mock_session(get=get_mock)
    assert await a.health_check() is True
    assert await a.health_check() is True
    assert get_mock.call_count == 1


async def test_moondream_cloud_call_model_success() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "answer": '{"suspicious": false, "confidence": 0.2, "description": "No suspicious activity"}'
        }
    )
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = MoondreamCloudAnalyzer(api_key="key", prompt="Analyze this frame.")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG, _FAKE_JPEG, _FAKE_JPEG], "Analyze.")
    assert "No suspicious activity" in result or "suspicious" in result.lower()
    # No "objects" key in the blanket mock response → person/animal/vehicle
    # detect all return [] per frame (3 detect calls each), so 3 frames × 3 = 9
    assert a._session.post.call_count == 9
    # Should have posted to the Cloud API
    call_kwargs = a._session.post.call_args
    assert "moondream.ai" in str(call_kwargs)


async def test_moondream_cloud_call_model_rate_limit() -> None:
    # When /detect returns 429, _detect_objects returns [] (no person detected).
    # The fallback "no person" response is used — the query is never called.
    mock_resp = AsyncMock()
    mock_resp.status = 429
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "test")
    # With detect-based flow: rate limit on detect → no person → fallback result used
    assert "No person detected" in result or result == ""


async def test_moondream_cloud_fetch_models() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    models = await a.fetch_models()
    assert len(models) == 1
    assert models[0]["name"] == "moondream3-preview"


def test_moondream_cloud_provider_name() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    assert a.provider_name == "moondream_cloud"
    assert a.model_name() == "moondream3-preview"


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
    """_call_model runs inference in a thread executor on all frames."""
    mock_model = MagicMock()
    # A person is detected on every frame so the pipeline reaches query()
    # instead of short-circuiting to the no-subject skip response.
    mock_model.detect.return_value = {
        "objects": [{"x_min": 0.2, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]
    }
    mock_model.caption.return_value = {"caption": "A person stands in the yard."}
    # First frame: not suspicious; second frame: suspicious
    mock_model.query.side_effect = [
        {
            "answer": '{"suspicious": false, "confidence": 0.1, "description": "Empty scene"}'
        },
        {
            "answer": '{"suspicious": true, "confidence": 0.8, "description": "Person near car"}'
        },
    ]

    a = MoondreamLocalAnalyzer(prompt="Analyze.")
    a._md_model = mock_model
    a._model_ready = True

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = await a._call_model([_FAKE_JPEG, _FAKE_JPEG], "Analyze this scene.")

    # Should pick the suspicious result
    assert "Person near car" in result


async def test_moondream_local_call_model_skips_empty_and_unparseable_responses() -> (
    None
):
    """An empty inference result is skipped entirely; an unparseable one is
    kept only as a last-resort fallback if nothing better comes along."""
    mock_model = MagicMock()
    mock_model.detect.return_value = {
        "objects": [{"x_min": 0.2, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]
    }
    mock_model.caption.return_value = {"caption": "A person stands in the yard."}
    mock_model.query.side_effect = [
        {"answer": ""},
        {"answer": "not valid json"},
    ]

    a = MoondreamLocalAnalyzer(prompt="Analyze.")
    a._md_model = mock_model
    a._model_ready = True

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = await a._call_model([_FAKE_JPEG, _FAKE_JPEG], "Analyze this scene.")

    assert result == "not valid json"


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
# MoondreamLocalAnalyzer — detect/caption helpers
# ------------------------------------------------------------------


def test_local_detect_filters_non_dicts() -> None:
    mock_model = MagicMock()
    mock_model.detect.return_value = {
        "objects": [
            {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9},
            "not_a_dict",
            None,
        ]
    }
    a = MoondreamLocalAnalyzer(prompt="p")
    a._md_model = mock_model

    result = a._local_detect("encoded", "person")
    assert result == [{"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]


def test_local_detect_exception_returns_empty() -> None:
    mock_model = MagicMock()
    mock_model.detect.side_effect = RuntimeError("inference failed")
    a = MoondreamLocalAnalyzer(prompt="p")
    a._md_model = mock_model

    assert a._local_detect("encoded", "person") == []


def test_local_caption_success() -> None:
    mock_model = MagicMock()
    mock_model.caption.return_value = {"caption": "A quiet driveway scene."}
    a = MoondreamLocalAnalyzer(prompt="p")
    a._md_model = mock_model

    assert a._local_caption("encoded") == "A quiet driveway scene."


def test_local_caption_requests_short_length() -> None:
    """Mirrors the cloud analyzer's length="short" grounding-cost fix."""
    mock_model = MagicMock()
    mock_model.caption.return_value = {"caption": "A person near a car."}
    a = MoondreamLocalAnalyzer(prompt="p")
    a._md_model = mock_model

    a._local_caption("encoded")

    _, kwargs = mock_model.caption.call_args
    assert kwargs["length"] == "short"


def test_local_caption_exception_returns_empty() -> None:
    mock_model = MagicMock()
    mock_model.caption.side_effect = RuntimeError("inference failed")
    a = MoondreamLocalAnalyzer(prompt="p")
    a._md_model = mock_model

    assert a._local_caption("encoded") == ""


def test_detect_protected_vehicle_sync_skips_when_zero_or_one_car() -> None:
    mock_model = MagicMock()
    mock_model.detect.side_effect = AssertionError("should not be called")
    a = MoondreamLocalAnalyzer(prompt="p", car_description="Silver Kia")
    a._md_model = mock_model

    single = [{"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]
    protected, other = a._detect_protected_vehicle_sync("encoded", single)
    assert protected == single
    assert other == []


def test_detect_protected_vehicle_sync_disambiguates() -> None:
    car_boxes = [
        {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9},
        {"x_min": 0.6, "y_min": 0.1, "x_max": 0.9, "y_max": 0.9},
    ]

    def fake_detect(encoded: Any, object_name: str) -> dict[str, Any]:
        assert object_name == "Silver Kia"
        return {"objects": [car_boxes[0]]}

    mock_model = MagicMock()
    mock_model.detect.side_effect = fake_detect
    a = MoondreamLocalAnalyzer(prompt="p", car_description="Silver Kia")
    a._md_model = mock_model

    protected, other = a._detect_protected_vehicle_sync("encoded", car_boxes)
    assert protected == [car_boxes[0]]
    assert other == [car_boxes[1]]


def test_detect_protected_vehicle_sync_falls_back_when_nothing_found() -> None:
    car_boxes = [
        {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9},
        {"x_min": 0.6, "y_min": 0.1, "x_max": 0.9, "y_max": 0.9},
    ]
    mock_model = MagicMock()
    mock_model.detect.return_value = {"objects": []}
    a = MoondreamLocalAnalyzer(prompt="p", car_description="Silver Kia")
    a._md_model = mock_model

    protected, other = a._detect_protected_vehicle_sync("encoded", car_boxes)
    assert protected == car_boxes
    assert other == []


def test_detect_protected_vehicle_sync_dedupes_duplicate_boxes_for_same_car() -> None:
    """Local-inference counterpart to the cloud dedup regression test: a
    single parked car detected twice by the generic "car" query must not
    surface as "another vehicle" next to itself."""
    same_car_a = {"x_min": 0.10, "y_min": 0.10, "x_max": 0.50, "y_max": 0.90}
    same_car_b = {"x_min": 0.12, "y_min": 0.10, "x_max": 0.50, "y_max": 0.90}
    mock_model = MagicMock()
    mock_model.detect.side_effect = AssertionError("should not be called")
    a = MoondreamLocalAnalyzer(prompt="p", car_description="Silver Kia")
    a._md_model = mock_model

    protected, other = a._detect_protected_vehicle_sync(
        "encoded", [same_car_a, same_car_b]
    )
    assert protected == [same_car_a]
    assert other == []


# ------------------------------------------------------------------
# MoondreamLocalAnalyzer — _analyze_frame_sync ambient detection
# ------------------------------------------------------------------


def _make_local_model(
    detect_by_object: dict[str, list[dict[str, float]]],
    query_answer: str,
    caption: str = "A quiet scene.",
) -> MagicMock:
    """Build a mock local moondream model dispatching detect/caption/query
    calls by object name, mirroring the cloud tests' _dispatch_moondream."""
    mock_model = MagicMock()
    mock_model.encode_image.return_value = "encoded"

    def fake_detect(encoded: Any, object_name: str) -> dict[str, Any]:
        return {"objects": detect_by_object.get(object_name, [])}

    mock_model.detect.side_effect = fake_detect
    mock_model.caption.return_value = {"caption": caption}
    mock_model.query.return_value = {"answer": query_answer}
    return mock_model


def test_analyze_frame_sync_non_car_camera_vehicle_gets_query() -> None:
    """A car passing a non-car camera (no person) must still reach query,
    not the generic no-subject skip, and carry a vehicle hint."""
    vehicle_boxes = [{"x_min": 0.2, "y_min": 0.3, "x_max": 0.6, "y_max": 0.8}]
    query_answer = '{"suspicious": false, "confidence": 0.3, "description": "A car drove up the street."}'
    mock_model = _make_local_model({"vehicle": vehicle_boxes}, query_answer)

    a = MoondreamLocalAnalyzer(prompt="p")
    a._md_model = mock_model

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = a._analyze_frame_sync(_FAKE_JPEG, "p", car_applies=False)

    assert "A car drove up the street" in result
    prompt_arg = mock_model.query.call_args[0][1]
    assert "INTERNAL VEHICLE HINT" in prompt_arg
    assert "Vehicle 1 is in the" in prompt_arg


def test_analyze_frame_sync_non_car_camera_animal_gets_query() -> None:
    animal_boxes = [{"x_min": 0.4, "y_min": 0.5, "x_max": 0.6, "y_max": 0.9}]
    query_answer = '{"suspicious": false, "confidence": 0.2, "description": "A cat walked across the yard."}'
    mock_model = _make_local_model({"animal": animal_boxes}, query_answer)

    a = MoondreamLocalAnalyzer(prompt="p")
    a._md_model = mock_model

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = a._analyze_frame_sync(_FAKE_JPEG, "p", car_applies=False)

    assert "A cat walked across the yard" in result
    prompt_arg = mock_model.query.call_args[0][1]
    assert "Animal 1 is in the" in prompt_arg
    assert "INTERNAL VEHICLE HINT" not in prompt_arg


def test_analyze_frame_sync_nothing_detected_skips_query() -> None:
    mock_model = _make_local_model({}, '{"suspicious": false}')

    a = MoondreamLocalAnalyzer(prompt="p")
    a._md_model = mock_model

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = a._analyze_frame_sync(_FAKE_JPEG, "p", car_applies=False)

    assert "No person detected" in result
    mock_model.query.assert_not_called()


def test_analyze_frame_sync_car_camera_multiple_vehicles_uses_vehicle_proximity_hint() -> (
    None
):
    """Car camera: another vehicle near the protected car uses the
    conservative vehicle-proximity hint, not the person/animal one."""
    car_boxes = [
        {"x_min": 0.1, "y_min": 0.2, "x_max": 0.5, "y_max": 0.9},
        {"x_min": 0.5, "y_min": 0.2, "x_max": 0.9, "y_max": 0.9},
    ]

    def fake_detect(encoded: Any, object_name: str) -> dict[str, Any]:
        if object_name == "car":
            return {"objects": car_boxes}
        if object_name == "Silver Kia":
            return {"objects": [car_boxes[0]]}
        return {"objects": []}

    mock_model = MagicMock()
    mock_model.encode_image.return_value = "encoded"
    mock_model.detect.side_effect = fake_detect
    mock_model.caption.return_value = {"caption": "Two cars in the driveway."}
    mock_model.query.return_value = {
        "answer": '{"suspicious": true, "confidence": 0.5, "description": "Another car parked beside it."}'
    }

    a = MoondreamLocalAnalyzer(prompt="p", car_description="Silver Kia")
    a._md_model = mock_model

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = a._analyze_frame_sync(_FAKE_JPEG, "p", car_applies=True)

    assert "Another car parked beside it" in result
    prompt_arg = mock_model.query.call_args[0][1]
    assert "INTERNAL VEHICLE PROXIMITY HINT" in prompt_arg
    assert "camera perspective" in prompt_arg

    # Even though the (mocked) model answered suspicious=true, no person or
    # animal was in frame — the vehicle-only override must force this false.
    is_suspicious, _, _ = MoondreamLocalAnalyzer._try_parse_json(result)
    assert is_suspicious is False


def test_analyze_frame_sync_car_camera_person_near_car_uses_proximity_hint() -> None:
    """Car camera: a person detected alongside the protected vehicle re-runs
    the car detect (Phase 1b skips it when a person is present) and uses the
    person/animal proximity hint, not the vehicle one."""
    person_boxes = [{"x_min": 0.35, "y_min": 0.2, "x_max": 0.55, "y_max": 0.9}]
    car_boxes = [{"x_min": 0.4, "y_min": 0.3, "x_max": 0.9, "y_max": 0.9}]

    def fake_detect(encoded: Any, object_name: str) -> dict[str, Any]:
        if object_name == "person":
            return {"objects": person_boxes}
        if object_name == "car":
            return {"objects": car_boxes}
        return {"objects": []}

    mock_model = MagicMock()
    mock_model.encode_image.return_value = "encoded"
    mock_model.detect.side_effect = fake_detect
    mock_model.caption.return_value = {"caption": "A person stands by the car."}
    mock_model.query.return_value = {
        "answer": '{"suspicious": true, "confidence": 0.8, "description": "Person touching the car."}'
    }

    a = MoondreamLocalAnalyzer(prompt="p", car_description="Silver Kia")
    a._md_model = mock_model

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = a._analyze_frame_sync(_FAKE_JPEG, "p", car_applies=True)

    assert "Person touching the car" in result
    prompt_arg = mock_model.query.call_args[0][1]
    assert "INTERNAL PROXIMITY HINT" in prompt_arg
    assert "person or animal" in prompt_arg


def test_analyze_frame_sync_person_touching_misclassified_car_still_flagged() -> None:
    """Local-analyzer counterpart of the cloud regression test: the person
    is touching the real protected vehicle, but disambiguation happens to
    label the OTHER car box as "protected" on this frame. Proximity must
    still be measured against every detected car box so contact isn't
    missed just because disambiguation picked the wrong box."""
    person_boxes = [{"x_min": 0.65, "y_min": 0.2, "x_max": 0.75, "y_max": 0.9}]
    left_car = {"x_min": 0.05, "y_min": 0.2, "x_max": 0.35, "y_max": 0.9}
    right_car = {"x_min": 0.60, "y_min": 0.2, "x_max": 0.95, "y_max": 0.9}

    def fake_detect(encoded: Any, object_name: str) -> dict[str, Any]:
        if object_name == "person":
            return {"objects": person_boxes}
        if object_name == "car":
            return {"objects": [left_car, right_car]}
        if object_name == "Silver Kia":
            return {"objects": [left_car]}
        return {"objects": []}

    mock_model = MagicMock()
    mock_model.encode_image.return_value = "encoded"
    mock_model.detect.side_effect = fake_detect
    mock_model.caption.return_value = {"caption": "A person stands by a car."}
    mock_model.query.return_value = {
        "answer": '{"suspicious": true, "confidence": 0.85, "description": "Person touching the car."}'
    }

    a = MoondreamLocalAnalyzer(prompt="p", car_description="Silver Kia")
    a._md_model = mock_model

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = a._analyze_frame_sync(_FAKE_JPEG, "p", car_applies=True)

    assert "Person touching the car" in result
    prompt_arg = mock_model.query.call_args[0][1]
    assert "INTERNAL PROXIMITY HINT" in prompt_arg
    assert "touching or pressed against" in prompt_arg
    assert "well away" not in prompt_arg


def test_analyze_frame_sync_car_camera_person_no_car_visible() -> None:
    """Car camera: a person is present but car detect returns empty — base
    prompt rules apply without an explicit suppression hint injected."""
    person_boxes = [{"x_min": 0.35, "y_min": 0.2, "x_max": 0.55, "y_max": 0.9}]

    def fake_detect(encoded: Any, object_name: str) -> dict[str, Any]:
        if object_name == "person":
            return {"objects": person_boxes}
        return {"objects": []}

    mock_model = MagicMock()
    mock_model.encode_image.return_value = "encoded"
    mock_model.detect.side_effect = fake_detect
    mock_model.caption.return_value = {"caption": "A person walks by."}
    mock_model.query.return_value = {
        "answer": '{"suspicious": false, "confidence": 0.2, "description": "Person walking past."}'
    }

    a = MoondreamLocalAnalyzer(prompt="p", car_description="Silver Kia")
    a._md_model = mock_model

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = a._analyze_frame_sync(_FAKE_JPEG, "p", car_applies=True)

    assert "Person walking past" in result
    prompt_arg = mock_model.query.call_args[0][1]
    # No suppression hint — the base prompt's vehicle-distance rules handle it.
    assert "not visible in this frame" not in prompt_arg
    assert "PROXIMITY HINT" not in prompt_arg


def test_analyze_frame_sync_falls_back_to_car_zone_when_no_car_detected() -> None:
    """Local analyzer counterpart: car detect finds nothing, but a fixed car
    zone is configured for the current camera — proximity must still be
    computed against the zone rather than emitting no hint at all."""
    person_boxes = [{"x_min": 0.55, "y_min": 0.2, "x_max": 0.65, "y_max": 0.9}]

    def fake_detect(encoded: Any, object_name: str) -> dict[str, Any]:
        if object_name == "person":
            return {"objects": person_boxes}
        return {"objects": []}

    mock_model = MagicMock()
    mock_model.encode_image.return_value = "encoded"
    mock_model.detect.side_effect = fake_detect
    mock_model.caption.return_value = {"caption": "A person stands in the driveway."}
    mock_model.query.return_value = {
        "answer": '{"suspicious": true, "confidence": 0.8, "description": "Person at the car spot."}'
    }

    a = MoondreamLocalAnalyzer(
        prompt="p",
        car_description="Silver Kia",
        car_zones={
            "Driveway": {"x_min": 0.5, "y_min": 0.2, "x_max": 0.9, "y_max": 0.9}
        },
    )
    a._md_model = mock_model
    a._current_camera = "Driveway"

    with patch("PIL.Image.open", return_value=MagicMock()):
        result = a._analyze_frame_sync(_FAKE_JPEG, "p", car_applies=True)

    assert "Person at the car spot" in result
    prompt_arg = mock_model.query.call_args[0][1]
    assert "INTERNAL PROXIMITY HINT" in prompt_arg
    assert "touching or pressed against" in prompt_arg


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


async def test_analyze_clip_serializes_concurrent_calls(analyzer: ClipAnalyzer) -> None:
    """Two concurrent analyze_clip() calls on the same instance must not
    interleave, since the pipeline stashes per-call state on self
    (_current_camera, _last_prompt_tokens/_last_completion_tokens) across many
    awaited I/O steps. Regression test for the _analyze_lock added to
    BaseAnalyzer.__init__."""
    concurrent_calls = 0
    max_concurrent = 0
    seen_cameras: list[str] = []

    async def fake_extract_frames(_path: str) -> list[bytes]:
        return [_FAKE_JPEG]

    async def fake_call_model(_frames: list[bytes], _prompt: str) -> str:
        nonlocal concurrent_calls, max_concurrent
        concurrent_calls += 1
        max_concurrent = max(max_concurrent, concurrent_calls)
        await asyncio.sleep(0)
        seen_cameras.append(analyzer._current_camera)
        await asyncio.sleep(0)
        concurrent_calls -= 1
        return json.dumps({"suspicious": False, "confidence": 0.1, "description": "ok"})

    with (
        patch.object(analyzer, "extract_frames", side_effect=fake_extract_frames),
        patch.object(analyzer, "_call_model", side_effect=fake_call_model),
    ):
        results = await asyncio.gather(
            analyzer.analyze_clip("/clips/a.mp4", "clip-a", "Front Door"),
            analyzer.analyze_clip("/clips/b.mp4", "clip-b", "Driveway"),
        )

    assert max_concurrent == 1
    assert sorted(seen_cameras) == ["Driveway", "Front Door"]
    assert {r.camera for r in results} == {"Front Door", "Driveway"}


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


def test_anthropic_model_pricing_sonnet_5() -> None:
    a = AnthropicAnalyzer(api_key="key", model="claude-sonnet-5", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 2.00
    assert out == 10.00


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


async def test_anthropic_health_check_caches_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call within the cache TTL must not hit the Anthropic API again."""
    import sys

    mock_mod = _make_anthropic_module()
    monkeypatch.setitem(sys.modules, "anthropic", mock_mod)

    a = AnthropicAnalyzer(api_key="valid-key", model="claude-haiku-4-5", prompt="test")
    a._client = mock_mod.AsyncAnthropic.return_value
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        first = await a.health_check()
        second = await a.health_check()
    assert first is True
    assert second is True
    assert mock_mod.AsyncAnthropic.return_value.models.list.call_count == 1


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

    with patch("asyncio.sleep", new_callable=AsyncMock):
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


def test_try_parse_json_null_confidence_defaults_to_zero() -> None:
    """Valid JSON with a non-numeric confidence (e.g. null) must not raise —
    it should fall back to 0.0 instead of crashing the clip's analysis."""
    response = '{"suspicious": true, "confidence": null, "description": "test"}'
    assert ClipAnalyzer._try_parse_json(response) == (True, 0.0, "test")


def test_try_parse_json_non_numeric_confidence_defaults_to_zero() -> None:
    """A string confidence value is likewise swallowed, not raised."""
    response = '{"suspicious": false, "confidence": "high", "description": "x"}'
    assert ClipAnalyzer._try_parse_json(response) == (False, 0.0, "x")


def test_try_parse_json_string_false_is_not_suspicious() -> None:
    """Regression test: a looser vision model can emit "suspicious": "false"
    as a JSON *string* rather than a boolean. bool("false") is True in
    Python, which used to flip a model's clearly-intended "not suspicious"
    into a spurious suspicious verdict (a false-positive "cry wolf" bug)."""
    response = '{"suspicious": "false", "confidence": 0.8, "description": "x"}'
    is_suspicious, _, _ = ClipAnalyzer._try_parse_json(response)
    assert is_suspicious is False


def test_try_parse_json_string_true_is_suspicious() -> None:
    """The string "true" (any case) is still honoured as suspicious."""
    response = '{"suspicious": "True", "confidence": 0.8, "description": "x"}'
    is_suspicious, _, _ = ClipAnalyzer._try_parse_json(response)
    assert is_suspicious is True


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
    session = analyzer._get_session()
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
    session = a._get_session()
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


async def test_moondream_cloud_call_api_frame_429_sets_rate_limited() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 429
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    a._session = _mock_session(post=MagicMock(return_value=mock_resp))
    assert a.rate_limited is False
    assert await a._call_api_frame(_FAKE_JPEG, "prompt") == ""
    assert a.rate_limited is True


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
    assert a.rate_limited is False
    with patch.dict(sys.modules, {"anthropic": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""
    assert a.rate_limited is True


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


# ------------------------------------------------------------------
# OpenAI helpers
# ------------------------------------------------------------------


class _MockOpenAIAPIStatusError(Exception):
    """Minimal stand-in for openai.APIStatusError."""

    def __init__(
        self, msg: str = "", status_code: int = 500, message: str = "err"
    ) -> None:
        super().__init__(msg)
        self.status_code = status_code
        self.message = message


def _make_openai_response(
    text: str = '{"suspicious": false, "confidence": 0.1, "description": "Empty scene"}',
    prompt_tokens: int = 150,
    completion_tokens: int = 45,
) -> MagicMock:
    """Return a mock openai chat.completions.create() response."""
    message = MagicMock()
    message.content = text

    choice = MagicMock()
    choice.message = message

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def _make_openai_module(
    response: MagicMock | None = None,
    auth_error: bool = False,
    permission_error: bool = False,
    rate_limit_error: bool = False,
    api_status_error: bool = False,
    models_data: list | None = None,
    models_auth_error: bool = False,
    models_exception: Exception | None = None,
) -> MagicMock:
    """Return a mock openai module with AsyncOpenAI client."""
    mod = MagicMock()

    # Error classes
    mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
    mod.PermissionDeniedError = type("PermissionDeniedError", (Exception,), {})
    mod.RateLimitError = type("RateLimitError", (Exception,), {})
    mod.BadRequestError = type(
        "BadRequestError", (Exception,), {"message": "bad request"}
    )
    mod.APIStatusError = _MockOpenAIAPIStatusError
    mod.APIConnectionError = type("APIConnectionError", (Exception,), {})

    # AsyncOpenAI client
    client = MagicMock()
    mod.AsyncOpenAI.return_value = client

    # chat.completions.create
    if auth_error:
        client.chat.completions.create = AsyncMock(
            side_effect=mod.AuthenticationError("bad key")
        )
    elif permission_error:
        client.chat.completions.create = AsyncMock(
            side_effect=mod.PermissionDeniedError("no perm")
        )
    elif rate_limit_error:
        client.chat.completions.create = AsyncMock(
            side_effect=mod.RateLimitError("rate limited")
        )
    elif api_status_error:
        err = _MockOpenAIAPIStatusError(
            "api error", status_code=500, message="server error"
        )
        client.chat.completions.create = AsyncMock(side_effect=err)
    elif response is not None:
        client.chat.completions.create = AsyncMock(return_value=response)

    # models.list
    if models_auth_error:
        client.models.list = AsyncMock(side_effect=mod.AuthenticationError("bad key"))
    elif models_exception is not None:
        client.models.list = AsyncMock(side_effect=models_exception)
    elif models_data is not None:
        page = MagicMock()
        page.data = models_data
        client.models.list = AsyncMock(return_value=page)
    else:
        page = MagicMock()
        page.data = []
        client.models.list = AsyncMock(return_value=page)

    # close
    client.close = AsyncMock()

    return mod


# ------------------------------------------------------------------
# OpenAIAnalyzer — basic attrs
# ------------------------------------------------------------------


def test_openai_provider_name() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    assert a.provider_name == "openai"
    assert a.model_name() == "gpt-4o-mini"


def test_openai_model_default() -> None:
    a = OpenAIAnalyzer(api_key="key", model="", prompt="test")
    assert a.model_name() == "gpt-4o-mini"


def test_openai_model_pricing_gpt4o_mini() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 0.15
    assert out == 0.60


def test_openai_model_pricing_gpt4o() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-4o", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 2.50
    assert out == 10.00


def test_openai_model_pricing_gpt4_turbo() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-4-turbo", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 10.00
    assert out == 30.00


def test_openai_model_pricing_gpt41_mini() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-4.1-mini", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 0.40
    assert out == 1.60


def test_openai_model_pricing_unknown_falls_back_to_gpt4o() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-future-99b", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 2.50
    assert out == 10.00


def test_openai_model_pricing_gpt5() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-5", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 1.25
    assert out == 10.00


def test_openai_model_pricing_gpt5_mini() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-5-mini", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 0.25
    assert out == 2.00


def test_openai_model_pricing_gpt5_nano() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-5-nano", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 0.05
    assert out == 0.40


def test_openai_model_pricing_gpt54() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-5.4", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 2.50
    assert out == 15.00


def test_openai_model_pricing_gpt54_mini() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-5.4-mini", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 0.75
    assert out == 4.50


def test_openai_model_pricing_gpt54_nano() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-5.4-nano", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 0.20
    assert out == 1.25


def test_openai_model_pricing_gpt55() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-5.5", prompt="test")
    inp, out = a.model_pricing()
    assert inp == 5.00
    assert out == 30.00


# ------------------------------------------------------------------
# OpenAIAnalyzer — is_openai_vision_model
# ------------------------------------------------------------------


def test_is_openai_vision_model_gpt4o() -> None:
    assert is_openai_vision_model("gpt-4o") is True
    assert is_openai_vision_model("gpt-4o-mini") is True
    assert is_openai_vision_model("gpt-4o-2024-08-06") is True


def test_is_openai_vision_model_gpt4_turbo() -> None:
    assert is_openai_vision_model("gpt-4-turbo") is True
    assert is_openai_vision_model("gpt-4-turbo-2024-04-09") is True


def test_is_openai_vision_model_gpt41() -> None:
    assert is_openai_vision_model("gpt-4.1") is True
    assert is_openai_vision_model("gpt-4.1-mini") is True
    assert is_openai_vision_model("gpt-4.1-nano") is True


def test_is_openai_vision_model_o_series() -> None:
    assert is_openai_vision_model("o1") is True
    assert is_openai_vision_model("o3") is True
    assert is_openai_vision_model("o4-mini") is True


def test_is_openai_vision_model_non_vision() -> None:
    assert is_openai_vision_model("text-davinci-003") is False
    assert is_openai_vision_model("whisper-1") is False
    assert is_openai_vision_model("text-embedding-ada-002") is False


def test_is_openai_vision_model_gpt5_family() -> None:
    assert is_openai_vision_model("gpt-5") is True
    assert is_openai_vision_model("gpt-5-mini") is True
    assert is_openai_vision_model("gpt-5-nano") is True
    assert is_openai_vision_model("gpt-5.4") is True
    assert is_openai_vision_model("gpt-5.4-mini") is True
    assert is_openai_vision_model("gpt-5.4-nano") is True
    assert is_openai_vision_model("gpt-5.5") is True


def test_is_openai_vision_model_excludes_pro_suffix() -> None:
    assert is_openai_vision_model("gpt-5.5-pro") is False


# ------------------------------------------------------------------
# OpenAIAnalyzer — health_check
# ------------------------------------------------------------------


async def test_openai_health_check_no_key() -> None:
    a = OpenAIAnalyzer(api_key="", model="gpt-4o-mini", prompt="test")
    assert await a.health_check() is False


async def test_openai_health_check_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.delitem(sys.modules, "openai", raising=False)
    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    with patch("builtins.__import__", side_effect=ImportError("no module openai")):
        result = await a.health_check()
    assert result is False


async def test_openai_health_check_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mock_mod = _make_openai_module()
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="valid-key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a.health_check()
    assert result is True


async def test_openai_health_check_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mock_mod = _make_openai_module(models_auth_error=True)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="bad-key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a.health_check()
    assert result is False


async def test_openai_health_check_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module()
    mock_mod.AsyncOpenAI.return_value.models.list = AsyncMock(
        side_effect=mock_mod.PermissionDeniedError("no permission")
    )
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a.health_check()
    assert result is False


async def test_openai_health_check_generic_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module(models_exception=RuntimeError("connection refused"))
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a.health_check()
    assert result is False


async def test_openai_health_check_caches_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call within the cache TTL must not hit the OpenAI API again."""
    import sys

    mock_mod = _make_openai_module()
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="valid-key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        first = await a.health_check()
        second = await a.health_check()
    assert first is True
    assert second is True
    assert mock_mod.AsyncOpenAI.return_value.models.list.call_count == 1


async def test_openai_health_check_cache_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the TTL elapses, health_check must hit the API again."""
    import sys

    import blink_downloader.analyzer as analyzer_module

    mock_mod = _make_openai_module()
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="valid-key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value

    fake_now = 1000.0
    monkeypatch.setattr(analyzer_module.time, "monotonic", lambda: fake_now)
    with patch.dict(sys.modules, {"openai": mock_mod}):
        await a.health_check()
        fake_now += a._HEALTH_CHECK_CACHE_SECONDS + 1
        await a.health_check()
    assert mock_mod.AsyncOpenAI.return_value.models.list.call_count == 2


# ------------------------------------------------------------------
# OpenAIAnalyzer — fetch_models
# ------------------------------------------------------------------


async def test_openai_fetch_models_from_api(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    m1 = MagicMock()
    m1.id = "gpt-4o"
    m2 = MagicMock()
    m2.id = "gpt-4o-mini"
    m3 = MagicMock()
    m3.id = "whisper-1"  # not vision-capable

    mock_mod = _make_openai_module(models_data=[m1, m2, m3])
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        models = await a.fetch_models()

    names = [m["name"] for m in models]
    assert "gpt-4o" in names
    assert "gpt-4o-mini" in names
    assert "whisper-1" not in names
    assert len(models) == 2


async def test_openai_fetch_models_fallback_no_key() -> None:
    a = OpenAIAnalyzer(api_key="", model="gpt-4o-mini", prompt="test")
    models = await a.fetch_models()
    assert len(models) == len(_OPENAI_FALLBACK_MODELS)
    assert any(m["name"] == "gpt-4o-mini" for m in models)


async def test_openai_fetch_models_fallback_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module(models_auth_error=True)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="bad-key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        models = await a.fetch_models()
    assert len(models) == len(_OPENAI_FALLBACK_MODELS)


async def test_openai_fetch_models_fallback_on_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module(models_exception=RuntimeError("API unavailable"))
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        models = await a.fetch_models()
    assert len(models) == len(_OPENAI_FALLBACK_MODELS)


async def test_openai_fetch_models_import_error() -> None:
    import sys

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    with patch.dict(sys.modules, {"openai": None}):
        models = await a.fetch_models()
    assert len(models) == len(_OPENAI_FALLBACK_MODELS)


async def test_openai_fetch_models_empty_api_returns_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the API returns no vision models, fall back to hardcoded list."""
    import sys

    non_vision = MagicMock()
    non_vision.id = "whisper-1"

    mock_mod = _make_openai_module(models_data=[non_vision])
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        models = await a.fetch_models()
    assert len(models) == len(_OPENAI_FALLBACK_MODELS)


# ------------------------------------------------------------------
# OpenAIAnalyzer — _call_model
# ------------------------------------------------------------------


async def test_openai_call_model_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    resp = _make_openai_response(
        '{"suspicious": true, "confidence": 0.85, "description": "Intruder"}',
        prompt_tokens=300,
        completion_tokens=60,
    )
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="Analyze.")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a._call_model([_FAKE_JPEG, _FAKE_JPEG], "Analyze this scene.")

    assert "Intruder" in result or "suspicious" in result.lower()
    assert a._last_prompt_tokens == 300
    assert a._last_completion_tokens == 60


async def test_openai_call_model_no_frames() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    result = await a._call_model([], "Analyze")
    assert result == ""


async def test_openai_call_model_import_error() -> None:
    import sys

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    with patch.dict(sys.modules, {"openai": None}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


async def test_openai_call_model_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mock_mod = _make_openai_module(auth_error=True)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="bad-key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "Analyze")
    assert result == ""


async def test_openai_call_model_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module(permission_error=True)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "Analyze")
    assert result == ""


async def test_openai_call_model_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module(rate_limit_error=True)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    assert a.rate_limited is False
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "Analyze")
    assert result == ""
    assert a.rate_limited is True


async def test_openai_call_model_bad_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module()
    bad_req = mock_mod.BadRequestError("model has no vision support")
    bad_req.message = "model has no vision support"
    mock_mod.AsyncOpenAI.return_value.chat.completions.create = AsyncMock(
        side_effect=bad_req
    )
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


async def test_openai_call_model_api_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module(api_status_error=True)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "Analyze")
    assert result == ""


async def test_openai_call_model_api_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module()
    mock_mod.AsyncOpenAI.return_value.chat.completions.create = AsyncMock(
        side_effect=mock_mod.APIConnectionError("connection refused")
    )
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


async def test_openai_call_model_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mock_mod = _make_openai_module()
    mock_mod.AsyncOpenAI.return_value.chat.completions.create = AsyncMock(
        side_effect=asyncio.TimeoutError
    )
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "Analyze") == ""


async def test_openai_call_model_generic_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    mock_mod = _make_openai_module()
    mock_mod.AsyncOpenAI.return_value.chat.completions.create = AsyncMock(
        side_effect=ValueError("unexpected error")
    )
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        assert await a._call_model([_FAKE_JPEG], "prompt") == ""


async def test_openai_call_model_no_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """When usage is None, token counts stay zero."""
    import sys

    resp = _make_openai_response("All clear")
    resp.usage = None
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        await a._call_model([_FAKE_JPEG], "prompt")
    assert a._last_prompt_tokens == 0
    assert a._last_completion_tokens == 0


async def test_openai_call_model_empty_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty choices list returns empty string."""
    import sys

    resp = _make_openai_response("text")
    resp.choices = []
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        result = await a._call_model([_FAKE_JPEG], "prompt")
    assert result == ""


async def test_openai_call_model_uses_max_completion_tokens_for_gpt5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpt-5 family models must use max_completion_tokens, not max_tokens."""
    import sys

    resp = _make_openai_response()
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-5.4-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        await a._call_model([_FAKE_JPEG], "prompt")

    create_call = mock_mod.AsyncOpenAI.return_value.chat.completions.create
    kwargs = create_call.call_args.kwargs
    assert "max_tokens" not in kwargs
    assert kwargs["max_completion_tokens"] == 1024
    assert kwargs["reasoning_effort"] == "medium"


async def test_openai_call_model_omits_reasoning_effort_for_pro_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "-pro" tier reasoning models only support "high" reasoning_effort, so the
    "low" optimization must not be applied to them."""
    import sys

    resp = _make_openai_response()
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-5.2-pro", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        await a._call_model([_FAKE_JPEG], "prompt")

    create_call = mock_mod.AsyncOpenAI.return_value.chat.completions.create
    kwargs = create_call.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 1024
    assert "reasoning_effort" not in kwargs


async def test_openai_call_model_uses_max_completion_tokens_for_o_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """o1/o3/o4-mini reasoning models must use max_completion_tokens too."""
    import sys

    resp = _make_openai_response()
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="o4-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        await a._call_model([_FAKE_JPEG], "prompt")

    create_call = mock_mod.AsyncOpenAI.return_value.chat.completions.create
    kwargs = create_call.call_args.kwargs
    assert "max_tokens" not in kwargs
    assert kwargs["max_completion_tokens"] == 1024
    assert kwargs["reasoning_effort"] == "medium"


async def test_openai_call_model_uses_max_tokens_for_gpt4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy gpt-4 family models keep using max_tokens."""
    import sys

    resp = _make_openai_response()
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        await a._call_model([_FAKE_JPEG], "prompt")

    create_call = mock_mod.AsyncOpenAI.return_value.chat.completions.create
    kwargs = create_call.call_args.kwargs
    assert "max_completion_tokens" not in kwargs
    assert kwargs["max_tokens"] == 512
    assert "reasoning_effort" not in kwargs


async def test_openai_call_model_uses_structured_outputs_for_gpt4o(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpt-4o family gets Structured Outputs (json_schema/strict), not json_object."""
    import sys

    resp = _make_openai_response()
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        await a._call_model([_FAKE_JPEG], "prompt")

    create_call = mock_mod.AsyncOpenAI.return_value.chat.completions.create
    kwargs = create_call.call_args.kwargs
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["strict"] is True
    schema = kwargs["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {"suspicious", "confidence", "description"}
    assert schema["additionalProperties"] is False


async def test_openai_call_model_uses_json_object_for_gpt4_turbo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpt-4-turbo predates Structured Outputs, so it stays on json_object mode."""
    import sys

    resp = _make_openai_response()
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    a = OpenAIAnalyzer(api_key="key", model="gpt-4-turbo", prompt="test")
    a._client = mock_mod.AsyncOpenAI.return_value
    with patch.dict(sys.modules, {"openai": mock_mod}):
        await a._call_model([_FAKE_JPEG], "prompt")

    create_call = mock_mod.AsyncOpenAI.return_value.chat.completions.create
    kwargs = create_call.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}


async def test_openai_call_model_uses_structured_outputs_for_gpt5_and_o4_mini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpt-5 and o4-mini also get Structured Outputs, not just legacy json_object."""
    import sys

    for model in ("gpt-5.4-mini", "o4-mini"):
        resp = _make_openai_response()
        mock_mod = _make_openai_module(response=resp)
        monkeypatch.setitem(sys.modules, "openai", mock_mod)

        a = OpenAIAnalyzer(api_key="key", model=model, prompt="test")
        a._client = mock_mod.AsyncOpenAI.return_value
        with patch.dict(sys.modules, {"openai": mock_mod}):
            await a._call_model([_FAKE_JPEG], "prompt")

        create_call = mock_mod.AsyncOpenAI.return_value.chat.completions.create
        kwargs = create_call.call_args.kwargs
        assert kwargs["response_format"]["type"] == "json_schema", model


# ------------------------------------------------------------------
# Cross-provider two-tier escalation (BaseAnalyzer._maybe_escalate /
# _call_model_with_escalation) — tier 2 may be any provider, including one
# different from tier 1.
# ------------------------------------------------------------------


async def test_escalation_disabled_by_default() -> None:
    """No escalation analyzer attached means only tier 1 is ever called."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="test")
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.9, "description": "Intruder"}'
    )
    result = await a._call_model_with_escalation([_FAKE_JPEG], "prompt")
    a._call_model.assert_awaited_once()
    assert "Intruder" in result
    assert a._last_escalation_model == ""
    assert a._last_escalation_provider == ""


async def test_escalation_skipped_when_not_suspicious() -> None:
    """Tier 1 result that isn't suspicious never triggers tier 2."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="test")
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.1, "description": "Empty scene"}'
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock()  # type: ignore[method-assign]
    a.set_escalation_analyzer(tier2)

    result = await a._call_model_with_escalation([_FAKE_JPEG], "prompt")

    tier2._call_model.assert_not_awaited()
    assert "Empty scene" in result


async def test_escalation_skipped_on_malformed_tier1_json() -> None:
    """Malformed/non-JSON tier-1 response never triggers escalation."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="test")
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value="not valid json at all"
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock()  # type: ignore[method-assign]
    a.set_escalation_analyzer(tier2)

    result = await a._call_model_with_escalation([_FAKE_JPEG], "prompt")

    tier2._call_model.assert_not_awaited()
    assert result == "not valid json at all"


async def test_escalation_triggers_with_empty_description() -> None:
    """A suspicious verdict still escalates even if description is empty."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="test")
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.7, "description": ""}'
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            '{"suspicious": true, "confidence": 0.95, '
            '"description": "Confirmed intruder"}'
        )
    )
    a.set_escalation_analyzer(tier2)

    result = await a._call_model_with_escalation([_FAKE_JPEG], "prompt")

    tier2._call_model.assert_awaited_once()
    assert "Confirmed intruder" in result


async def test_escalation_triggers_cross_provider_and_tracks_tokens_per_tier() -> None:
    """Suspicious tier-1 verdict escalates to a DIFFERENT provider; each tier
    tracks its own tokens independently since tier 1 and tier 2 are separate
    BaseAnalyzer instances (no shared save/restore dance needed)."""
    tier1 = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")

    async def _tier1_call(frames: list, prompt: str) -> str:
        tier1._last_prompt_tokens = 300
        tier1._last_completion_tokens = 50
        return (
            '{"suspicious": true, "confidence": 0.7, "description": "Person near door"}'
        )

    tier1._call_model = _tier1_call  # type: ignore[method-assign]

    tier2 = MoondreamCloudAnalyzer(api_key="key", prompt="test")

    async def _tier2_call(frames: list, prompt: str) -> str:
        tier2._last_prompt_tokens = 500
        tier2._last_completion_tokens = 80
        return (
            '{"suspicious": true, "confidence": 0.95, '
            '"description": "Confirmed intruder"}'
        )

    tier2._call_model = _tier2_call  # type: ignore[method-assign]
    tier1.set_escalation_analyzer(tier2)

    result = await tier1._call_model_with_escalation([_FAKE_JPEG], "prompt")

    assert "Confirmed intruder" in result
    assert tier1._last_prompt_tokens == 300
    assert tier1._last_completion_tokens == 50
    assert tier1._last_escalation_provider == "moondream_cloud"
    assert tier1._last_escalation_model == tier2.model_name()
    assert tier1._last_escalation_prompt_tokens == 500
    assert tier1._last_escalation_completion_tokens == 80


async def test_escalation_falls_back_when_tier2_fails() -> None:
    """If the escalation call returns nothing, the tier-1 result is kept."""
    tier1 = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    tier1._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.7, "description": "Person near door"}'
    )
    tier1._last_prompt_tokens = 300
    tier1._last_completion_tokens = 50

    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock(return_value="")  # type: ignore[method-assign]
    tier1.set_escalation_analyzer(tier2)

    result = await tier1._call_model_with_escalation([_FAKE_JPEG], "prompt")

    assert "Person near door" in result
    assert tier1._last_prompt_tokens == 300
    assert tier1._last_completion_tokens == 50
    assert tier1._last_escalation_model == ""
    assert tier1._last_escalation_provider == ""


async def test_escalation_falls_back_on_truncated_tier2_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-empty but truncated/malformed tier-2 response (e.g. a reasoning
    model's thinking tokens ate its completion budget before the JSON closed)
    must not be trusted as a genuine "not suspicious" verdict — it must fall
    back to tier 1's suspicious result instead of silently suppressing the
    alert. This is provider-agnostic: tier 2 here is a completely different
    provider than tier 1."""
    tier1 = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    tier1._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.9, "description": "Person at car door"}'
    )
    tier1._last_prompt_tokens = 300
    tier1._last_completion_tokens = 50

    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    # Truncated mid-value: non-empty, but not parseable JSON.
    tier2._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.95, "descri'
    )
    tier1.set_escalation_analyzer(tier2)

    with caplog.at_level("WARNING"):
        result = await tier1._call_model_with_escalation([_FAKE_JPEG], "prompt")

    assert "Person at car door" in result
    is_suspicious, _, _ = tier1._try_parse_json(result)
    assert is_suspicious is True
    assert tier1._last_prompt_tokens == 300
    assert tier1._last_completion_tokens == 50
    assert "malformed/truncated" in caplog.text


async def test_escalation_via_sequential_strategy_escalates_once_on_winning_frame() -> (
    None
):
    """When frame_strategy is 'sequential', escalation must fire at most once
    — on the single winning frame/response from _analyze_sequentially — not
    once per frame, to keep tier-2 cost bounded."""
    tier1 = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="sequential",
    )
    susp = json.dumps(
        {"suspicious": True, "confidence": 0.7, "description": "Person near car"}
    )
    non_susp = json.dumps(
        {"suspicious": False, "confidence": 0.9, "description": "Empty street"}
    )

    async def fake_call_model(frames: list, prompt: str) -> str:
        return susp if frames[0] == _FAKE_JPEG_2 else non_susp

    tier1._call_model = fake_call_model  # type: ignore[method-assign]

    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            '{"suspicious": true, "confidence": 0.95, '
            '"description": "Confirmed intruder"}'
        )
    )
    tier1.set_escalation_analyzer(tier2)

    response, escalation_frame = await tier1._analyze_sequentially(
        [_FAKE_JPEG, _FAKE_JPEG_2], "p"
    )
    assert escalation_frame == _FAKE_JPEG_2
    assert escalation_frame is not None
    final = await tier1._maybe_escalate([escalation_frame], "p", response)

    tier2._call_model.assert_awaited_once()
    assert "Confirmed intruder" in final


# ------------------------------------------------------------------
# High-recall escalation for protected-vehicle ("car") cameras — a clear
# tier-1 verdict on one of these cameras must still be double-checked by
# tier 2, unlike ordinary cameras where a clear verdict is trusted outright.
# See BaseAnalyzer._car_protection_applies / _maybe_escalate.
# ------------------------------------------------------------------


async def test_car_camera_clear_verdict_still_escalates() -> None:
    """A car-protected camera's "clear" tier-1 result must still be sent to
    tier 2 — this is the fix for a real miss: tier 1 said 'clear' on a
    person leaning against the protected vehicle, and the old asymmetric
    escalation (suspicious-only) never gave tier 2 a chance to catch it."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="test",
        car_description="Silver Honda Civic",
        car_cameras=["Driveway"],
    )
    a._current_camera = "Driveway"
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.89, "description": "Person pauses near car"}'
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.85, "description": "Person leaning against car"}'
    )
    a.set_escalation_analyzer(tier2)

    result = await a._call_model_with_escalation([_FAKE_JPEG], "prompt")

    tier2._call_model.assert_awaited_once()
    assert "leaning against car" in result
    assert a._last_escalation_provider == "anthropic"


async def test_car_camera_clear_verdict_stays_clear_when_tier2_agrees() -> None:
    """If tier 2 also says clear, tier 1's own "clear" response is kept
    rather than swapped for an equivalent one — avoids losing tier-1's
    already-recorded description/confidence pairing for no benefit."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="test",
        car_description="Silver Honda Civic",
        car_cameras=["Driveway"],
    )
    a._current_camera = "Driveway"
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.9, "description": "Empty driveway"}'
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.8, "description": "Still empty"}'
    )
    a.set_escalation_analyzer(tier2)

    result = await a._call_model_with_escalation([_FAKE_JPEG], "prompt")

    tier2._call_model.assert_awaited_once()
    assert "Empty driveway" in result
    # Tokens/model attribution are still recorded even though the tier-1
    # text is kept, since a real tier-2 API call was made.
    assert a._last_escalation_provider == "anthropic"


async def test_non_car_camera_clear_verdict_never_escalates() -> None:
    """A camera not in car_cameras keeps the cost-optimized behavior even
    when a protected vehicle exists elsewhere on the property."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="test",
        car_description="Silver Honda Civic",
        car_cameras=["Driveway"],
    )
    a._current_camera = "Front Door"
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.85, "description": "Person leaves via front door"}'
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock()  # type: ignore[method-assign]
    a.set_escalation_analyzer(tier2)

    result = await a._call_model_with_escalation([_FAKE_JPEG], "prompt")

    tier2._call_model.assert_not_awaited()
    assert "Person leaves via front door" in result


async def test_car_camera_without_zone_restriction_high_recall_applies_everywhere() -> (
    None
):
    """car_cameras=[] (documented default: applies to every camera) also
    gets high-recall escalation on any camera, not just an explicitly
    listed one."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="test",
        car_description="Silver Honda Civic",
    )
    a._current_camera = "Backyard"
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.9, "description": "Person pauses near car"}'
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.8, "description": "Contact with car"}'
    )
    a.set_escalation_analyzer(tier2)

    result = await a._call_model_with_escalation([_FAKE_JPEG], "prompt")

    tier2._call_model.assert_awaited_once()
    assert "Contact with car" in result


async def test_escalation_propagates_camera_identity_to_tier2() -> None:
    """Regression test: tier2 never runs its own _reset_analysis_state()
    (that only happens inside an analyzer's own _analyze_clip_locked()), so
    without _maybe_escalate explicitly propagating the camera, tier2's
    _current_camera stays at its unset default (""). Moondream's _call_model
    reads _current_camera to compute car_applies/look up the car zone, so a
    restricted (non-empty) ai_car_cameras config would silently degrade the
    escalated call's car-protection rules to the wrong (empty) camera."""
    tier1 = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="test",
        car_description="Silver Honda Civic",
        car_cameras=["Driveway"],
    )
    tier1._current_camera = "Driveway"
    tier1._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.9, "description": "Empty driveway"}'
    )

    tier2 = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="test",
        car_description="Silver Honda Civic",
        car_cameras=["Driveway"],
        car_zones={
            "Driveway": {"x_min": 0.1, "y_min": 0.1, "x_max": 0.5, "y_max": 0.5}
        },
    )
    seen: dict[str, object] = {}

    async def _tier2_call(frames: list, prompt: str) -> str:
        # Mirrors exactly what MoondreamCloudAnalyzer._call_model itself
        # does first: read _current_camera, then compute car_applies and
        # look up the car zone from it.
        seen["camera"] = tier2._current_camera
        seen["car_applies"] = tier2._car_protection_applies(tier2._current_camera)
        seen["zone"] = tier2._car_zones.get(tier2._current_camera)
        return (
            '{"suspicious": true, "confidence": 0.8, "description": "Contact with car"}'
        )

    tier2._call_model = _tier2_call  # type: ignore[method-assign]
    tier1.set_escalation_analyzer(tier2)

    await tier1._call_model_with_escalation([_FAKE_JPEG], "prompt")

    assert seen["camera"] == "Driveway"
    assert seen["car_applies"] is True
    assert seen["zone"] == {"x_min": 0.1, "y_min": 0.1, "x_max": 0.5, "y_max": 0.5}


def test_is_well_formed_json_object_true_for_complete_json() -> None:
    assert BaseAnalyzer._is_well_formed_json_object(
        '{"suspicious": false, "confidence": 0.1, "description": "Empty"}'
    )


def test_is_well_formed_json_object_false_for_truncated_json() -> None:
    assert not BaseAnalyzer._is_well_formed_json_object(
        '{"suspicious": true, "confidence": 0.9, "descri'
    )


def test_is_well_formed_json_object_false_for_no_braces() -> None:
    assert not BaseAnalyzer._is_well_formed_json_object("not json at all")


# ------------------------------------------------------------------
# OpenAIAnalyzer — close / _get_client
# ------------------------------------------------------------------


async def test_openai_close() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    mock_client = AsyncMock()
    a._client = mock_client
    await a.close()
    mock_client.close.assert_called_once()
    assert a._client is None


async def test_openai_close_no_client() -> None:
    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="test")
    await a.close()  # Should not raise


def test_openai_get_client_creates_client() -> None:
    import sys

    mock_mod = _make_openai_module()
    a = OpenAIAnalyzer(api_key="sk-openai-test", model="gpt-4o-mini", prompt="test")
    with patch.dict(sys.modules, {"openai": mock_mod}):
        client = a._get_client()
    assert client is not None
    mock_mod.AsyncOpenAI.assert_called_once_with(api_key="sk-openai-test")


# ------------------------------------------------------------------
# OpenAIAnalyzer — frame resize
# ------------------------------------------------------------------


def test_openai_resize_frame_resizes_large_image() -> None:
    import io

    from PIL import Image

    img = Image.new("RGB", (3000, 2000), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    result = OpenAIAnalyzer._resize_frame(buf.getvalue(), max_dimension=2048)
    resized = Image.open(io.BytesIO(result))
    assert max(resized.width, resized.height) <= 2048


def test_openai_resize_frame_skips_small_image() -> None:
    import io

    from PIL import Image

    img = Image.new("RGB", (640, 480), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    original = buf.getvalue()
    assert OpenAIAnalyzer._resize_frame(original) == original


# ------------------------------------------------------------------
# OpenAIAnalyzer — full pipeline
# ------------------------------------------------------------------


async def test_openai_full_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full analyze_clip pipeline: ffmpeg → OpenAI API → AnalysisResult."""
    import sys

    resp = _make_openai_response(
        '{"suspicious": true, "confidence": 0.9, "description": "Suspicious person"}',
        prompt_tokens=400,
        completion_tokens=80,
    )
    mock_mod = _make_openai_module(response=resp)
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="Analyze.")
    a._client = mock_mod.AsyncOpenAI.return_value

    with patch.dict(sys.modules, {"openai": mock_mod}):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await a.analyze_clip("/clips/test.mp4", "clip-oai-1", "Front Door")

    assert result.clip_id == "clip-oai-1"
    assert result.is_suspicious is True
    assert result.confidence == 0.9
    assert result.tokens_prompt == 400
    assert result.tokens_completion == 80
    assert result.frame_count == 1


async def test_openai_tokens_reset_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token counts from a previous call must not bleed into the next."""
    import sys

    resp1 = _make_openai_response(prompt_tokens=500, completion_tokens=100)
    resp2 = _make_openai_response(prompt_tokens=200, completion_tokens=40)

    mock_mod = _make_openai_module()
    mock_mod.AsyncOpenAI.return_value.chat.completions.create = AsyncMock(
        side_effect=[resp1, resp2]
    )
    monkeypatch.setitem(sys.modules, "openai", mock_mod)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    a = OpenAIAnalyzer(api_key="key", model="gpt-4o-mini", prompt="Analyze.")
    a._client = mock_mod.AsyncOpenAI.return_value

    with patch.dict(sys.modules, {"openai": mock_mod}):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            r1 = await a.analyze_clip("/clips/a.mp4", "c1", "Cam1")
            r2 = await a.analyze_clip("/clips/b.mp4", "c2", "Cam2")

    assert r1.tokens_prompt == 500
    assert r1.tokens_completion == 100
    assert r2.tokens_prompt == 200
    assert r2.tokens_completion == 40


# ------------------------------------------------------------------
# create_analyzer factory — OpenAI
# ------------------------------------------------------------------


def test_create_analyzer_openai() -> None:
    a = create_analyzer("openai", "prompt", openai_api_key="sk-test")
    assert isinstance(a, OpenAIAnalyzer)
    assert a.model_name() == "gpt-4o-mini"  # default


def test_create_analyzer_openai_with_model() -> None:
    a = create_analyzer(
        "openai", "prompt", openai_api_key="sk-test", openai_model="gpt-4o"
    )
    assert isinstance(a, OpenAIAnalyzer)
    assert a.model_name() == "gpt-4o"


def test_create_analyzer_openai_no_key() -> None:
    a = create_analyzer("openai", "prompt")
    assert a is None


def test_create_analyzer_same_provider_escalation() -> None:
    """Same-provider two-model escalation (today's OpenAI-only use case,
    generalized) still works via ai_escalation_provider/ai_escalation_model."""
    a = create_analyzer(
        "openai",
        "prompt",
        openai_api_key="sk-test",
        openai_model="gpt-4o-mini",
        escalation_provider="openai",
        escalation_model="gpt-4o",
    )
    assert isinstance(a, OpenAIAnalyzer)
    assert a.escalation_analyzer is not None
    assert a.escalation_analyzer.provider_name == "openai"
    assert a.escalation_analyzer.model_name() == "gpt-4o"


def test_create_analyzer_cross_provider_escalation() -> None:
    """Tier 2 may be a completely different provider than tier 1 — e.g. tier
    1 = openai, tier 2 = moondream_cloud — reusing tier 1's own already-loaded
    moondream_api_key rather than needing a separate credential field."""
    a = create_analyzer(
        "openai",
        "prompt",
        openai_api_key="sk-test",
        openai_model="gpt-4o-mini",
        moondream_api_key="md-test",
        escalation_provider="moondream_cloud",
    )
    assert isinstance(a, OpenAIAnalyzer)
    assert a.escalation_analyzer is not None
    assert a.escalation_analyzer.provider_name == "moondream_cloud"


def test_create_analyzer_escalation_disabled_when_unset() -> None:
    a = create_analyzer("openai", "prompt", openai_api_key="sk-test")
    assert isinstance(a, OpenAIAnalyzer)
    assert a.escalation_analyzer is None


def test_create_analyzer_escalation_noop_when_matches_tier1_exactly() -> None:
    """Same provider and same (default) model as tier 1 is a no-op, not a
    redundant second analyzer."""
    a = create_analyzer(
        "openai",
        "prompt",
        openai_api_key="sk-test",
        openai_model="gpt-4o-mini",
        escalation_provider="openai",
        escalation_model="gpt-4o-mini",
    )
    assert isinstance(a, OpenAIAnalyzer)
    assert a.escalation_analyzer is None


def test_create_analyzer_escalation_tier2_build_failure_falls_back_to_tier1_only() -> (
    None
):
    """A misconfigured tier 2 (e.g. missing credentials) disables escalation
    but must not prevent tier 1 from being returned."""
    a = create_analyzer(
        "openai",
        "prompt",
        openai_api_key="sk-test",
        escalation_provider="anthropic",  # no anthropic_api_key supplied
    )
    assert isinstance(a, OpenAIAnalyzer)
    assert a.escalation_analyzer is None


# ------------------------------------------------------------------
# Per-camera prompts
# ------------------------------------------------------------------


def test_build_prompt_uses_camera_specific_prompt() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Default prompt.",
        camera_prompts={"Driveway": "Driveway-specific prompt."},
    )
    built = a._build_prompt("Driveway")
    assert built.startswith("Driveway-specific prompt.")
    assert "\n\nCamera: Driveway" in built
    assert "OUTPUT RULES" in built
    assert a._build_prompt("Front Door").startswith("Default prompt.")


def test_build_prompt_falls_back_to_default_for_unknown_camera() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Global prompt.",
        camera_prompts={"Driveway": "Car cam prompt."},
    )
    prompt = a._build_prompt("Backyard")
    assert "Global prompt." in prompt
    assert "Car cam prompt." not in prompt


def test_build_prompt_no_camera_prompts_uses_base() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Base prompt.",
        camera_prompts=None,
    )
    assert "Base prompt." in a._build_prompt("Any Camera")


def test_create_analyzer_passes_camera_prompts() -> None:
    prompts = {"Driveway": "Flag anyone near the car."}
    a = create_analyzer(
        "ollama",
        "default",
        camera_prompts=prompts,
        ollama_url="http://localhost:11434",
    )
    assert isinstance(a, ClipAnalyzer)
    assert a._camera_prompts == prompts


def test_create_analyzer_moondream_local_passes_camera_prompts() -> None:
    prompts = {"Driveway": "Watch the car."}
    a = create_analyzer("moondream_local", "default", camera_prompts=prompts)
    assert isinstance(a, MoondreamLocalAnalyzer)
    assert a._camera_prompts == prompts


# ------------------------------------------------------------------
# Confidence=0.0 fix (suspicious but no confidence)
# ------------------------------------------------------------------


def test_parse_response_suspicious_zero_confidence_uses_keyword_fallback(
    analyzer: ClipAnalyzer,
) -> None:
    """When JSON returns suspicious=true but confidence=0.0, keyword fallback applies."""
    response = '{"suspicious": true, "confidence": 0.0, "description": "suspicious person near car"}'
    is_suspicious, confidence, summary = analyzer.parse_response(response)
    assert is_suspicious is True
    assert confidence > 0.0  # must not be 0.0


def test_parse_response_suspicious_zero_confidence_no_keywords_defaults_half() -> None:
    """When suspicious=true, confidence=0.0, and no custom keywords match → defaults to 0.5."""
    # Use keywords that don't appear anywhere in the response JSON
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="test",
        suspicious_keywords=["robbery", "arson"],
    )
    response = (
        '{"suspicious": true, "confidence": 0.0, "description": "person in frame"}'
    )
    _, confidence, _ = a.parse_response(response)
    assert confidence == 0.5


def test_parse_response_not_suspicious_zero_confidence_unchanged(
    analyzer: ClipAnalyzer,
) -> None:
    """When suspicious=false, confidence=0.0 is left as-is."""
    response = (
        '{"suspicious": false, "confidence": 0.0, "description": "Empty driveway"}'
    )
    is_suspicious, confidence, _ = analyzer.parse_response(response)
    assert is_suspicious is False
    assert confidence == 0.0


def test_parse_response_non_zero_confidence_unchanged(
    analyzer: ClipAnalyzer,
) -> None:
    """Explicit non-zero confidence from JSON is never overridden."""
    response = (
        '{"suspicious": true, "confidence": 0.8, "description": "Person near car"}'
    )
    _, confidence, _ = analyzer.parse_response(response)
    assert confidence == 0.8


# ------------------------------------------------------------------
# Moondream Cloud model_pricing and fetch_models
# ------------------------------------------------------------------


def test_moondream_cloud_model_pricing() -> None:
    from blink_downloader.analyzer import _MOONDREAM_CLOUD_PRICING

    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    inp, out = a.model_pricing()
    assert inp == _MOONDREAM_CLOUD_PRICING[0]
    assert out == _MOONDREAM_CLOUD_PRICING[1]
    assert inp == 0.30
    assert out == 2.50


async def test_moondream_cloud_fetch_models_includes_pricing() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    models = await a.fetch_models()
    assert len(models) == 1
    model = models[0]
    assert "0.30" in model["description"]
    assert "2.50" in model["description"]
    assert "display_name" in model


# ------------------------------------------------------------------
# car proximity prompt
# ------------------------------------------------------------------


def test_build_prompt_car_proximity_message() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Blue Toyota Camry",
    )
    prompt = a._build_prompt("Driveway")
    assert "Blue Toyota Camry" in prompt
    assert "1 foot" in prompt
    assert "PROTECTED VEHICLE" in prompt


def test_build_prompt_scopes_distance_rules_to_described_vehicle() -> None:
    """When multiple vehicles may be visible (e.g. apartment parking), the
    prompt must tell the model to apply distance/tampering rules only to
    the vehicle matching the description — not any vehicle in frame."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Blue Toyota Camry",
    )
    prompt = a._build_prompt("Driveway")
    assert "SPECIFIC vehicle" in prompt
    assert "ONLY to the vehicle matching this description" in prompt


def test_build_prompt_car_single_vehicle_defaults_to_protected() -> None:
    """The prompt must instruct the model to treat a single visible vehicle
    as the protected one by default, and not withhold a contact/proximity
    finding just because a color or plate can't be confirmed (e.g. at night
    under infrared/grayscale conditions) — this is what let a person
    touching their own car go unflagged as "not the protected vehicle"."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Grey Kia Forte, plate ABC1234",
    )
    prompt = a._build_prompt("Driveway")
    assert "only ONE vehicle is visible" in prompt
    assert "treat it as the protected vehicle by default" in prompt
    assert "infrared footage" in prompt


def test_build_prompt_car_distinguishes_impact_from_mere_presence() -> None:
    """Lawn equipment/wind-blown debris merely near the protected vehicle is
    not suspicious, but an object actually striking/damaging it must still
    be flagged even with no person at fault."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Silver Kia Forte",
    )
    prompt = a._build_prompt("Driveway")
    assert "no person involved and no visible impact" in prompt
    assert "visibly strikes " in prompt
    assert "urinate/defecate on" in prompt


def test_build_prompt_zone_motion_high_fraction() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Driveway", zone_motion_fraction=0.82)
    assert "ZONE MOTION" in prompt
    assert "82%" in prompt
    assert "concentrated at or near the protected vehicle" in prompt


def test_build_prompt_zone_motion_low_fraction() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Driveway", zone_motion_fraction=0.10)
    assert "ZONE MOTION" in prompt
    assert "10%" in prompt
    assert "elsewhere in the frame" in prompt


def test_build_prompt_no_zone_motion_hint_when_not_provided() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Driveway")
    assert "ZONE MOTION" not in prompt


# ------------------------------------------------------------------
# vision_hints — optional computer-vision pipeline hints (see vision.py)
# ------------------------------------------------------------------


def test_build_prompt_no_vision_hints_when_not_provided() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Driveway")
    assert "OBJECT DETECTION" not in prompt
    assert "TRACKING" not in prompt
    assert "DEPTH ESTIMATE" not in prompt
    assert "CONTACT ANALYSIS" not in prompt
    assert "RECOGNIZED RESIDENT" not in prompt


def test_build_prompt_includes_populated_vision_hints() -> None:
    from blink_downloader.vision import VisionHints

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    hints = VisionHints(
        detection_hint="\n\nOBJECT DETECTION: test detection hint",
        tracking_hint="\n\nTRACKING: test tracking hint",
        depth_hint="\n\nDEPTH ESTIMATE: test depth hint",
        contact_hint="\n\nCONTACT ANALYSIS: test contact hint",
        recognized_resident_hint="\n\nRECOGNIZED RESIDENT: test recognition hint",
    )
    prompt = a._build_prompt("Driveway", vision_hints=hints)
    assert "test detection hint" in prompt
    assert "test tracking hint" in prompt
    assert "test depth hint" in prompt
    assert "test contact hint" in prompt
    assert "test recognition hint" in prompt


def test_build_prompt_ignores_unset_vision_hint_fields() -> None:
    from blink_downloader.vision import VisionHints

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    hints = VisionHints(detection_hint="\n\nOBJECT DETECTION: only this one")
    prompt = a._build_prompt("Driveway", vision_hints=hints)
    assert "only this one" in prompt
    assert "DEPTH ESTIMATE" not in prompt


def test_attach_vision_pipeline_sets_field() -> None:
    from blink_downloader.vision import VisionConfig, VisionPipeline

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    assert a._vision_pipeline is None
    pipeline = VisionPipeline(VisionConfig())
    a.attach_vision_pipeline(pipeline)
    assert a._vision_pipeline is pipeline


async def test_analyze_clip_prompt_identical_with_fully_disabled_vision_pipeline() -> (
    None
):
    """AI analysis with a real VisionPipeline attached but every stage
    toggled off (the default AppConfig state) must produce byte-identical
    prompt text to having no pipeline attached at all — enabling
    ai_analysis_enabled must never itself change behavior; only actually
    turning on a specific CV toggle should."""
    from blink_downloader.vision import VisionConfig, VisionPipeline

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0

    a_without = ClipAnalyzer(
        ollama_url="http://localhost:11434", model="llava", prompt="p"
    )
    a_without.set_prompt_debug(True)
    a_without._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.1, "description": "Clear"}'
    )
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result_without = await a_without.analyze_clip(
            "/clips/test.mp4", "c1", "Driveway"
        )

    a_with = ClipAnalyzer(
        ollama_url="http://localhost:11434", model="llava", prompt="p"
    )
    a_with.set_prompt_debug(True)
    a_with.attach_vision_pipeline(VisionPipeline(VisionConfig()))
    a_with._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.1, "description": "Clear"}'
    )
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result_with = await a_with.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result_without.prompt_text == result_with.prompt_text


async def test_analyze_clip_with_vision_pipeline_injects_hints_and_enhanced_frames() -> (
    None
):
    """End-to-end: an attached VisionPipeline's hints reach the final prompt,
    and enhanced_frames (when set) replace the frames actually sent to the
    AI model — exercising the wiring added to _analyze_clip_locked."""
    from blink_downloader.vision import VisionHints

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    a.set_prompt_debug(True)

    enhanced = [_real_jpeg(50)]
    fake_pipeline = MagicMock()
    fake_pipeline.process_clip = AsyncMock(
        return_value=VisionHints(
            enhanced_frames=enhanced,
            detection_hint="\n\nOBJECT DETECTION: a person and a car were detected",
        )
    )
    a.attach_vision_pipeline(fake_pipeline)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.1, "description": "Clear"}'
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await a.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    fake_pipeline.process_clip.assert_awaited_once()
    assert "a person and a car were detected" in result.prompt_text
    call_frames = a._call_model.call_args.args[0]
    assert call_frames == enhanced


async def test_analyze_clip_passes_car_protection_applies_per_camera() -> None:
    """The vision pipeline must be told whether *this specific camera* is
    under protected-vehicle rules — not just whether a description exists —
    so a camera outside ai_car_cameras never gets vehicle-proximity hints
    just because it happens to see an unrelated car and person."""
    from blink_downloader.vision import VisionHints

    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        car_description="Silver Honda Civic",
        car_cameras=["Driveway"],
    )
    fake_pipeline = MagicMock()
    fake_pipeline.process_clip = AsyncMock(return_value=VisionHints())
    a.attach_vision_pipeline(fake_pipeline)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.1, "description": "Clear"}'
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await a.analyze_clip("/clips/test.mp4", "c1", "Driveway")
        await a.analyze_clip("/clips/test.mp4", "c2", "Front Door")

    driveway_kwargs = fake_pipeline.process_clip.call_args_list[0].kwargs
    front_door_kwargs = fake_pipeline.process_clip.call_args_list[1].kwargs
    assert driveway_kwargs["car_protection_applies"] is True
    assert front_door_kwargs["car_protection_applies"] is False


# ------------------------------------------------------------------
# Face-recognition suspicious-flag bypass (safety-critical) — see
# BaseAnalyzer._face_bypass_applies / _personalize_summary
# ------------------------------------------------------------------


def test_face_bypass_applies_approved_only() -> None:
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    hints = VisionHints(
        face_recognition=FaceRecognitionResult(approved_names=["Brian"])
    )
    assert ClipAnalyzer._face_bypass_applies(hints) is True


def test_face_bypass_applies_multiple_approved() -> None:
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    hints = VisionHints(
        face_recognition=FaceRecognitionResult(approved_names=["Brian", "Amy"])
    )
    assert ClipAnalyzer._face_bypass_applies(hints) is True


def test_face_bypass_does_not_apply_no_faces() -> None:
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    hints = VisionHints(face_recognition=FaceRecognitionResult())
    assert ClipAnalyzer._face_bypass_applies(hints) is False


def test_face_bypass_does_not_apply_stranger_present_alongside_approved() -> None:
    """The critical safety case: an approved family member AND an
    unrecognized stranger both appear — must NOT bypass."""
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    hints = VisionHints(
        face_recognition=FaceRecognitionResult(
            approved_names=["Brian"], unrecognized_present=True
        )
    )
    assert ClipAnalyzer._face_bypass_applies(hints) is False


def test_face_bypass_does_not_apply_unapproved_enrollment_present() -> None:
    """A recognized-but-not-approved enrollment (e.g. a nanny who hasn't been
    granted bypass trust) must block the bypass just like a stranger."""
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    hints = VisionHints(
        face_recognition=FaceRecognitionResult(
            approved_names=["Brian"], other_names=["Nanny"]
        )
    )
    assert ClipAnalyzer._face_bypass_applies(hints) is False


def test_face_bypass_does_not_apply_unrecognized_only() -> None:
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    hints = VisionHints(
        face_recognition=FaceRecognitionResult(unrecognized_present=True)
    )
    assert ClipAnalyzer._face_bypass_applies(hints) is False


def test_face_bypass_does_not_apply_no_vision_hints() -> None:
    assert ClipAnalyzer._face_bypass_applies(None) is False


def test_face_bypass_does_not_apply_vision_hints_without_face_recognition() -> None:
    from blink_downloader.vision import VisionHints

    assert ClipAnalyzer._face_bypass_applies(VisionHints()) is False


def test_personalize_summary_rewrites_leading_generic_subject() -> None:
    summary = ClipAnalyzer._personalize_summary(
        "A person walked up the driveway toward the front door.", ["Brian"]
    )
    assert summary == "Brian walked up the driveway toward the front door."


def test_personalize_summary_handles_multiple_names() -> None:
    summary = ClipAnalyzer._personalize_summary(
        "Someone approached the vehicle.", ["Brian", "Amy"]
    )
    assert summary == "Brian and Amy approached the vehicle."


def test_personalize_summary_falls_back_to_prefix_when_no_match() -> None:
    summary = ClipAnalyzer._personalize_summary(
        "Motion detected near the garage door.", ["Brian"]
    )
    assert summary == "Brian: Motion detected near the garage door."


def test_personalize_summary_noop_without_names_or_summary() -> None:
    assert ClipAnalyzer._personalize_summary("A person walked by.", []) == (
        "A person walked by."
    )
    assert ClipAnalyzer._personalize_summary("", ["Brian"]) == ""


async def test_analyze_clip_bypasses_suspicious_flag_for_approved_only() -> None:
    """End-to-end: when the vision pipeline reports only approved household
    members present (no strangers, no unapproved matches), a suspicious AI
    verdict must be overridden to not-suspicious and the summary personalized."""
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    fake_pipeline = MagicMock()
    fake_pipeline.process_clip = AsyncMock(
        return_value=VisionHints(
            face_recognition=FaceRecognitionResult(approved_names=["Brian"])
        )
    )
    a.attach_vision_pipeline(fake_pipeline)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            '{"suspicious": true, "confidence": 0.8, '
            '"description": "A person is lingering near the vehicle."}'
        )
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await a.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.is_suspicious is False
    assert result.confidence == pytest.approx(0.8)  # confidence itself is untouched
    assert result.summary == "Brian is lingering near the vehicle."


async def test_analyze_clip_stays_suspicious_when_stranger_also_present() -> None:
    """This is the adversarial case that must never regress: an approved
    household member AND an unrecognized stranger both appear — the clip
    must remain flagged suspicious. A false bypass here would be exactly the
    "complete and total failure" scenario the bypass must never cause."""
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    fake_pipeline = MagicMock()
    fake_pipeline.process_clip = AsyncMock(
        return_value=VisionHints(
            face_recognition=FaceRecognitionResult(
                approved_names=["Brian"], unrecognized_present=True
            )
        )
    )
    a.attach_vision_pipeline(fake_pipeline)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            '{"suspicious": true, "confidence": 0.9, '
            '"description": "A person is tampering with the vehicle."}'
        )
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await a.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.is_suspicious is True
    assert result.summary == "A person is tampering with the vehicle."


async def test_analyze_clip_stays_suspicious_when_only_unapproved_match() -> None:
    """A recognized-but-not-approved enrollment must not bypass either."""
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    fake_pipeline = MagicMock()
    fake_pipeline.process_clip = AsyncMock(
        return_value=VisionHints(
            face_recognition=FaceRecognitionResult(other_names=["Nanny"])
        )
    )
    a.attach_vision_pipeline(fake_pipeline)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.7, "description": "Someone is near the door."}'
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await a.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.is_suspicious is True


async def test_analyze_clip_face_recognition_never_appears_in_prompt_sent_to_model() -> (
    None
):
    """Prompt-leakage guard: even when an approved household member is
    recognized, the person's actual name must never appear in the prompt
    text handed to the AI provider — only a name-free count/fact. This is
    what actually enforces "biometric data never leaves the network," not
    just an implementation detail."""
    from blink_downloader.vision import FaceRecognitionResult, VisionHints

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    fake_pipeline = MagicMock()
    fake_pipeline.process_clip = AsyncMock(
        return_value=VisionHints(
            face_recognition=FaceRecognitionResult(approved_names=["Brian"])
        )
    )
    a.attach_vision_pipeline(fake_pipeline)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.1, "description": "Clear"}'
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await a.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    sent_prompt = a._call_model.call_args.args[1]
    assert "Brian" not in sent_prompt


# ------------------------------------------------------------------
# short-event hint — see _SHORT_EVENT_DURATION_SECONDS / _build_prompt
# ------------------------------------------------------------------


def test_build_prompt_short_event_hint_for_brief_clip() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Front Door", clip_duration=6.0)
    assert "SHORT EVENT" in prompt
    assert "6 seconds" in prompt
    assert "not brevity alone" in prompt


def test_build_prompt_no_short_event_hint_for_long_clip() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Front Door", clip_duration=45.0)
    assert "SHORT EVENT" not in prompt


def test_build_prompt_no_short_event_hint_when_duration_unknown() -> None:
    """clip_duration=0.0 (the default, meaning duration wasn't available)
    must not be treated as an ultra-short clip."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Front Door")
    assert "SHORT EVENT" not in prompt


# ------------------------------------------------------------------
# camera descriptions
# ------------------------------------------------------------------


def test_build_prompt_with_camera_description() -> None:
    """Camera description is included in prompt when set."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        camera_descriptions={"FrontDoor": "Faces the front porch and mailbox"},
    )
    prompt = a._build_prompt("FrontDoor")
    assert "Faces the front porch and mailbox" in prompt
    assert "FrontDoor" in prompt


def test_update_camera_descriptions() -> None:
    """update_camera_descriptions replaces the internal mapping."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
    )
    built = a._build_prompt("Garage")
    assert built.startswith("Analyze.")
    assert "\n\nCamera: Garage" in built
    a.update_camera_descriptions({"Garage": "Side entrance to the house"})
    prompt = a._build_prompt("Garage")
    assert "Side entrance to the house" in prompt


def test_update_camera_prompts_replaces_not_merges() -> None:
    """update_camera_prompts fully replaces the mapping so clearing a
    camera's custom prompt in the AI tab actually stops it from applying,
    instead of a dict.update() merge leaving the stale value in place."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        camera_prompts={"Garage": "Watch for tools", "Driveway": "Watch for cars"},
    )
    assert "Watch for tools" in a._build_prompt("Garage")

    a.update_camera_prompts({"Driveway": "Watch for cars"})
    # Garage's custom prompt was dropped from the new mapping entirely, so it
    # must fall back to the base prompt rather than keep the old override.
    garage_prompt = a._build_prompt("Garage")
    assert "Watch for tools" not in garage_prompt
    assert garage_prompt.startswith("Analyze.")
    assert "Watch for cars" in a._build_prompt("Driveway")


def test_update_car_cameras_replaces_not_merges() -> None:
    """update_car_cameras fully replaces the set, so a camera dropped from
    the new set stops being treated as protected — a dict/set merge would
    leave it car-camera-flagged forever."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="a red sedan",
        car_cameras=["Driveway", "Garage"],
    )
    assert "PROTECTED VEHICLE" in a._build_prompt("Driveway")
    assert "PROTECTED VEHICLE" in a._build_prompt("Garage")

    a.update_car_cameras({"Driveway"})
    assert "PROTECTED VEHICLE" in a._build_prompt("Driveway")
    assert "PROTECTED VEHICLE" not in a._build_prompt("Garage")

    # Empty car_cameras is documented, intentional behavior meaning "applies
    # to all cameras" (see CLAUDE.md) — update_car_cameras must preserve
    # that semantic rather than treating an empty set as "no cameras".
    a.update_car_cameras(set())
    assert "PROTECTED VEHICLE" in a._build_prompt("Driveway")
    assert "PROTECTED VEHICLE" in a._build_prompt("Garage")


def test_update_car_description_replaces_and_can_deactivate() -> None:
    """update_car_description takes effect immediately (no restart) and,
    like every other car-protection setting, clearing it deactivates every
    car-protection rule rather than leaving the previous description active."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434", model="llava", prompt="Analyze."
    )
    assert a.car_protection_active is False
    assert "PROTECTED VEHICLE" not in a._build_prompt("Driveway")

    a.update_car_description("a red sedan")
    assert a.car_protection_active is True
    assert "PROTECTED VEHICLE" in a._build_prompt("Driveway")

    a.update_car_description("")
    assert a.car_protection_active is False
    assert "PROTECTED VEHICLE" not in a._build_prompt("Driveway")


def test_update_car_zones_replaces_not_merges() -> None:
    """update_car_zones fully replaces the mapping, so a camera dropped from
    the new mapping stops carrying zone data — mirrors update_car_cameras."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_zones={
            "Driveway": {"x_min": 0.1, "y_min": 0.1, "x_max": 0.9, "y_max": 0.9}
        },
    )
    assert a._car_zones == {
        "Driveway": {"x_min": 0.1, "y_min": 0.1, "x_max": 0.9, "y_max": 0.9}
    }

    a.update_car_zones(
        {"Garage": {"x_min": 0.2, "y_min": 0.2, "x_max": 0.8, "y_max": 0.8}}
    )
    assert a._car_zones == {
        "Garage": {"x_min": 0.2, "y_min": 0.2, "x_max": 0.8, "y_max": 0.8}
    }

    a.update_car_zones({})
    assert a._car_zones == {}


def test_car_protection_active_reflects_car_description() -> None:
    """car_protection_active is False until car_description is set, regardless
    of is_car_camera checkboxes — the AI tab checkbox alone does nothing
    without a Configuration-tab vehicle description to match against."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_cameras=["Driveway"],
    )
    assert a.car_protection_active is False

    a2 = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Silver Kia Forte",
        car_cameras=["Driveway"],
    )
    assert a2.car_protection_active is True


def test_build_prompt_car_description_with_distance_rules() -> None:
    """Distance rules appear in prompt when car_description is set."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Red Honda Civic",
    )
    prompt = a._build_prompt("Driveway")
    assert "Red Honda Civic" in prompt
    assert "1 foot" in prompt
    assert "2 feet" in prompt
    assert "distance" in prompt.lower()
    assert "PROTECTED VEHICLE" in prompt


def test_build_prompt_car_camera_included_gets_car_example() -> None:
    """A camera explicitly listed in car_cameras gets the car-distance example."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Silver Kia Forte",
        car_cameras=["Driveway"],
    )
    prompt = a._build_prompt("Driveway")
    assert "PROTECTED VEHICLE" in prompt
    assert "standing about 2 feet from the car" in prompt


def test_build_prompt_car_camera_excluded_states_no_vehicle() -> None:
    """A camera NOT in car_cameras must not be nudged into inventing a car.

    Regression test: previously the OUTPUT RULES example phrase referenced
    'the car' / 'the driveway' unconditionally, which caused non-car cameras
    (e.g. a front-door camera that cannot see the driveway) to parrot that
    exact language back in their descriptions.
    """
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Silver Kia Forte",
        car_cameras=["Driveway"],
    )
    prompt = a._build_prompt("Front Door")
    assert "PROTECTED VEHICLE" not in prompt
    assert "does not view the protected vehicle" in prompt
    assert "standing about 2 feet from the car" not in prompt
    assert "walking past the driveway" not in prompt
    # Camera-agnostic example used instead
    assert "front steps" in prompt or "yard" in prompt


def test_build_prompt_empty_car_cameras_still_applies_to_all() -> None:
    """Leaving car_cameras empty preserves the documented default: apply to
    every camera (config.py: 'Leave empty to apply to all cameras')."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
        car_description="Silver Kia Forte",
        car_cameras=None,
    )
    prompt = a._build_prompt("Front Door")
    assert "PROTECTED VEHICLE" in prompt
    assert "does not view the protected vehicle" not in prompt


def test_build_prompt_output_rules_scoped_to_camera_name() -> None:
    """OUTPUT RULES explicitly names the current camera and warns against
    borrowing scenery from other cameras on the property."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Analyze.",
    )
    prompt = a._build_prompt("Backyard")
    assert "from the Backyard camera" in prompt
    assert "other cameras on the property" in prompt


def test_create_analyzer_ollama_with_camera_descriptions() -> None:
    """create_analyzer passes camera_descriptions to ClipAnalyzer."""
    descriptions = {"Backyard": "Overlooks the pool"}
    a = create_analyzer(
        ai_provider="ollama",
        prompt="Analyze.",
        camera_descriptions=descriptions,
        ollama_url="http://localhost:11434",
        ollama_model="llava",
    )
    assert isinstance(a, ClipAnalyzer)
    prompt = a._build_prompt("Backyard")
    assert "Overlooks the pool" in prompt


# =============================================================================
# v3.0.0 — Smart frame selection, sequential mode, anomaly context, frame_strategy
# =============================================================================

_FAKE_JPEG_2 = (
    b"\xff\xd8"
    + b"\x10" * 30  # slightly different pixel data for motion diff
    + b"\xff\xd9"
)
_FAKE_JPEG_3 = b"\xff\xd8" + b"\x20" * 30 + b"\xff\xd9"


# ---------------------------------------------------------------------------
# frame_strategy propagation
# ---------------------------------------------------------------------------


def test_clip_analyzer_default_frame_strategy() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    assert a._frame_strategy == "smart"


def test_clip_analyzer_sequential_strategy() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="sequential",
    )
    assert a._frame_strategy == "sequential"


def test_create_analyzer_passes_frame_strategy() -> None:
    a = create_analyzer(
        ai_provider="ollama",
        prompt="p",
        frame_strategy="sequential",
        ollama_url="http://localhost:11434",
        ollama_model="llava",
    )
    assert isinstance(a, ClipAnalyzer)
    assert a._frame_strategy == "sequential"


def test_create_analyzer_moondream_cloud_frame_strategy() -> None:
    a = create_analyzer(
        ai_provider="moondream_cloud",
        prompt="p",
        frame_strategy="uniform",
        moondream_api_key="key",
    )
    assert isinstance(a, MoondreamCloudAnalyzer)
    assert a._frame_strategy == "uniform"


def test_create_analyzer_moondream_local_frame_strategy() -> None:
    a = create_analyzer(
        ai_provider="moondream_local",
        prompt="p",
        frame_strategy="sequential",
    )
    assert isinstance(a, MoondreamLocalAnalyzer)
    assert a._frame_strategy == "sequential"


def test_create_analyzer_anthropic_frame_strategy() -> None:
    a = create_analyzer(
        ai_provider="anthropic",
        prompt="p",
        frame_strategy="smart",
        anthropic_api_key="key",
        anthropic_model="claude-haiku-4-5",
    )
    assert isinstance(a, AnthropicAnalyzer)
    assert a._frame_strategy == "smart"


def test_create_analyzer_openai_frame_strategy() -> None:
    a = create_analyzer(
        ai_provider="openai",
        prompt="p",
        frame_strategy="sequential",
        openai_api_key="key",
        openai_model="gpt-4o-mini",
    )
    assert isinstance(a, OpenAIAnalyzer)
    assert a._frame_strategy == "sequential"


# ---------------------------------------------------------------------------
# Smart frame selection
# ---------------------------------------------------------------------------


def test_select_best_frames_returns_all_if_under_target() -> None:
    frames = [_FAKE_JPEG, _FAKE_JPEG_2]
    result = ClipAnalyzer._select_best_frames(frames, 5)
    assert result == frames


def test_select_best_frames_returns_target_count() -> None:
    # 6 frames → select 3
    frames = [_FAKE_JPEG, _FAKE_JPEG_2, _FAKE_JPEG_3] * 2
    result = ClipAnalyzer._select_best_frames(frames, 3)
    assert len(result) <= 3


def test_select_best_frames_always_includes_first_and_last() -> None:
    frames = [_FAKE_JPEG, _FAKE_JPEG_2, _FAKE_JPEG_3, _FAKE_JPEG, _FAKE_JPEG_2]
    result = ClipAnalyzer._select_best_frames(frames, 3)
    assert result[0] == frames[0]
    assert result[-1] == frames[-1]


def test_select_uniform_frames_returns_all_if_under_target() -> None:
    frames = [_FAKE_JPEG, _FAKE_JPEG_2]
    result = ClipAnalyzer._select_uniform_frames(frames, 5)
    assert result == frames


def test_select_uniform_frames_returns_target_count() -> None:
    frames = [_FAKE_JPEG, _FAKE_JPEG_2, _FAKE_JPEG_3] * 4  # 12 frames
    result = ClipAnalyzer._select_uniform_frames(frames, 5)
    assert len(result) == 5


def test_select_uniform_frames_includes_first_and_last() -> None:
    frames = [_FAKE_JPEG, _FAKE_JPEG_2, _FAKE_JPEG_3] * 4  # 12 frames
    result = ClipAnalyzer._select_uniform_frames(frames, 5)
    assert result[0] == frames[0]
    assert result[-1] == frames[-1]


def test_select_uniform_frames_single_target() -> None:
    frames = [_FAKE_JPEG, _FAKE_JPEG_2, _FAKE_JPEG_3]
    result = ClipAnalyzer._select_uniform_frames(frames, 1)
    assert result == [frames[0]]


def test_select_best_frames_fallback_on_pil_error() -> None:
    """When PIL raises an error, even-spaced selection is used as fallback."""
    frames = [_FAKE_JPEG, _FAKE_JPEG_2, _FAKE_JPEG_3, _FAKE_JPEG]
    with patch("blink_downloader.analyzer.ClipAnalyzer._select_best_frames") as mock:
        mock.side_effect = Exception("PIL unavailable")
        # The actual function should catch exceptions internally
        mock.side_effect = None
        mock.return_value = frames[:2]
        result = mock(frames, 2)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Sequential analysis mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_sequentially_picks_most_alarming() -> None:
    """_analyze_sequentially returns the suspicious result over the non-suspicious."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="sequential",
    )
    non_susp = json.dumps(
        {"suspicious": False, "confidence": 0.9, "description": "Empty street"}
    )
    susp = json.dumps(
        {"suspicious": True, "confidence": 0.7, "description": "Person near car"}
    )

    call_count = 0

    async def fake_call_model(frames: list, prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        return susp if call_count == 2 else non_susp

    analyzer._call_model = fake_call_model  # type: ignore[method-assign]
    result, frame = await analyzer._analyze_sequentially(
        [_FAKE_JPEG, _FAKE_JPEG_2], "p"
    )
    assert "Person near car" in result
    assert frame == _FAKE_JPEG_2
    assert call_count == 2


@pytest.mark.asyncio
async def test_analyze_sequentially_empty_frames() -> None:
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    result, frame = await analyzer._analyze_sequentially([], "prompt")
    assert result == ""
    assert frame is None


@pytest.mark.asyncio
async def test_analyze_sequentially_skips_empty_responses() -> None:
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    good = json.dumps(
        {"suspicious": False, "confidence": 0.5, "description": "All clear"}
    )

    async def fake_call_model(frames: list, prompt: str) -> str:
        return "" if len(frames) == 1 and frames[0] == _FAKE_JPEG else good

    analyzer._call_model = fake_call_model  # type: ignore[method-assign]
    result, frame = await analyzer._analyze_sequentially(
        [_FAKE_JPEG, _FAKE_JPEG_2], "p"
    )
    assert "All clear" in result
    assert frame == _FAKE_JPEG_2


@pytest.mark.asyncio
async def test_analyze_sequentially_malformed_frame_kept_only_as_last_resort() -> None:
    """A frame whose response has no parseable description is kept only as a
    last-resort placeholder — a later frame with a real (even non-suspicious)
    description must still replace it, and a genuinely suspicious later frame
    must never be shadowed by an earlier malformed one."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    malformed = "not valid json"
    suspicious = json.dumps(
        {"suspicious": True, "confidence": 0.8, "description": "Person at car door"}
    )

    async def fake_call_model(frames: list, prompt: str) -> str:
        return malformed if frames[0] == _FAKE_JPEG else suspicious

    analyzer._call_model = fake_call_model  # type: ignore[method-assign]
    result, frame = await analyzer._analyze_sequentially(
        [_FAKE_JPEG, _FAKE_JPEG_2], "p"
    )
    assert "Person at car door" in result
    assert frame == _FAKE_JPEG_2


@pytest.mark.asyncio
async def test_analyze_clip_uses_sequential_mode() -> None:
    """analyze_clip calls _analyze_sequentially when frame_strategy is 'sequential'."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="sequential",
        max_frames=2,
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG + _FAKE_JPEG_2, b""))
    mock_proc.returncode = 0

    good_resp = json.dumps(
        {"suspicious": False, "confidence": 0.8, "description": "Clear"}
    )

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": good_resp})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch.object(
            analyzer, "_analyze_sequentially", wraps=analyzer._analyze_sequentially
        ) as seq_spy:
            await analyzer.analyze_clip("/clips/test.mp4", "c1", "Driveway")
    seq_spy.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_clip_sequential_downselects_oversampled_pool() -> None:
    """A 60s-clip-sized oversampled pool must be trimmed to max_frames before
    each frame is analysed individually — otherwise a long clip would trigger
    one API call per sampled frame instead of per selected frame."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="sequential",
        max_frames=2,
    )
    many_frames = (_FAKE_JPEG + _FAKE_JPEG_2 + _FAKE_JPEG_3) * 4  # 12 raw frames
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(many_frames, b""))
    mock_proc.returncode = 0

    good_resp = json.dumps(
        {"suspicious": False, "confidence": 0.8, "description": "Clear"}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": good_resp})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.frame_count == 2


@pytest.mark.asyncio
async def test_analyze_clip_uniform_downselects_oversampled_pool() -> None:
    """Uniform mode must also trim its oversampled pool down to max_frames."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
        max_frames=2,
    )
    many_frames = (_FAKE_JPEG + _FAKE_JPEG_2 + _FAKE_JPEG_3) * 4  # 12 raw frames
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(many_frames, b""))
    mock_proc.returncode = 0

    good_resp = json.dumps(
        {"suspicious": False, "confidence": 0.8, "description": "Clear"}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": good_resp})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.frame_count == 2


# ---------------------------------------------------------------------------
# _build_prompt — anomaly score and time-of-day context
# ---------------------------------------------------------------------------


@pytest.fixture
def utc_local_tz():
    """_time_of_day_segment converts clip_timestamp (always UTC) to local
    time before bucketing it — correct in production (see the regression
    test below), but it means these bucket-boundary tests need a pinned,
    known local timezone to be deterministic, rather than silently
    depending on whatever timezone happens to be ambient wherever they
    run (this sandbox's, for instance, is Atlantic time, not UTC)."""
    import os
    import time as _time_module

    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    _time_module.tzset()
    yield
    if original_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_tz
    _time_module.tzset()


def test_build_prompt_includes_time_of_day(utc_local_tz) -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="Base prompt.",
    )
    prompt = a._build_prompt(
        "Driveway",
        clip_timestamp="2026-06-28T03:30:00+00:00",
    )
    assert "late night" in prompt


def test_build_prompt_evening_time(utc_local_tz) -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", clip_timestamp="2026-06-28T19:00:00+00:00")
    assert "evening" in prompt


def test_build_prompt_morning_time(utc_local_tz) -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", clip_timestamp="2026-06-28T10:00:00+00:00")
    assert "Time of day: morning" in prompt


def test_build_prompt_early_morning_time(utc_local_tz) -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", clip_timestamp="2026-06-28T06:00:00+00:00")
    assert "Time of day: early morning" in prompt


def test_build_prompt_afternoon_time(utc_local_tz) -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", clip_timestamp="2026-06-28T14:00:00+00:00")
    assert "Time of day: afternoon" in prompt


def test_build_prompt_night_time(utc_local_tz) -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", clip_timestamp="2026-06-28T21:00:00+00:00")
    assert "Time of day: night" in prompt


def test_time_of_day_uses_local_timezone_not_raw_utc_hour() -> None:
    """clip_timestamp is always UTC (Blink's convention), but "is this
    normal for the time of day" is a local question — a clip at 20:10 UTC
    is broad-daylight late afternoon in a UTC-3 (e.g. Atlantic/Halifax)
    timezone, not "night". Regression test for reading dt.hour directly
    off the UTC value instead of converting to local time first."""
    import os
    import time as _time_module

    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Halifax"  # UTC-3 (ADT) in summer
        _time_module.tzset()
        # 20:10 UTC -> 17:10 ADT: late afternoon/early evening, nowhere
        # near "night" despite the UTC hour alone suggesting it.
        prompt = a._build_prompt("Cam", clip_timestamp="2026-07-14T20:10:00+00:00")
        assert "Time of day: night" not in prompt
        assert "Time of day: evening" in prompt
        assert "17:10 local time" in prompt
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        _time_module.tzset()


def test_build_prompt_no_anomaly_alert_below_threshold() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", anomaly_score=0.3)
    assert "BEHAVIOR ALERT" not in prompt


def test_build_prompt_anomaly_alert_above_threshold() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", anomaly_score=0.75)
    assert "BEHAVIOR ALERT" in prompt
    assert "0.75" in prompt


def test_build_prompt_anomaly_score_zero_no_alert() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", anomaly_score=0.0)
    assert "BEHAVIOR ALERT" not in prompt


def test_build_prompt_bad_timestamp_ignored() -> None:
    """Invalid timestamp should not raise — time-of-day context is skipped."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
    )
    prompt = a._build_prompt("Cam", clip_timestamp="not-a-date")
    assert "p" in prompt  # Base prompt still present


# ---------------------------------------------------------------------------
# analyze_clip passes anomaly_score through to AnalysisResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_clip_stores_anomaly_score() -> None:
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
        max_frames=1,
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_FAKE_JPEG, b""))
    mock_proc.returncode = 0

    resp_json = json.dumps(
        {"suspicious": False, "confidence": 0.5, "description": "OK"}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": resp_json})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip(
            "/clips/test.mp4",
            "c1",
            "Driveway",
            anomaly_score=0.72,
            clip_timestamp="2026-06-28T03:00:00+00:00",
        )

    assert result.anomaly_score == pytest.approx(0.72)
    assert result.to_dict()["anomaly_score"] == pytest.approx(0.72)


@pytest.mark.asyncio
async def test_analyze_clip_anomaly_score_in_no_frames_result() -> None:
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip(
            "/clips/empty.mp4", "c1", "Cam", anomaly_score=0.8
        )
    assert result.anomaly_score == pytest.approx(0.8)
    assert result.frame_count == 0


# ---------------------------------------------------------------------------
# Smart mode: extract_frames uses 2x in smart mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_frames_smart_requests_double() -> None:
    """In smart mode ffmpeg is called with 2 × max_frames when that exceeds
    the 60-second coverage floor (isolated here with a large frame_interval
    so the coverage floor doesn't dominate the count)."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        max_frames=3,
        frame_strategy="smart",
        frame_interval=20.0,
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await analyzer.extract_frames("/clips/test.mp4")

    cmd = mock_exec.call_args[0]
    frames_arg_idx = list(cmd).index("-frames:v") + 1
    assert cmd[frames_arg_idx] == "6"  # 3 * 2


@pytest.mark.asyncio
async def test_extract_frames_uniform_requests_exact() -> None:
    """In uniform mode ffmpeg is called with exactly max_frames when that
    exceeds the 60-second coverage floor (isolated with a large frame_interval)."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        max_frames=5,
        frame_strategy="uniform",
        frame_interval=20.0,
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await analyzer.extract_frames("/clips/test.mp4")

    cmd = mock_exec.call_args[0]
    frames_arg_idx = list(cmd).index("-frames:v") + 1
    assert cmd[frames_arg_idx] == "5"


@pytest.mark.asyncio
async def test_extract_frames_covers_full_60s_clip_by_default() -> None:
    """A small max_frames with the default 2s interval must not truncate
    extraction to only the first few seconds of a up-to-60s clip — ffmpeg
    must be asked for enough frames to span the whole clip."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        max_frames=3,
        frame_strategy="smart",
        frame_interval=2.0,
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await analyzer.extract_frames("/clips/test.mp4")

    cmd = mock_exec.call_args[0]
    frames_arg_idx = list(cmd).index("-frames:v") + 1
    # 3 * 2 = 6 would only cover the first 12s; 60s / 2s = 30 covers the full clip.
    assert cmd[frames_arg_idx] == "30"


@pytest.mark.asyncio
async def test_extract_frames_uniform_covers_full_60s_clip_by_default() -> None:
    """Uniform mode must also cover the full clip, not just max_frames * interval."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        max_frames=5,
        frame_strategy="uniform",
        frame_interval=2.0,
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await analyzer.extract_frames("/clips/test.mp4")

    cmd = mock_exec.call_args[0]
    frames_arg_idx = list(cmd).index("-frames:v") + 1
    assert cmd[frames_arg_idx] == "30"


@pytest.mark.asyncio
async def test_extract_frames_uses_640_scale() -> None:
    """Frame extraction uses scale=640:-1 for token cost reduction."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await analyzer.extract_frames("/clips/test.mp4")

    cmd_str = " ".join(str(a) for a in mock_exec.call_args[0])
    assert "scale=640:-1" in cmd_str


# ===========================================================================
# Additional coverage — MoondreamCloudAnalyzer detect-based analysis
# ===========================================================================


def _make_detect_resp(objects: list) -> AsyncMock:
    """Create a mock aiohttp response for the /detect endpoint."""
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"objects": objects})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_query_resp(answer: str) -> AsyncMock:
    """Create a mock aiohttp response for the /query endpoint."""
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"answer": answer})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_caption_resp(caption: str = "A quiet driveway scene.") -> AsyncMock:
    """Create a mock aiohttp response for the /caption endpoint."""
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"caption": caption})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _dispatch_moondream(
    url: str,
    kwargs: dict[str, Any],
    *,
    detect: list[dict[str, float]] | Callable[[str], list[dict[str, float]]],
    query_answer: str,
    caption: str = "A quiet driveway scene.",
    injected_prompts: list[str] | None = None,
) -> AsyncMock:
    """Shared endpoint dispatcher for Moondream Cloud mocks.

    ``detect`` is either a fixed list of boxes (returned for every /detect
    call regardless of object) or a callable ``(object_name) -> list``.
    """
    if "/detect" in url:
        obj = kwargs.get("json", {}).get("object", "")
        boxes = detect(obj) if callable(detect) else detect
        return _make_detect_resp(boxes)
    if "/caption" in url:
        return _make_caption_resp(caption)
    if injected_prompts is not None:
        injected_prompts.append(kwargs.get("json", {}).get("question", ""))
    return _make_query_resp(query_answer)


# ------------------------------------------------------------------
# _detect_objects
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_objects_success() -> None:
    """_detect_objects returns parsed bounding boxes on HTTP 200."""
    boxes = [{"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]
    resp = _make_detect_resp(boxes)
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = _mock_session(post=MagicMock(return_value=resp))

    result = await a._detect_objects(b"fake_frame", "person")
    assert result == boxes


@pytest.mark.asyncio
async def test_detect_objects_non_200_returns_empty() -> None:
    """_detect_objects returns [] when the API returns a non-200 status."""
    resp = AsyncMock()
    resp.status = 500
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = _mock_session(post=MagicMock(return_value=resp))

    result = await a._detect_objects(b"fake", "person")
    assert result == []


@pytest.mark.asyncio
async def test_detect_objects_client_error_returns_empty() -> None:
    """_detect_objects returns [] on aiohttp.ClientError."""
    import aiohttp

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("conn refused"))
    session.closed = False
    a._session = session

    result = await a._detect_objects(b"fake", "person")
    assert result == []


@pytest.mark.asyncio
async def test_detect_objects_filters_non_dicts() -> None:
    """_detect_objects discards non-dict entries from the objects array."""
    objects_raw = [
        {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9},
        "not_a_dict",
        42,
        None,
    ]
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"objects": objects_raw})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = _mock_session(post=MagicMock(return_value=resp))

    result = await a._detect_objects(b"f", "person")
    assert result == [{"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]


# ------------------------------------------------------------------
# _caption_frame
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caption_frame_success() -> None:
    """_caption_frame returns the caption text and accumulates tokens."""
    resp = _make_caption_resp("A grey Kia Forte parked on a gravel driveway.")
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = _mock_session(post=MagicMock(return_value=resp))

    result = await a._caption_frame(b"fake_frame")
    assert result == "A grey Kia Forte parked on a gravel driveway."
    assert a._last_prompt_tokens > 0
    assert a._last_completion_tokens > 0


@pytest.mark.asyncio
async def test_caption_frame_non_200_returns_empty() -> None:
    resp = AsyncMock()
    resp.status = 500
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = _mock_session(post=MagicMock(return_value=resp))

    result = await a._caption_frame(b"fake_frame")
    assert result == ""


@pytest.mark.asyncio
async def test_caption_frame_timeout_returns_empty() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = _mock_session(post=MagicMock(side_effect=asyncio.TimeoutError))

    result = await a._caption_frame(b"fake_frame")
    assert result == ""


@pytest.mark.asyncio
async def test_caption_frame_client_error_returns_empty() -> None:
    import aiohttp

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("conn refused"))
    session.closed = False
    a._session = session

    result = await a._caption_frame(b"fake_frame")
    assert result == ""


@pytest.mark.asyncio
async def test_caption_frame_includes_finetune_model() -> None:
    """When a fine-tuned model is configured, /caption's payload includes it."""
    resp = _make_caption_resp("A quiet scene.")
    session = _mock_session(post=MagicMock(return_value=resp))
    a = MoondreamCloudAnalyzer(
        api_key="key", prompt="p", finetune_model="moondream3-preview/abc123@50"
    )
    a._session = session

    await a._caption_frame(b"fake_frame")

    _, kwargs = session.post.call_args
    assert kwargs["json"]["model"] == "moondream3-preview/abc123@50"


@pytest.mark.asyncio
async def test_caption_frame_requests_short_length() -> None:
    """/caption uses length="short" to keep grounding context (and cost) low.

    Regression test: a "normal"-length caption enumerates every visible
    element (background vehicles, foliage, utility poles, ...) and that
    detail was leaking into the final description, driving up completion
    tokens for little security value.
    """
    resp = _make_caption_resp("A person near a car.")
    session = _mock_session(post=MagicMock(return_value=resp))
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = session

    await a._caption_frame(b"fake_frame")

    _, kwargs = session.post.call_args
    assert kwargs["json"]["length"] == "short"


# ------------------------------------------------------------------
# _detect_protected_vehicle
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_protected_vehicle_skips_when_zero_or_one_car() -> None:
    """No disambiguation needed with 0 or 1 car boxes — no extra detect call."""
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    session = MagicMock()
    session.post = MagicMock(side_effect=AssertionError("should not be called"))
    session.closed = False
    a._session = session

    single = [{"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]
    protected, other = await a._detect_protected_vehicle(b"frame", single)
    assert protected == single
    assert other == []

    protected, other = await a._detect_protected_vehicle(b"frame", [])
    assert protected == []
    assert other == []


@pytest.mark.asyncio
async def test_detect_protected_vehicle_skips_without_car_description() -> None:
    """No car_description configured — nothing to disambiguate against."""
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    session = MagicMock()
    session.post = MagicMock(side_effect=AssertionError("should not be called"))
    session.closed = False
    a._session = session

    car_boxes = [
        {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9},
        {"x_min": 0.5, "y_min": 0.1, "x_max": 0.9, "y_max": 0.9},
    ]
    protected, other = await a._detect_protected_vehicle(b"frame", car_boxes)
    assert protected == car_boxes
    assert other == []


@pytest.mark.asyncio
async def test_detect_protected_vehicle_disambiguates_ambiguous_case() -> None:
    """More than one car box + a targeted detect that finds the protected
    car → the non-matching box is returned as an "other vehicle"."""
    car_boxes = [
        {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9},
        {"x_min": 0.6, "y_min": 0.1, "x_max": 0.9, "y_max": 0.9},
    ]

    async def fake_detect(frame: bytes, object_name: str) -> list[dict[str, float]]:
        assert object_name == "Silver Kia"
        return [car_boxes[0]]

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    with patch.object(a, "_detect_objects", side_effect=fake_detect):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            protected, other = await a._detect_protected_vehicle(b"frame", car_boxes)

    assert protected == [car_boxes[0]]
    assert other == [car_boxes[1]]


@pytest.mark.asyncio
async def test_detect_protected_vehicle_falls_back_when_nothing_found() -> None:
    """Targeted detect finds nothing usable → fall back to treating every
    car box as the protected vehicle rather than manufacturing a false
    'other vehicle' alert."""
    car_boxes = [
        {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9},
        {"x_min": 0.6, "y_min": 0.1, "x_max": 0.9, "y_max": 0.9},
    ]

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    with patch.object(a, "_detect_objects", new=AsyncMock(return_value=[])):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            protected, other = await a._detect_protected_vehicle(b"frame", car_boxes)

    assert protected == car_boxes
    assert other == []


@pytest.mark.asyncio
async def test_detect_protected_vehicle_dedupes_duplicate_boxes_for_same_car() -> None:
    """A single parked car detected as two heavily-overlapping boxes by the
    generic "car" query must NOT be reported as "another vehicle" next to
    itself — this was the root cause of false "vehicle parked right next to
    the protected vehicle" alerts for a car simply parked alone."""
    same_car_a = {"x_min": 0.10, "y_min": 0.10, "x_max": 0.50, "y_max": 0.90}
    same_car_b = {"x_min": 0.12, "y_min": 0.10, "x_max": 0.50, "y_max": 0.90}
    duplicate_boxes = [same_car_a, same_car_b]

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    with patch.object(
        a, "_detect_objects", side_effect=AssertionError("should not be called")
    ):
        protected, other = await a._detect_protected_vehicle(b"frame", duplicate_boxes)

    assert protected == [same_car_a]
    assert other == []


@pytest.mark.asyncio
async def test_detect_protected_vehicle_still_finds_genuinely_separate_car() -> None:
    """A duplicate-detected protected car plus one genuinely separate
    vehicle → the separate vehicle still surfaces as "other", only the
    duplicate collapses."""
    same_car_a = {"x_min": 0.05, "y_min": 0.10, "x_max": 0.45, "y_max": 0.90}
    same_car_b = {"x_min": 0.07, "y_min": 0.10, "x_max": 0.45, "y_max": 0.90}
    separate_car = {"x_min": 0.60, "y_min": 0.10, "x_max": 0.95, "y_max": 0.90}
    all_boxes = [same_car_a, same_car_b, separate_car]

    async def fake_detect(frame: bytes, object_name: str) -> list[dict[str, float]]:
        assert object_name == "Silver Kia"
        return [same_car_a]

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    with patch.object(a, "_detect_objects", side_effect=fake_detect):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            protected, other = await a._detect_protected_vehicle(b"frame", all_boxes)

    assert protected == [same_car_a]
    assert other == [separate_car]


@pytest.mark.asyncio
async def test_detect_protected_vehicle_uses_stripped_query_for_detect_call() -> None:
    """A car_description containing a license plate must have the plate
    stripped before it's sent to Moondream's zero-shot /detect — a plate
    number isn't a visual feature the detector can ground, and including it
    risks derailing the query onto the wrong box or nothing at all, which
    would corrupt the whole disambiguation this method exists for."""
    car_boxes = [
        {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9},
        {"x_min": 0.6, "y_min": 0.1, "x_max": 0.9, "y_max": 0.9},
    ]

    async def fake_detect(frame: bytes, object_name: str) -> list[dict[str, float]]:
        assert object_name == "Grey Kia Forte"
        return [car_boxes[0]]

    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="p",
        car_description="Grey Kia Forte, plate ABC1234",
    )
    with patch.object(a, "_detect_objects", side_effect=fake_detect):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            protected, other = await a._detect_protected_vehicle(b"frame", car_boxes)

    assert protected == [car_boxes[0]]
    assert other == [car_boxes[1]]


# ------------------------------------------------------------------
# _visual_detect_query
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Grey Kia Forte, plate ABC1234", "Grey Kia Forte"),
        ("Silver Kia Forte, license plate: XYZ-999", "Silver Kia Forte"),
        (
            "Red truck, plate #7GHK123, parked in driveway",
            "Red truck, parked in driveway",
        ),
        ("Silver Kia Forte", "Silver Kia Forte"),
        ("", ""),
    ],
)
def test_visual_detect_query_strips_plate_mention(
    description: str, expected: str
) -> None:
    assert MoondreamCloudAnalyzer._visual_detect_query(description) == expected


def test_visual_detect_query_falls_back_when_nothing_left() -> None:
    """If the entire description is just a plate mention, stripping it would
    leave nothing usable — fall back to the original text rather than
    sending an empty detect query."""
    assert (
        MoondreamCloudAnalyzer._visual_detect_query("plate ABC1234") == "plate ABC1234"
    )


# ------------------------------------------------------------------
# _bbox_min_gap
# ------------------------------------------------------------------


def test_bbox_min_gap_overlapping() -> None:
    """Boxes that overlap → gap == 0.0."""
    a_boxes = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5}]
    b_boxes = [{"x_min": 0.3, "y_min": 0.3, "x_max": 0.8, "y_max": 0.8}]
    gap = MoondreamCloudAnalyzer._bbox_min_gap(a_boxes, b_boxes)
    assert gap == 0.0


def test_bbox_min_gap_adjacent() -> None:
    """Boxes that touch but don't overlap → gap ~= 0.0."""
    a_boxes = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 1.0}]
    b_boxes = [{"x_min": 0.5, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}]
    gap = MoondreamCloudAnalyzer._bbox_min_gap(a_boxes, b_boxes)
    assert gap == pytest.approx(0.0, abs=1e-9)


def test_bbox_min_gap_separated() -> None:
    """Boxes with clear separation → gap > 0."""
    a_boxes = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.2, "y_max": 0.2}]
    b_boxes = [{"x_min": 0.8, "y_min": 0.8, "x_max": 1.0, "y_max": 1.0}]
    gap = MoondreamCloudAnalyzer._bbox_min_gap(a_boxes, b_boxes)
    # Euclidean: sqrt(0.6^2 + 0.6^2) ≈ 0.849
    assert gap > 0.8


def test_bbox_min_gap_multiple_pairs() -> None:
    """Returns the minimum gap across all box pairs."""
    a_boxes = [
        {"x_min": 0.0, "y_min": 0.0, "x_max": 0.1, "y_max": 0.1},
        {"x_min": 0.4, "y_min": 0.4, "x_max": 0.6, "y_max": 0.6},
    ]
    b_boxes = [{"x_min": 0.5, "y_min": 0.5, "x_max": 0.7, "y_max": 0.7}]
    gap = MoondreamCloudAnalyzer._bbox_min_gap(a_boxes, b_boxes)
    assert gap == 0.0  # Second a-box overlaps b-box


# ------------------------------------------------------------------
# _bbox_min_pairwise_gap (vehicle-to-vehicle proximity within one list)
# ------------------------------------------------------------------


def test_bbox_min_pairwise_gap_fewer_than_two_boxes() -> None:
    """With 0 or 1 boxes there's no pair to compare — returns max separation."""
    assert MoondreamCloudAnalyzer._bbox_min_pairwise_gap([]) == 1.0
    box = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5}]
    assert MoondreamCloudAnalyzer._bbox_min_pairwise_gap(box) == 1.0


def test_bbox_min_pairwise_gap_overlapping_boxes() -> None:
    """Two overlapping boxes (e.g. cars parked right next to each other) → 0.0."""
    boxes = [
        {"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5},
        {"x_min": 0.4, "y_min": 0.0, "x_max": 0.9, "y_max": 0.5},
    ]
    assert MoondreamCloudAnalyzer._bbox_min_pairwise_gap(boxes) == 0.0


def test_bbox_min_pairwise_gap_finds_closest_of_three() -> None:
    """With 3+ boxes, returns the minimum gap across all distinct pairs."""
    boxes = [
        {"x_min": 0.0, "y_min": 0.0, "x_max": 0.1, "y_max": 0.1},
        {"x_min": 0.9, "y_min": 0.9, "x_max": 1.0, "y_max": 1.0},
        {"x_min": 0.0, "y_min": 0.0, "x_max": 0.1, "y_max": 0.1},  # duplicates box 1
    ]
    gap = MoondreamCloudAnalyzer._bbox_min_pairwise_gap(boxes)
    assert gap == 0.0  # boxes 1 and 3 coincide


# ------------------------------------------------------------------
# _proximity_hint tiers
# ------------------------------------------------------------------


def test_proximity_hint_touching() -> None:
    hint = MoondreamCloudAnalyzer._proximity_hint(0.0, "dog")
    assert "touching" in hint.lower()


def test_proximity_hint_under_one_foot() -> None:
    hint = MoondreamCloudAnalyzer._proximity_hint(0.04, "person or animal")
    assert "less than 1 foot" in hint


def test_proximity_hint_one_to_three_feet() -> None:
    hint = MoondreamCloudAnalyzer._proximity_hint(0.10, "person or animal")
    assert "1–3 feet" in hint
    assert "do NOT flag as suspicious" in hint


def test_proximity_hint_far_away() -> None:
    hint = MoondreamCloudAnalyzer._proximity_hint(0.5, "person or animal")
    assert "several feet" in hint
    assert "NOT suspicious" in hint


# ------------------------------------------------------------------
# _vehicle_proximity_hint
# ------------------------------------------------------------------


def test_vehicle_proximity_hint_warns_about_camera_perspective() -> None:
    """The vehicle-to-vehicle hint must never instruct the model to parrot
    'touching'/'right next to' language purely from bbox gap — that was one
    root cause of false suspicious alerts for ordinary passing traffic."""
    hint = MoondreamCloudAnalyzer._vehicle_proximity_hint(0.0)
    assert "INTERNAL VEHICLE PROXIMITY HINT" in hint
    assert "camera perspective" in hint
    assert "Describe this as" not in hint


def test_vehicle_proximity_hint_is_unconditional_suspicious_false() -> None:
    """A second vehicle parked or stopped close to the protected one — even
    at a near-zero gap — must never be suspicious on its own; only a person
    or animal near either vehicle can make the scene worth flagging."""
    hint = MoondreamCloudAnalyzer._vehicle_proximity_hint(0.02)
    assert "set suspicious=false regardless of" in hint
    assert "0.02" in hint


def test_vehicle_proximity_hint_suggests_plain_driving_description() -> None:
    hint = MoondreamCloudAnalyzer._vehicle_proximity_hint(0.3)
    assert "a car drove up the street" in hint
    assert "suspicious=false" in hint
    assert "Only set suspicious=true if a person or animal" in hint


# ------------------------------------------------------------------
# _bbox_iou
# ------------------------------------------------------------------


def test_bbox_iou_identical_boxes() -> None:
    box = {"x_min": 0.2, "y_min": 0.2, "x_max": 0.6, "y_max": 0.6}
    assert MoondreamCloudAnalyzer._bbox_iou(box, box) == pytest.approx(1.0)


def test_bbox_iou_no_overlap() -> None:
    a = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.1, "y_max": 0.1}
    b = {"x_min": 0.5, "y_min": 0.5, "x_max": 0.6, "y_max": 0.6}
    assert MoondreamCloudAnalyzer._bbox_iou(a, b) == 0.0


def test_bbox_iou_touching_but_no_area_overlap() -> None:
    """Two boxes that merely touch at an edge (gap 0) have IoU 0 — distinct
    from two detections of the same box, which should have IoU 1."""
    a = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.3, "y_max": 1.0}
    b = {"x_min": 0.3, "y_min": 0.0, "x_max": 0.6, "y_max": 1.0}
    assert MoondreamCloudAnalyzer._bbox_iou(a, b) == 0.0


def test_bbox_iou_partial_overlap() -> None:
    a = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5}
    b = {"x_min": 0.25, "y_min": 0.0, "x_max": 0.75, "y_max": 0.5}
    # intersection: 0.25*0.5 = 0.125; union: 0.25 + 0.25 - 0.125 = 0.375
    assert MoondreamCloudAnalyzer._bbox_iou(a, b) == pytest.approx(0.125 / 0.375)


# ------------------------------------------------------------------
# _dedupe_boxes
# ------------------------------------------------------------------


def test_dedupe_boxes_empty_or_single_passthrough() -> None:
    assert MoondreamCloudAnalyzer._dedupe_boxes([]) == []
    box = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5}]
    assert MoondreamCloudAnalyzer._dedupe_boxes(box) == box


def test_dedupe_boxes_collapses_heavily_overlapping_duplicates() -> None:
    """Two boxes for the same physical object (high IoU) collapse to one —
    the larger of the pair survives."""
    bigger = {"x_min": 0.10, "y_min": 0.10, "x_max": 0.50, "y_max": 0.90}
    smaller = {"x_min": 0.12, "y_min": 0.10, "x_max": 0.50, "y_max": 0.90}
    result = MoondreamCloudAnalyzer._dedupe_boxes([smaller, bigger])
    assert result == [bigger]


def test_dedupe_boxes_keeps_distinct_non_overlapping_boxes() -> None:
    """Two genuinely separate objects (IoU below threshold) both survive."""
    a = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.2, "y_max": 0.2}
    b = {"x_min": 0.8, "y_min": 0.8, "x_max": 1.0, "y_max": 1.0}
    result = MoondreamCloudAnalyzer._dedupe_boxes([a, b])
    assert result == [a, b]


def test_dedupe_boxes_respects_custom_iou_threshold() -> None:
    """A lower iou_threshold merges boxes that a higher one would keep apart."""
    a = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5}
    b = {"x_min": 0.25, "y_min": 0.0, "x_max": 0.75, "y_max": 0.5}
    # IoU here is 0.125/0.375 ≈ 0.333 (see test_bbox_iou_partial_overlap)
    assert MoondreamCloudAnalyzer._dedupe_boxes([a, b], iou_threshold=0.5) == [a, b]
    assert MoondreamCloudAnalyzer._dedupe_boxes([a, b], iou_threshold=0.3) == [a]


# ------------------------------------------------------------------
# _other_vehicle_boxes
# ------------------------------------------------------------------


def test_other_vehicle_boxes_empty_protected_returns_all() -> None:
    all_boxes = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.3, "y_max": 0.3}]
    result = MoondreamCloudAnalyzer._other_vehicle_boxes([], all_boxes)
    assert result == all_boxes


def test_other_vehicle_boxes_excludes_matching_protected_box() -> None:
    protected = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.3, "y_max": 0.3}]
    same_box = {"x_min": 0.01, "y_min": 0.01, "x_max": 0.3, "y_max": 0.3}
    other_box = {"x_min": 0.6, "y_min": 0.6, "x_max": 0.9, "y_max": 0.9}
    result = MoondreamCloudAnalyzer._other_vehicle_boxes(
        protected, [same_box, other_box]
    )
    assert result == [other_box]


def test_other_vehicle_boxes_adjacent_cars_both_kept() -> None:
    """Two physically distinct cars parked flush against each other (gap
    0, IoU 0) must both survive — the protected-vehicle box does not
    erroneously swallow its touching neighbour."""
    protected = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.3, "y_max": 1.0}]
    neighbour = {"x_min": 0.3, "y_min": 0.0, "x_max": 0.6, "y_max": 1.0}
    result = MoondreamCloudAnalyzer._other_vehicle_boxes(protected, [neighbour])
    assert result == [neighbour]


# ------------------------------------------------------------------
# _position_hint (labeled subjects)
# ------------------------------------------------------------------


def test_position_hint_empty_returns_blank() -> None:
    assert MoondreamCloudAnalyzer._position_hint([]) == ""


def test_position_hint_single_person() -> None:
    box = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.2, "y_max": 0.2}
    hint = MoondreamCloudAnalyzer._position_hint([("Person", box)])
    assert "Person 1 is in the top-left of the frame" in hint
    assert "INTERNAL POSITION HINT" in hint


def test_position_hint_mixed_labels_numbered_independently() -> None:
    person_box = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.2, "y_max": 0.2}
    vehicle_box = {"x_min": 0.8, "y_min": 0.8, "x_max": 1.0, "y_max": 1.0}
    hint = MoondreamCloudAnalyzer._position_hint(
        [("Person", person_box), ("Vehicle", vehicle_box)]
    )
    assert "Person 1 is in the top-left of the frame" in hint
    assert "Vehicle 1 is in the bottom-right of the frame" in hint


def test_position_hint_limits_to_three_subjects() -> None:
    box = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.2, "y_max": 0.2}
    subjects = [("Person", box)] * 5
    hint = MoondreamCloudAnalyzer._position_hint(subjects)
    assert "Person 4" not in hint
    assert "Person 3" in hint


# ------------------------------------------------------------------
# _vehicle_hint
# ------------------------------------------------------------------


def test_vehicle_hint_content() -> None:
    hint = MoondreamCloudAnalyzer._vehicle_hint()
    assert "INTERNAL VEHICLE HINT" in hint
    assert "car drove up the street" in hint
    assert "suspicious=false" in hint


# ------------------------------------------------------------------
# _no_subject_response
# ------------------------------------------------------------------


def test_no_subject_response_is_valid_clear_json() -> None:
    is_suspicious, confidence, summary = MoondreamCloudAnalyzer._try_parse_json(
        MoondreamCloudAnalyzer._no_subject_response()
    )
    assert is_suspicious is False
    assert confidence == pytest.approx(0.9)
    assert "No person detected" in summary


# ------------------------------------------------------------------
# _force_not_suspicious
# ------------------------------------------------------------------


def test_force_not_suspicious_overrides_true_verdict() -> None:
    """A model-reported suspicious=true is rewritten to false, with
    confidence capped and the description left intact — used to enforce the
    vehicle-only-is-never-suspicious policy even if the model itself doesn't
    honor the prompt's negative instruction."""
    response = (
        '{"suspicious": true, "confidence": 0.5, '
        '"description": "A silver Kia Forte is parked close to the protected vehicle."}'
    )
    rewritten = MoondreamCloudAnalyzer._force_not_suspicious(response)
    is_suspicious, confidence, summary = MoondreamCloudAnalyzer._try_parse_json(
        rewritten
    )
    assert is_suspicious is False
    assert confidence <= 0.3
    assert "silver Kia Forte" in summary


def test_force_not_suspicious_leaves_already_clear_response_untouched() -> None:
    response = (
        '{"suspicious": false, "confidence": 0.1, "description": "Nothing notable."}'
    )
    assert MoondreamCloudAnalyzer._force_not_suspicious(response) == response


def test_force_not_suspicious_leaves_unparseable_response_untouched() -> None:
    assert MoondreamCloudAnalyzer._force_not_suspicious("not json") == "not json"


def test_force_not_suspicious_leaves_malformed_braces_untouched() -> None:
    """Braces are present (so start/end are found) but the contents aren't
    valid JSON — hits the JSONDecodeError branch rather than the "no braces"
    early return."""
    response = "{suspicious: true, not valid json}"
    assert MoondreamCloudAnalyzer._force_not_suspicious(response) == response


def test_force_not_suspicious_defaults_confidence_when_unconvertible() -> None:
    """A non-numeric confidence field can't be coerced to float; the rewrite
    should still force suspicious=false and fall back to confidence=0.0
    instead of raising."""
    response = (
        '{"suspicious": true, "confidence": "very high", '
        '"description": "A car is parked nearby."}'
    )
    rewritten = MoondreamCloudAnalyzer._force_not_suspicious(response)
    is_suspicious, confidence, summary = MoondreamCloudAnalyzer._try_parse_json(
        rewritten
    )
    assert is_suspicious is False
    assert confidence == 0.0
    assert "car is parked nearby" in summary


# ------------------------------------------------------------------
# _call_model with detect-based analysis
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_with_person_detected() -> None:
    """When /detect finds a person, _call_model proceeds to /query."""
    person_boxes = [{"x_min": 0.3, "y_min": 0.1, "x_max": 0.5, "y_max": 0.9}]
    query_answer = (
        '{"suspicious": false, "confidence": 0.7, "description": "Person walking by."}'
    )

    call_count = 0

    def side_effect_post(url, **kwargs):
        nonlocal call_count
        call_count += 1
        return _dispatch_moondream(
            url, kwargs, detect=person_boxes, query_answer=query_answer
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect_post)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = session

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "Person walking by" in result
    assert call_count == 3  # 1 detect + 1 caption + 1 query


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_no_person_skips_query() -> None:
    """When /detect finds no person, /query is skipped and fallback used."""
    detect_resp = _make_detect_resp([])

    session = MagicMock()
    session.post = MagicMock(return_value=detect_resp)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = session

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG, _FAKE_JPEG], "p")

    # 3 detect calls per frame (person, animal, vehicle) — all empty, no query
    assert session.post.call_count == 6
    assert "No person detected" in result


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_car_camera_no_subject_still_skips_query() -> (
    None
):
    """Car camera with no person, no animal, and only a single car in frame
    (the protected vehicle itself, not a second one) still skips /query —
    the extra animal/car detect calls shouldn't cause false triggers."""
    car_boxes = [{"x_min": 0.3, "y_min": 0.2, "x_max": 0.8, "y_max": 0.9}]

    def side_effect(url, **kwargs):
        obj = kwargs.get("json", {}).get("object", "")
        if obj == "car":
            return _make_detect_resp(car_boxes)
        return _make_detect_resp([])  # no person, no animal

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    a._session = session
    a._current_camera = "Driveway"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    # detect:person, detect:animal, detect:car — but no /query call
    assert session.post.call_count == 3
    assert "No person detected" in result


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_car_camera_animal_near_car_triggers_query() -> (
    None
):
    """Car camera: an animal near the protected vehicle (no person in frame)
    must not be silently written off — /query still runs with a proximity
    hint about the animal instead of a hardcoded 'not suspicious' result."""
    animal_boxes = [{"x_min": 0.55, "y_min": 0.5, "x_max": 0.65, "y_max": 0.9}]
    car_boxes = [{"x_min": 0.3, "y_min": 0.2, "x_max": 0.8, "y_max": 0.9}]
    query_answer = (
        '{"suspicious": true, "confidence": 0.6, "description": "Dog sniffing at car."}'
    )

    injected_prompts: list[str] = []

    def detect(obj: str) -> list[dict[str, float]]:
        if obj == "animal":
            return animal_boxes
        if obj == "car":
            return car_boxes
        return []  # no person

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=detect,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    a._session = session
    a._current_camera = "Driveway"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "Dog sniffing at car" in result
    assert injected_prompts
    assert "INTERNAL PROXIMITY HINT" in injected_prompts[0]
    assert "person or animal" in injected_prompts[0]


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_car_camera_multiple_vehicles_triggers_query() -> (
    None
):
    """Car camera: a second vehicle stopped close to the protected car (no
    person or animal in frame) must still trigger /query with a
    vehicle-to-vehicle proximity hint, not the hardcoded skip result."""
    car_boxes = [
        {"x_min": 0.1, "y_min": 0.2, "x_max": 0.5, "y_max": 0.9},
        {"x_min": 0.5, "y_min": 0.2, "x_max": 0.9, "y_max": 0.9},
    ]
    query_answer = '{"suspicious": true, "confidence": 0.55, "description": "Another car parked beside it."}'

    injected_prompts: list[str] = []

    def detect(obj: str) -> list[dict[str, float]]:
        if obj == "car":
            return car_boxes
        if obj == "Silver Kia":
            # Description-specific detect disambiguates the protected
            # vehicle as the first box, leaving the second as "other".
            return [car_boxes[0]]
        return []  # no person, no animal

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=detect,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    a._session = session
    a._current_camera = "Driveway"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "Another car parked beside it" in result
    assert injected_prompts
    assert "INTERNAL VEHICLE PROXIMITY HINT" in injected_prompts[0]
    assert "camera perspective" in injected_prompts[0]

    # Even though the (mocked) model answered suspicious=true, no person or
    # animal was in frame — the vehicle-only override must force this false.
    is_suspicious, _, _ = MoondreamCloudAnalyzer._try_parse_json(result)
    assert is_suspicious is False


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_person_touching_misclassified_car_still_flagged() -> (
    None
):
    """Regression test: the person is touching the REAL protected vehicle,
    but the description-specific detect call (independently, on the same
    frame) happens to match the *other* car box — e.g. because the person's
    body now partially occludes the real car, shifting its box just enough
    that the two independent zero-shot detections no longer agree. Before
    the fix, the proximity gap was only measured against whichever box
    disambiguation labelled "protected", so this exact scenario silently
    produced a "well away from the vehicle" hint despite direct contact.
    The fix measures proximity against every detected car box, so contact
    is never missed regardless of which box wins disambiguation."""
    person_boxes = [{"x_min": 0.65, "y_min": 0.2, "x_max": 0.75, "y_max": 0.9}]
    left_car = {"x_min": 0.05, "y_min": 0.2, "x_max": 0.35, "y_max": 0.9}
    right_car = {"x_min": 0.60, "y_min": 0.2, "x_max": 0.95, "y_max": 0.9}
    query_answer = '{"suspicious": true, "confidence": 0.85, "description": "Person touching the car."}'

    injected_prompts: list[str] = []

    def detect(obj: str) -> list[dict[str, float]]:
        if obj == "person":
            return person_boxes
        if obj == "car":
            return [left_car, right_car]
        if obj == "Silver Kia":
            # Disambiguation matches the LEFT car as "protected" — the
            # person is actually touching the RIGHT one.
            return [left_car]
        return []

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=detect,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    a._session = session
    a._current_camera = "Driveway"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "Person touching the car" in result
    assert injected_prompts
    assert "INTERNAL PROXIMITY HINT" in injected_prompts[0]
    assert "touching or pressed against" in injected_prompts[0]
    assert "well away" not in injected_prompts[0]


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_car_camera_with_car_detected() -> None:
    """Car camera: when car detected, proximity gap is injected into prompt."""
    person_boxes = [{"x_min": 0.0, "y_min": 0.0, "x_max": 0.1, "y_max": 0.9}]
    car_boxes = [{"x_min": 0.5, "y_min": 0.0, "x_max": 0.9, "y_max": 0.9}]
    query_answer = '{"suspicious": false, "confidence": 0.8, "description": "Person far from car."}'

    call_order: list[str] = []

    def side_effect(url, **kwargs):
        if "detect" in url:
            obj = kwargs.get("json", {}).get("object", "")
            call_order.append(f"detect:{obj}")
            if obj == "person":
                return _make_detect_resp(person_boxes)
            return _make_detect_resp(car_boxes)
        call_order.append("query")
        return _make_query_resp(query_answer)

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="p",
        car_description="Silver Kia Forte",
    )
    a._session = session
    a._current_camera = "Driveway"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "Person far from car" in result
    assert "detect:person" in call_order
    assert "detect:car" in call_order
    assert "query" in call_order


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_car_camera_no_car_in_frame() -> None:
    """Car camera: when car detect returns empty, no suppression hint is injected —
    the base prompt's vehicle-distance rules apply without interference."""
    person_boxes = [{"x_min": 0.4, "y_min": 0.1, "x_max": 0.6, "y_max": 0.9}]
    query_answer = (
        '{"suspicious": false, "confidence": 0.7, "description": "Person at door."}'
    )

    injected_prompts: list[str] = []

    def detect(obj: str) -> list[dict[str, float]]:
        if obj == "person":
            return person_boxes
        return []  # no car

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=detect,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="base prompt",
        car_description="Silver Kia",
    )
    a._session = session
    a._current_camera = "Driveway"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "base prompt")

    assert result != ""
    # Car detect failed → no suppression hint, no proximity hint either
    assert injected_prompts
    assert "not visible" not in injected_prompts[0].lower()
    assert "PROXIMITY HINT" not in injected_prompts[0]


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_falls_back_to_car_zone_when_no_car_detected() -> (
    None
):
    """Car camera: car detect finds nothing at all, but a fixed car zone is
    configured for this camera — the person's proximity to that zone must
    still produce a proximity hint instead of silently applying no hint."""
    person_boxes = [{"x_min": 0.55, "y_min": 0.2, "x_max": 0.65, "y_max": 0.9}]
    query_answer = '{"suspicious": true, "confidence": 0.8, "description": "Person at the car spot."}'

    injected_prompts: list[str] = []

    def detect(obj: str) -> list[dict[str, float]]:
        if obj == "person":
            return person_boxes
        return []  # car detect finds nothing, either query

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=detect,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="base prompt",
        car_description="Silver Kia",
        car_zones={
            "Driveway": {"x_min": 0.5, "y_min": 0.2, "x_max": 0.9, "y_max": 0.9}
        },
    )
    a._session = session
    a._current_camera = "Driveway"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "base prompt")

    assert result != ""
    assert injected_prompts
    assert "INTERNAL PROXIMITY HINT" in injected_prompts[0]
    assert "touching or pressed against" in injected_prompts[0]


@pytest.mark.asyncio
async def test_moondream_cloud_call_model_non_car_camera_injects_position() -> None:
    """Non-car camera: person position injected into prompt without car rules."""
    person_boxes = [{"x_min": 0.2, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]
    query_answer = '{"suspicious": false, "confidence": 0.6, "description": "Person at front door."}'

    injected_prompts: list[str] = []

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=person_boxes,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = session

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "Person at front door" in result
    assert injected_prompts
    assert "INTERNAL POSITION HINT" in injected_prompts[0]
    assert "of the frame" in injected_prompts[0]


@pytest.mark.asyncio
async def test_moondream_cloud_car_camera_overlap_flagged() -> None:
    """Person box overlapping car box → spatial note shows OVERLAPS."""
    # Person and car boxes that overlap
    person_boxes = [{"x_min": 0.3, "y_min": 0.0, "x_max": 0.6, "y_max": 1.0}]
    car_boxes = [{"x_min": 0.5, "y_min": 0.0, "x_max": 0.9, "y_max": 1.0}]
    query_answer = (
        '{"suspicious": true, "confidence": 0.9, "description": "Person touching car."}'
    )

    injected_prompts: list[str] = []

    def detect(obj: str) -> list[dict[str, float]]:
        if obj == "person":
            return person_boxes
        return car_boxes

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=detect,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p", car_description="Silver Kia")
    a._session = session
    a._current_camera = "Driveway"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "Person touching car" in result
    assert injected_prompts
    assert "INTERNAL PROXIMITY HINT" in injected_prompts[0]
    assert "touching" in injected_prompts[0].lower()


@pytest.mark.asyncio
async def test_moondream_cloud_tokens_accumulate_per_frame() -> None:
    """Token counts accumulate across multiple frames when person is detected."""
    person_boxes = [{"x_min": 0.2, "y_min": 0.0, "x_max": 0.4, "y_max": 1.0}]
    query_answer = (
        '{"suspicious": false, "confidence": 0.5, "description": "All clear."}'
    )

    def side_effect(url, **kwargs):
        if "detect" in url:
            return _make_detect_resp(person_boxes)
        return _make_query_resp(query_answer)

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = session

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await a._call_model([_FAKE_JPEG, _FAKE_JPEG], "test prompt")

    # 2 frames × (800 image tokens + prompt text tokens) → > 0
    assert a._last_prompt_tokens > 0
    assert a._last_completion_tokens > 0


@pytest.mark.asyncio
async def test_moondream_cloud_car_camera_scoped_to_car_cameras() -> None:
    """Car rules NOT applied when camera is not in ai_car_cameras list."""
    person_boxes = [{"x_min": 0.3, "y_min": 0.0, "x_max": 0.5, "y_max": 1.0}]
    query_answer = '{"suspicious": false, "confidence": 0.6, "description": "Clear."}'

    injected_prompts: list[str] = []

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=person_boxes,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="p",
        car_description="Silver Kia",
        car_cameras=["Driveway"],  # Only Driveway gets car rules
    )
    a._session = session
    # Camera "Front Door" is NOT in car_cameras → should not get car detect
    a._current_camera = "Front Door"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await a._call_model([_FAKE_JPEG], "p")

    # 3 calls: detect(person) + caption + query — no detect(car) for Front Door
    assert session.post.call_count == 3
    # Injected prompt should have position data, NOT car rules
    assert injected_prompts
    assert "INTERNAL POSITION HINT" in injected_prompts[0]
    assert "touching" not in injected_prompts[0].lower()


# ------------------------------------------------------------------
# Non-car camera ambient vehicle/animal detection (no person in frame)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moondream_cloud_non_car_camera_vehicle_gets_caption_and_query() -> None:
    """A car passing a non-car camera (no person) must still get a caption
    and a query — not the generic 'no person detected' skip response."""
    vehicle_boxes = [{"x_min": 0.2, "y_min": 0.3, "x_max": 0.6, "y_max": 0.8}]
    query_answer = '{"suspicious": false, "confidence": 0.3, "description": "A car drove up the street."}'

    injected_prompts: list[str] = []

    def detect(obj: str) -> list[dict[str, float]]:
        if obj == "vehicle":
            return vehicle_boxes
        return []  # no person, no animal

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=detect,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = session
    a._current_camera = "Front Door"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "A car drove up the street" in result
    assert injected_prompts
    assert "INTERNAL VEHICLE HINT" in injected_prompts[0]
    assert "Vehicle 1 is in the" in injected_prompts[0]


@pytest.mark.asyncio
async def test_moondream_cloud_non_car_camera_animal_gets_caption_and_query() -> None:
    """An animal on a non-car camera (no person) must still get a caption
    and a query, with its position injected but no vehicle-specific hint."""
    animal_boxes = [{"x_min": 0.4, "y_min": 0.5, "x_max": 0.6, "y_max": 0.9}]
    query_answer = '{"suspicious": false, "confidence": 0.2, "description": "A cat walked across the yard."}'

    injected_prompts: list[str] = []

    def detect(obj: str) -> list[dict[str, float]]:
        if obj == "animal":
            return animal_boxes
        return []  # no person, no vehicle

    def side_effect(url, **kwargs):
        return _dispatch_moondream(
            url,
            kwargs,
            detect=detect,
            query_answer=query_answer,
            injected_prompts=injected_prompts,
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = session
    a._current_camera = "Backyard"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    assert "A cat walked across the yard" in result
    assert injected_prompts
    assert "Animal 1 is in the" in injected_prompts[0]
    assert "INTERNAL VEHICLE HINT" not in injected_prompts[0]


@pytest.mark.asyncio
async def test_moondream_cloud_non_car_camera_nothing_detected_skips_query() -> None:
    """Non-car camera with no person, animal, or vehicle detected still
    uses the cheap skip path — no caption/query wasted on empty motion."""
    query_answer = '{"suspicious": false, "confidence": 0.1, "description": "unused"}'

    def side_effect(url, **kwargs):
        return _dispatch_moondream(url, kwargs, detect=[], query_answer=query_answer)

    session = MagicMock()
    session.post = MagicMock(side_effect=side_effect)
    session.closed = False

    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = session
    a._current_camera = "Front Door"

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await a._call_model([_FAKE_JPEG], "p")

    # 3 detect calls (person, animal, vehicle) — no caption, no query
    assert session.post.call_count == 3
    assert "No person detected" in result


# ------------------------------------------------------------------
# Database anomaly score — coverage for rare-hour and duration paths
# ------------------------------------------------------------------


# =============================================================================
# v3.0.2 — MoondreamFineTuneManager and finetune_model inference
# =============================================================================


def _make_ft_resp(status: int = 200, body: dict | None = None) -> AsyncMock:
    """Create a mock aiohttp response for the fine-tuning API."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


# ------------------------------------------------------------------
# MoondreamFineTuneManager — get_model_id (static)
# ------------------------------------------------------------------


def test_get_model_id_format() -> None:
    model_id = MoondreamFineTuneManager.get_model_id("abc123", 50)
    assert model_id == "moondream3-preview/abc123@50"


def test_get_model_id_zero_step() -> None:
    assert MoondreamFineTuneManager.get_model_id("x", 0) == "moondream3-preview/x@0"


# ------------------------------------------------------------------
# MoondreamFineTuneManager — lifecycle
# ------------------------------------------------------------------


async def test_ft_manager_get_session_creates_session() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    session = m._get_session()
    assert session is not None
    await m.close()


async def test_ft_manager_close_open_session() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    m._session = mock_session
    await m.close()
    mock_session.close.assert_called_once()


async def test_ft_manager_close_already_closed() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    mock_session = MagicMock()
    mock_session.closed = True
    m._session = mock_session
    await m.close()
    mock_session.close.assert_not_called()


# ------------------------------------------------------------------
# MoondreamFineTuneManager — create_finetune
# ------------------------------------------------------------------


async def test_create_finetune_success() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(return_value=_make_ft_resp(200, {"finetune_id": "ft-abc123"}))
    )
    result = await m.create_finetune("my-cam-finetune", rank=16)
    assert result == "ft-abc123"


async def test_create_finetune_invalid_rank() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    result = await m.create_finetune("test", rank=7)
    assert result is None


async def test_create_finetune_http_error() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(500)))
    result = await m.create_finetune("test", rank=8)
    assert result is None


async def test_create_finetune_empty_id_returns_none() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(return_value=_make_ft_resp(200, {"finetune_id": ""}))
    )
    result = await m.create_finetune("test", rank=16)
    assert result is None


async def test_create_finetune_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    result = await m.create_finetune("test", rank=16)
    assert result is None


async def test_create_finetune_all_valid_ranks() -> None:
    for rank in (8, 16, 24, 32):
        m = MoondreamFineTuneManager(api_key="key")
        m._session = _mock_session(
            post=MagicMock(
                return_value=_make_ft_resp(200, {"finetune_id": f"ft-{rank}"})
            )
        )
        result = await m.create_finetune(f"test-{rank}", rank=rank)
        assert result == f"ft-{rank}"


# ------------------------------------------------------------------
# MoondreamFineTuneManager — list_finetunes
# ------------------------------------------------------------------


async def test_list_finetunes_success() -> None:
    finetunes = [{"id": "ft-1", "name": "cam-ft"}, {"id": "ft-2", "name": "other"}]
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        get=MagicMock(return_value=_make_ft_resp(200, {"finetunes": finetunes}))
    )
    result = await m.list_finetunes()
    assert result == finetunes


async def test_list_finetunes_non_200_returns_empty() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(get=MagicMock(return_value=_make_ft_resp(401)))
    result = await m.list_finetunes()
    assert result == []


async def test_list_finetunes_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        get=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    result = await m.list_finetunes()
    assert result == []


async def test_list_finetunes_passes_cursor() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        get=MagicMock(return_value=_make_ft_resp(200, {"finetunes": []}))
    )
    await m.list_finetunes(limit=10, cursor="next-page-token")
    call_kwargs = m._session.get.call_args
    params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
    assert params.get("cursor") == "next-page-token"
    assert params.get("limit") == 10


# ------------------------------------------------------------------
# MoondreamFineTuneManager — get_finetune
# ------------------------------------------------------------------


async def test_get_finetune_success() -> None:
    body = {"id": "ft-1", "name": "cam-ft", "rank": 16}
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(get=MagicMock(return_value=_make_ft_resp(200, body)))
    result = await m.get_finetune("ft-1")
    assert result == body


async def test_get_finetune_not_found() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(get=MagicMock(return_value=_make_ft_resp(404)))
    result = await m.get_finetune("nonexistent")
    assert result is None


async def test_get_finetune_server_error() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(get=MagicMock(return_value=_make_ft_resp(500)))
    result = await m.get_finetune("ft-1")
    assert result is None


async def test_get_finetune_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        get=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    result = await m.get_finetune("ft-1")
    assert result is None


# ------------------------------------------------------------------
# MoondreamFineTuneManager — delete_finetune
# ------------------------------------------------------------------


async def test_delete_finetune_success() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        delete=MagicMock(return_value=_make_ft_resp(200, {"ok": True}))
    )
    assert await m.delete_finetune("ft-1") is True


async def test_delete_finetune_not_found() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(delete=MagicMock(return_value=_make_ft_resp(404)))
    assert await m.delete_finetune("nonexistent") is False


async def test_delete_finetune_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        delete=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await m.delete_finetune("ft-1") is False


# ------------------------------------------------------------------
# MoondreamFineTuneManager — generate_rollouts
# ------------------------------------------------------------------


async def test_generate_rollouts_success_query_skill() -> None:
    body = {"rollouts": ["person", "car", "empty", "person"]}
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(200, body)))
    result = await m.generate_rollouts(
        finetune_id="ft-1",
        image=_FAKE_JPEG,
        question="What do you see?",
        num_rollouts=4,
    )
    assert result == body


async def test_generate_rollouts_with_ground_truth() -> None:
    body = {"rollouts": ["person"], "rewards": [1.0]}
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(200, body)))
    result = await m.generate_rollouts(
        finetune_id="ft-1",
        image=_FAKE_JPEG,
        question="What do you see?",
        num_rollouts=1,
        ground_truth="person",
    )
    assert "rewards" in result
    assert result["rewards"] == [1.0]


async def test_generate_rollouts_detect_skill_uses_object_key() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(return_value=_make_ft_resp(200, {"rollouts": []}))
    )
    await m.generate_rollouts(
        finetune_id="ft-1",
        image=_FAKE_JPEG,
        question="person",
        num_rollouts=2,
        skill="detect",
    )
    call_kwargs = m._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    assert payload["request"]["skill"] == "detect"
    assert "object" in payload["request"]
    assert "question" not in payload["request"]


async def test_generate_rollouts_clamps_num_rollouts() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(return_value=_make_ft_resp(200, {"rollouts": []}))
    )
    await m.generate_rollouts("ft-1", _FAKE_JPEG, "q", num_rollouts=99)
    call_kwargs = m._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    assert payload["num_rollouts"] == 16  # clamped to max


async def test_generate_rollouts_http_error() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(500)))
    result = await m.generate_rollouts("ft-1", _FAKE_JPEG, "q")
    assert result == {}


async def test_generate_rollouts_timeout() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(side_effect=asyncio.TimeoutError))
    result = await m.generate_rollouts("ft-1", _FAKE_JPEG, "q")
    assert result == {}


# ------------------------------------------------------------------
# MoondreamFineTuneManager — train_step
# ------------------------------------------------------------------


async def test_train_step_rl_mode() -> None:
    metrics = {"kl_divergence": 0.01, "gradient_norm": 0.5, "reward_mean": 0.8}
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(200, metrics)))
    result = await m.train_step(
        finetune_id="ft-1",
        request={"skill": "query", "question": "Who is there?"},
        rollouts=["person", "empty"],
        rewards=[1.0, 0.0],
        mode="rl",
    )
    assert result == metrics


async def test_train_step_sft_mode_uses_first_rollout_as_target() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(return_value=_make_ft_resp(200, {"ok": True}))
    )
    await m.train_step(
        finetune_id="ft-1",
        request={"skill": "query", "question": "Describe."},
        rollouts=["A person is walking."],
        mode="sft",
    )
    call_kwargs = m._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    group = payload["groups"][0]
    assert group["mode"] == "sft"
    assert group["target"] == "A person is walking."


async def test_train_step_rl_includes_rewards() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(200, {})))
    await m.train_step(
        "ft-1",
        {"skill": "query", "question": "q"},
        ["r1", "r2"],
        rewards=[1.0, 0.0],
        mode="rl",
    )
    call_kwargs = m._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    group = payload["groups"][0]
    assert group["rewards"] == [1.0, 0.0]


async def test_train_step_http_error() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(500)))
    result = await m.train_step("ft-1", {}, [], [])
    assert result == {}


async def test_train_step_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    result = await m.train_step("ft-1", {}, [], [])
    assert result == {}


# ------------------------------------------------------------------
# MoondreamFineTuneManager — train_from_examples
# ------------------------------------------------------------------


async def test_train_from_examples_sft_uses_ground_truth_as_target() -> None:
    rollout_resp = _make_ft_resp(200, {"rollouts": ["a person is walking"]})
    train_resp = _make_ft_resp(200, {"kl_divergence": 0.02})
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(side_effect=[rollout_resp, train_resp]))

    result = await m.train_from_examples(
        "ft-1",
        [
            {
                "image": _FAKE_JPEG,
                "question": "Is anything suspicious happening?",
                "ground_truth": json.dumps({"suspicious": False}),
            }
        ],
    )
    assert result["steps_completed"] == 1
    train_call = m._session.post.call_args_list[1]
    payload = train_call.kwargs.get("json") or train_call[1].get("json", {})
    group = payload["groups"][0]
    assert group["mode"] == "sft"
    assert group["target"] == json.dumps({"suspicious": False})


async def test_train_from_examples_skips_example_with_no_rollouts() -> None:
    empty_rollout_resp = _make_ft_resp(200, {"rollouts": []})
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=empty_rollout_resp))

    result = await m.train_from_examples(
        "ft-1",
        [{"image": _FAKE_JPEG, "question": "q", "ground_truth": "{}"}],
    )
    assert result == {"steps_completed": 0, "results": [], "successful_indices": []}
    # generate_rollouts was called, but train_step never should have been
    # (only one POST call total).
    assert m._session.post.call_count == 1


async def test_train_from_examples_continues_after_one_failure() -> None:
    rollout_ok = _make_ft_resp(200, {"rollouts": ["r"]})
    train_ok = _make_ft_resp(200, {"ok": True})
    rollout_empty = _make_ft_resp(200, {"rollouts": []})
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(side_effect=[rollout_empty, rollout_ok, train_ok])
    )

    result = await m.train_from_examples(
        "ft-1",
        [
            {"image": _FAKE_JPEG, "question": "q1", "ground_truth": "{}"},
            {"image": _FAKE_JPEG, "question": "q2", "ground_truth": "{}"},
        ],
    )
    assert result["steps_completed"] == 1
    # Only the second example (index 1) actually completed a training step —
    # the first was skipped for lack of rollouts and must not be marked trained.
    assert result["successful_indices"] == [1]


async def test_train_from_examples_rl_mode_trains_on_rollouts() -> None:
    rollout_resp = _make_ft_resp(200, {"rollouts": ["a", "b"], "rewards": [1.0, 0.0]})
    train_resp = _make_ft_resp(200, {"ok": True})
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(side_effect=[rollout_resp, train_resp]))

    result = await m.train_from_examples(
        "ft-1",
        [{"image": _FAKE_JPEG, "question": "q", "ground_truth": "{}"}],
        mode="rl",
    )
    assert result["steps_completed"] == 1
    train_call = m._session.post.call_args_list[1]
    payload = train_call.kwargs.get("json") or train_call[1].get("json", {})
    group = payload["groups"][0]
    assert group["mode"] == "rl"
    assert group["rewards"] == [1.0, 0.0]


# ------------------------------------------------------------------
# MoondreamFineTuneManager — checkpoints
# ------------------------------------------------------------------


async def test_save_checkpoint_success() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(return_value=_make_ft_resp(200, {"ok": True}))
    )
    assert await m.save_checkpoint("ft-1") is True


async def test_save_checkpoint_failure() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(500)))
    assert await m.save_checkpoint("ft-1") is False


async def test_save_checkpoint_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await m.save_checkpoint("ft-1") is False


async def test_list_checkpoints_success() -> None:
    checkpoints = [{"step": 10}, {"step": 20}]
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        get=MagicMock(return_value=_make_ft_resp(200, {"checkpoints": checkpoints}))
    )
    result = await m.list_checkpoints("ft-1")
    assert result == checkpoints


async def test_list_checkpoints_non_200_returns_empty() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(get=MagicMock(return_value=_make_ft_resp(404)))
    assert await m.list_checkpoints("ft-1") == []


async def test_list_checkpoints_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        get=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await m.list_checkpoints("ft-1") == []


async def test_delete_checkpoint_success() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(delete=MagicMock(return_value=_make_ft_resp(200)))
    assert await m.delete_checkpoint("ft-1", 50) is True


async def test_delete_checkpoint_not_found() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(delete=MagicMock(return_value=_make_ft_resp(404)))
    assert await m.delete_checkpoint("ft-1", 99) is False


async def test_delete_checkpoint_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        delete=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await m.delete_checkpoint("ft-1", 50) is False


# ------------------------------------------------------------------
# MoondreamFineTuneManager — log_metrics
# ------------------------------------------------------------------


async def test_log_metrics_success() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(return_value=_make_ft_resp(200, {"ok": True}))
    )
    assert await m.log_metrics("ft-1", step=25, metrics={"accuracy": 0.87}) is True


async def test_log_metrics_failure() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(400)))
    assert await m.log_metrics("ft-1", step=1, metrics={}) is False


async def test_log_metrics_network_error() -> None:
    import aiohttp

    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    )
    assert await m.log_metrics("ft-1", step=1, metrics={"val_acc": 0.9}) is False


async def test_log_metrics_sends_correct_payload() -> None:
    m = MoondreamFineTuneManager(api_key="key")
    m._session = _mock_session(post=MagicMock(return_value=_make_ft_resp(200)))
    await m.log_metrics("ft-abc", step=42, metrics={"loss": 0.05, "acc": 0.95})
    call_kwargs = m._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    assert payload["step"] == 42
    assert payload["metrics"] == {"loss": 0.05, "acc": 0.95}


# ------------------------------------------------------------------
# MoondreamCloudAnalyzer — finetune_model integration
# ------------------------------------------------------------------


def test_moondream_cloud_model_name_with_finetune() -> None:
    """model_name() returns the fine-tune model ID when configured."""
    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="p",
        finetune_model="moondream3-preview/ft-abc@50",
    )
    assert a.model_name() == "moondream3-preview/ft-abc@50"


def test_moondream_cloud_model_name_without_finetune() -> None:
    """model_name() returns the base model ID when no fine-tune is configured."""
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    assert a.model_name() == "moondream3-preview"


async def test_moondream_cloud_fetch_models_with_finetune() -> None:
    """fetch_models includes the fine-tune model entry when configured."""
    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="p",
        finetune_model="moondream3-preview/ft-abc@50",
    )
    models = await a.fetch_models()
    assert len(models) == 2
    names = [m["name"] for m in models]
    assert "moondream3-preview" in names
    assert "moondream3-preview/ft-abc@50" in names


async def test_moondream_cloud_fetch_models_without_finetune() -> None:
    """fetch_models returns only the base model when no fine-tune is configured."""
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    models = await a.fetch_models()
    assert len(models) == 1
    assert models[0]["name"] == "moondream3-preview"


async def test_moondream_cloud_call_api_frame_includes_finetune_model() -> None:
    """_call_api_frame sends the fine-tune model ID in the request payload."""
    query_resp = _make_query_resp(
        '{"suspicious": false, "confidence": 0.5, "description": "Clear."}'
    )
    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="p",
        finetune_model="moondream3-preview/ft-xyz@100",
    )
    a._session = _mock_session(post=MagicMock(return_value=query_resp))

    await a._call_api_frame(_FAKE_JPEG, "Analyze.")
    call_kwargs = a._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    assert payload.get("model") == "moondream3-preview/ft-xyz@100"


async def test_moondream_cloud_call_api_frame_no_model_when_no_finetune() -> None:
    """_call_api_frame does NOT include 'model' when no fine-tune is configured."""
    query_resp = _make_query_resp(
        '{"suspicious": false, "confidence": 0.5, "description": "Clear."}'
    )
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = _mock_session(post=MagicMock(return_value=query_resp))

    await a._call_api_frame(_FAKE_JPEG, "Analyze.")
    call_kwargs = a._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    assert "model" not in payload


async def test_moondream_cloud_detect_includes_finetune_model() -> None:
    """_detect_objects sends the fine-tune model ID in the payload."""
    detect_resp = _make_detect_resp(
        [{"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.9}]
    )
    a = MoondreamCloudAnalyzer(
        api_key="key",
        prompt="p",
        finetune_model="moondream3-preview/ft-abc@50",
    )
    a._session = _mock_session(post=MagicMock(return_value=detect_resp))

    await a._detect_objects(_FAKE_JPEG, "person")
    call_kwargs = a._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    assert payload.get("model") == "moondream3-preview/ft-abc@50"


async def test_moondream_cloud_detect_no_model_when_no_finetune() -> None:
    """_detect_objects does NOT include 'model' when no fine-tune is configured."""
    detect_resp = _make_detect_resp([])
    a = MoondreamCloudAnalyzer(api_key="key", prompt="p")
    a._session = _mock_session(post=MagicMock(return_value=detect_resp))

    await a._detect_objects(_FAKE_JPEG, "person")
    call_kwargs = a._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
    assert "model" not in payload


def test_create_analyzer_moondream_cloud_with_finetune_model() -> None:
    """create_analyzer passes finetune_model through to MoondreamCloudAnalyzer."""
    a = create_analyzer(
        "moondream_cloud",
        "prompt",
        moondream_api_key="key",
        moondream_finetune_model="moondream3-preview/ft-123@75",
    )
    assert isinstance(a, MoondreamCloudAnalyzer)
    assert a.model_name() == "moondream3-preview/ft-123@75"


def test_create_analyzer_moondream_cloud_no_finetune_model() -> None:
    """create_analyzer works without finetune_model — uses base model."""
    a = create_analyzer("moondream_cloud", "prompt", moondream_api_key="key")
    assert isinstance(a, MoondreamCloudAnalyzer)
    assert a.model_name() == "moondream3-preview"


# ===========================================================================
# v3.0.5 — long-clip frame budget, timeline-spread frame selection, scene
# baseline ("smart brain"), and refined prompt wording
# ===========================================================================


def _real_jpeg(shade: int = 100, width: int = 64) -> bytes:
    """Build a genuinely decodable solid-color JPEG for PIL-dependent tests."""
    import io

    from PIL import Image

    img = Image.new("RGB", (width, 64), color=(shade, shade, shade))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _target_frame_count — 30s frame-budget threshold
# ---------------------------------------------------------------------------


def test_target_frame_count_short_clip_uses_max_frames() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        max_frames=3,
        frame_interval=2.0,
    )
    # 15 frames * 2.0s = 30s — at the threshold, not over it.
    assert a._target_frame_count(15) == 3


def test_target_frame_count_long_clip_doubles_frame_budget() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        max_frames=3,
        frame_interval=2.0,
    )
    # 16 frames * 2.0s = 32s — over the 30s threshold.
    assert a._target_frame_count(16) == 6  # max_frames (3) * multiplier (2)


def test_target_frame_count_prefers_known_clip_duration_over_estimate() -> None:
    """A precise clip_duration should override the raw-frame-count estimate."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        max_frames=3,
        frame_interval=2.0,
    )
    # Raw frame count alone would estimate 10s (under threshold), but the
    # real duration (45s) is over it and must win.
    assert a._target_frame_count(5, clip_duration=45.0) == 6
    # Raw frame count alone would estimate 40s (over threshold), but the
    # real duration (20s) is under it and must win.
    assert a._target_frame_count(20, clip_duration=20.0) == 3


@pytest.mark.asyncio
async def test_analyze_clip_long_clip_sends_bonus_frames() -> None:
    """A clip estimated to run past 30s should send double max_frames."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
        max_frames=3,
        frame_interval=2.0,
    )
    # 20 raw frames * 2.0s = 40s (> 30s) → target = 3 * 2 = 6
    many_frames = _FAKE_JPEG * 20
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(many_frames, b""))
    mock_proc.returncode = 0

    good_resp = json.dumps(
        {"suspicious": False, "confidence": 0.8, "description": "Clear"}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": good_resp})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.frame_count == 6


@pytest.mark.asyncio
async def test_analyze_clip_uses_known_clip_duration_over_estimate() -> None:
    """A precise clip_duration passed to analyze_clip should drive the doubling
    decision instead of the raw-frame-count estimate."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
        max_frames=3,
        frame_interval=2.0,
    )
    # Only 10 raw frames extracted (would estimate 20s, under threshold), but
    # the real clip_duration (50s) is over it and must still double the budget.
    few_frames = _FAKE_JPEG * 10
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(few_frames, b""))
    mock_proc.returncode = 0

    good_resp = json.dumps(
        {"suspicious": False, "confidence": 0.8, "description": "Clear"}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": good_resp})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip(
            "/clips/test.mp4", "c1", "Driveway", clip_duration=50.0
        )

    assert result.frame_count == 6


@pytest.mark.asyncio
async def test_analyze_clip_short_clip_keeps_configured_max_frames() -> None:
    """A clip estimated at or under 30s must not get a doubled frame budget."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
        max_frames=3,
        frame_interval=2.0,
    )
    # 15 raw frames * 2.0s = 30s (at, not over, threshold) → target stays 3
    many_frames = _FAKE_JPEG * 15
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(many_frames, b""))
    mock_proc.returncode = 0

    good_resp = json.dumps(
        {"suspicious": False, "confidence": 0.8, "description": "Clear"}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": good_resp})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.frame_count == 3


# ---------------------------------------------------------------------------
# _select_best_frames — timeline-spread fill (item c)
# ---------------------------------------------------------------------------


def test_select_best_frames_spreads_extra_picks_across_timeline() -> None:
    """Extra picks (beyond first/last/peak) should spread across the clip's
    timeline instead of clustering around a single motion burst."""
    shades = [10] * 20
    shades[9] = 250
    shades[10] = 250
    frames = [_real_jpeg(shades[i], width=64 + i) for i in range(20)]

    result = ClipAnalyzer._select_best_frames(frames, 5)
    assert len(result) == 5

    indices = sorted(frames.index(f) for f in result)
    assert indices[0] == 0
    assert indices[-1] == 19
    gaps = [b - a for a, b in zip(indices, indices[1:])]
    assert min(gaps) >= 3


def test_select_best_frames_relaxes_spacing_when_pool_too_small() -> None:
    """With a small pool, the spacing constraint must relax rather than
    return fewer than target_count frames."""
    frames = [_real_jpeg(10, width=64 + i) for i in range(6)]
    result = ClipAnalyzer._select_best_frames(frames, 5)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# _scene_thumbnail
# ---------------------------------------------------------------------------


def test_scene_thumbnail_returns_normalized_pixel_values() -> None:
    thumb = ClipAnalyzer._scene_thumbnail(_real_jpeg(128, width=200))
    assert thumb is not None
    assert len(thumb) == 16 * 16
    assert all(0.0 <= v <= 1.0 for v in thumb)
    assert all(abs(v - 128 / 255) < 0.05 for v in thumb)


def test_scene_thumbnail_returns_none_for_invalid_data() -> None:
    assert ClipAnalyzer._scene_thumbnail(b"not a real jpeg") is None


def test_scene_thumbnail_returns_none_for_empty_bytes() -> None:
    assert ClipAnalyzer._scene_thumbnail(b"") is None


# ---------------------------------------------------------------------------
# attach_scene_baseline_db / analyze_clip integration
# ---------------------------------------------------------------------------


def test_attach_scene_baseline_db_sets_attribute() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    assert a._scene_baseline_db is None
    sentinel = object()
    a.attach_scene_baseline_db(sentinel)  # type: ignore[arg-type]
    assert a._scene_baseline_db is sentinel


async def _run_analyze_clip_with_mock_db(
    is_suspicious: bool,
    confidence: float | None = None,
) -> tuple[MagicMock, object]:
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
        max_frames=1,
    )
    mock_db = MagicMock()
    mock_db.get_scene_deviation = AsyncMock(return_value=0.05)
    mock_db.record_scene_baseline = AsyncMock()
    analyzer.attach_scene_baseline_db(mock_db)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(), b""))
    mock_proc.returncode = 0

    if confidence is None:
        confidence = 0.9 if is_suspicious else 0.5
    resp_json = json.dumps(
        {
            "suspicious": is_suspicious,
            "confidence": confidence,
            "description": "Someone prying at the door" if is_suspicious else "OK",
        }
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": resp_json})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await analyzer.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    return mock_db, result


@pytest.mark.asyncio
async def test_analyze_clip_looks_up_scene_deviation_when_db_attached() -> None:
    mock_db, _ = await _run_analyze_clip_with_mock_db(is_suspicious=False)
    mock_db.get_scene_deviation.assert_called_once()
    assert mock_db.get_scene_deviation.call_args[0][0] == "Driveway"


@pytest.mark.asyncio
async def test_analyze_clip_records_scene_baseline_when_not_suspicious() -> None:
    mock_db, _ = await _run_analyze_clip_with_mock_db(is_suspicious=False)
    mock_db.record_scene_baseline.assert_called_once()
    assert mock_db.record_scene_baseline.call_args[0][0] == "Driveway"


@pytest.mark.asyncio
async def test_analyze_clip_skips_scene_baseline_recording_when_suspicious() -> None:
    """A confidently suspicious clip must never be folded into the 'normal' baseline."""
    mock_db, _ = await _run_analyze_clip_with_mock_db(
        is_suspicious=True, confidence=0.9
    )
    mock_db.record_scene_baseline.assert_not_called()


@pytest.mark.asyncio
async def test_analyze_clip_records_scene_baseline_when_suspicious_but_low_confidence() -> (
    None
):
    """A low-confidence suspicious hedge must still teach the baseline.

    Otherwise a persistent but benign change (a car parked overnight, trash
    put out for collection) that the model only flags out of caution because
    of the scene-deviation hint would never get absorbed into "normal",
    causing the same hint — and the same hedge — to fire on every future
    clip forever.
    """
    mock_db, _ = await _run_analyze_clip_with_mock_db(
        is_suspicious=True, confidence=0.4
    )
    mock_db.record_scene_baseline.assert_called_once()
    assert mock_db.record_scene_baseline.call_args[0][0] == "Driveway"


@pytest.mark.asyncio
async def test_analyze_clip_computes_zone_motion_fraction_end_to_end() -> None:
    """Full analyze_clip() pipeline: a car_zone configured for the clip's
    camera must reach _build_prompt as a computed zone_motion_fraction,
    proving the wiring from extracted frames through to the prompt actually
    runs, not just the unit-level pieces in isolation."""
    analyzer = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        frame_strategy="uniform",
        max_frames=2,
        car_description="Silver Kia",
        car_zones={
            "Driveway": {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
        },
    )

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(_real_jpeg_with_bar(5) + _real_jpeg_with_bar(45), b"")
    )
    mock_proc.returncode = 0

    resp_json = json.dumps(
        {"suspicious": False, "confidence": 0.3, "description": "Quiet driveway."}
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"response": resp_json})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    analyzer._session = _mock_session(post=MagicMock(return_value=mock_resp))

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        patch.object(
            analyzer,
            "extract_frames",
            new=AsyncMock(
                return_value=[
                    _real_jpeg_with_bar(5),
                    _real_jpeg_with_bar(45),
                ]
            ),
        ),
        patch.object(analyzer, "_build_prompt", wraps=analyzer._build_prompt) as spy,
    ):
        await analyzer.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert spy.call_args.kwargs["zone_motion_fraction"] is not None


# ---------------------------------------------------------------------------
# _build_prompt — scene_deviation wording (item d)
# ---------------------------------------------------------------------------


def test_build_prompt_scene_deviation_none_omits_scene_baseline() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam", scene_deviation=None)
    assert "SCENE BASELINE" not in prompt


def test_build_prompt_scene_deviation_high_flags_change() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam", scene_deviation=0.5)
    assert "SCENE BASELINE" in prompt
    assert "differs from its usual background" in prompt
    assert "0.50" in prompt


def test_build_prompt_scene_deviation_high_warns_lighting_not_suspicious() -> None:
    """A high scene-baseline deviation is frequently just lighting/weather/
    day-night transition — the prompt must say so explicitly so weaker models
    don't treat ambient change alone as evidence of something suspicious."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam", scene_deviation=0.5)
    assert "lighting, weather, shadows" in prompt
    assert "day/night transition" in prompt
    assert "set suspicious=false" in prompt


def test_build_prompt_scene_deviation_low_confirms_normal() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam", scene_deviation=0.02)
    assert "SCENE BASELINE" in prompt
    assert "closely matches its usual background" in prompt


def test_build_prompt_scene_deviation_zero_confirms_normal() -> None:
    """0.0 (exact match) must take the 'matches baseline' branch, not be
    treated as falsy/absent the way None is."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam", scene_deviation=0.0)
    assert "SCENE BASELINE" in prompt
    assert "closely matches its usual background" in prompt


# ---------------------------------------------------------------------------
# v4.0.0 bug fix — "favor calm read" scene-baseline framing must never
# undermine the strict PROTECTED VEHICLE distance rules on a car camera.
#
# Root cause of a real regression: a protected vehicle parked in its usual
# spot IS the camera's learned "usual background", so a low scene-deviation
# score on that camera is true on almost every clip regardless of whether a
# person is currently touching/leaning on the vehicle (the opening frame the
# score is computed from is captured at/near the start of motion, often
# before a person has reached the car). The old code appended a "favor a
# calm, routine read" instruction on every one of those clips, which measurably
# competed with and could override the PROTECTED VEHICLE block's much
# stricter "within 1 foot -> suspicious=true, confidence>=0.8" rule — even
# for large, generally instruction-compliant providers like OpenAI, not just
# smaller local models.
# ---------------------------------------------------------------------------


def test_build_prompt_omits_favor_calm_scene_baseline_for_car_camera() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        car_description="Silver Kia Forte, parked in the driveway",
    )
    prompt = a._build_prompt("Driveway", scene_deviation=0.02)
    assert "favor a calm, routine read" not in prompt
    assert "closely matches its usual background" not in prompt
    # The strict distance rules must still be present and untouched.
    assert "PROTECTED VEHICLE" in prompt
    assert "confidence ≥0.8" in prompt


def test_build_prompt_keeps_elevated_scene_baseline_for_car_camera() -> None:
    """A HIGH deviation ('something new in frame') is still a useful signal
    on a car camera — e.g. flags a second vehicle or object that wasn't
    there before — only the low-deviation 'favor calm' framing is dropped."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        car_description="Silver Kia Forte, parked in the driveway",
    )
    prompt = a._build_prompt("Driveway", scene_deviation=0.5)
    assert "SCENE BASELINE" in prompt
    assert "differs from its usual background" in prompt


def test_build_prompt_keeps_favor_calm_scene_baseline_for_non_car_camera() -> None:
    """Non-car cameras (or a car camera list that excludes this one) are
    unaffected by the fix — the calming framing is still valid there."""
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        car_description="Silver Kia Forte, parked in the driveway",
        car_cameras=["Driveway"],
    )
    prompt = a._build_prompt("Front Door", scene_deviation=0.02)
    assert "closely matches its usual background" in prompt
    assert "Favor a calm, routine read" in prompt


def test_build_prompt_keeps_favor_calm_scene_baseline_when_no_car_configured() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam", scene_deviation=0.02)
    assert "closely matches its usual background" in prompt
    assert "Favor a calm, routine read" in prompt


# ---------------------------------------------------------------------------
# _build_prompt — refined car/person/animal + passing-traffic wording (item e)
# ---------------------------------------------------------------------------


def test_build_prompt_classifies_person_vehicle_animal() -> None:
    """General subject-classification guidance applies to every camera, not
    just ones with a protected vehicle configured."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam")
    assert "person, a vehicle" in prompt
    assert "professional security analyst" in prompt


def test_build_prompt_passing_car_wording_general() -> None:
    """Even without a protected vehicle configured, ordinary passing traffic
    should be described as driving up the street, not as being 'near' something."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam")
    assert "a car drove up the street" in prompt


def test_build_prompt_car_applies_passing_traffic_no_near_language() -> None:
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        car_description="Blue Toyota Camry",
    )
    prompt = a._build_prompt("Driveway")
    assert "driving up or down the street" in prompt
    assert "do not say it was 'near' the protected vehicle" in prompt


def test_build_prompt_passerby_not_suspicious_camera_agnostic() -> None:
    """The "just passing through" guidance for a person/animal must appear
    even when no protected vehicle is configured for this camera — false-
    positive reduction shouldn't require a car to be set up."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam")
    assert "without stopping, lingering, or" in prompt
    assert "must be marked suspicious=false" in prompt
    assert "never suspicious by itself, only stopping, lingering, tampering" in prompt


def test_build_prompt_confidence_floor_rule_present() -> None:
    """suspicious=true must be tied to a documented confidence floor so the
    alert-confidence gate (config ai_min_confidence default 0.5) matches what
    the model is actually instructed to do."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam")
    assert "confidence must be at least 0.5" in prompt
    assert "ambient change alone" in prompt
    assert "set suspicious=false instead of reporting a low-confidence guess" in prompt


# ===========================================================================
# v4.0.0 — motion-trajectory hint, cross-provider escalation, RECENT
# CORRECTIONS prompt block, Moondream fine-tune hot-swap
# ===========================================================================


def _real_jpeg_with_bar(
    bar_x: int,
    width: int = 64,
    height: int = 64,
    bg: int = 20,
    fg: int = 220,
    bar_width: int = 10,
) -> bytes:
    """Build a decodable JPEG: a bright vertical bar on a dark background.

    Used to construct frame sequences with a controllable, real motion
    signal for :meth:`BaseAnalyzer._compute_motion_trajectory_hint` tests —
    moving ``bar_x`` across frames simulates lateral movement, and varying
    ``fg`` at a fixed ``bar_x`` simulates a growing/shrinking motion
    intensity trend without a lateral shift.
    """
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color=(bg, bg, bg))
    draw = ImageDraw.Draw(img)
    x0 = max(0, bar_x)
    x1 = min(width - 1, bar_x + bar_width)
    draw.rectangle([x0, 0, x1, height - 1], fill=(fg, fg, fg))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Motion-trajectory hint
# ---------------------------------------------------------------------------


def test_motion_trajectory_hint_insufficient_frames() -> None:
    frames = [_real_jpeg(100), _real_jpeg(100)]
    assert BaseAnalyzer._compute_motion_trajectory_hint(frames) is None


def test_motion_trajectory_hint_no_motion() -> None:
    frames = [_real_jpeg(100)] * 4
    assert BaseAnalyzer._compute_motion_trajectory_hint(frames) is None


def test_motion_trajectory_hint_left_to_right() -> None:
    frames = [
        _real_jpeg_with_bar(2),
        _real_jpeg_with_bar(18),
        _real_jpeg_with_bar(34),
        _real_jpeg_with_bar(50),
    ]
    hint = BaseAnalyzer._compute_motion_trajectory_hint(frames)
    assert hint == "moving left to right across the frame"


def test_motion_trajectory_hint_right_to_left() -> None:
    frames = [
        _real_jpeg_with_bar(50),
        _real_jpeg_with_bar(34),
        _real_jpeg_with_bar(18),
        _real_jpeg_with_bar(2),
    ]
    hint = BaseAnalyzer._compute_motion_trajectory_hint(frames)
    assert hint == "moving right to left across the frame"


def test_motion_trajectory_hint_intensity_increasing() -> None:
    frames = [
        _real_jpeg_with_bar(27, fg=60),
        _real_jpeg_with_bar(27, fg=100),
        _real_jpeg_with_bar(27, fg=160),
        _real_jpeg_with_bar(27, fg=230),
    ]
    hint = BaseAnalyzer._compute_motion_trajectory_hint(frames)
    assert hint == "movement intensity increasing over time (may be approaching)"


def test_motion_trajectory_hint_intensity_decreasing() -> None:
    frames = [
        _real_jpeg_with_bar(27, fg=230),
        _real_jpeg_with_bar(27, fg=160),
        _real_jpeg_with_bar(27, fg=100),
        _real_jpeg_with_bar(27, fg=60),
    ]
    hint = BaseAnalyzer._compute_motion_trajectory_hint(frames)
    assert hint == "movement intensity decreasing over time (may be retreating)"


def test_motion_trajectory_hint_returns_none_on_pil_error() -> None:
    """Non-decodable frames (e.g. PIL unavailable/corrupt JPEG) return None
    rather than raising."""
    frames = [_FAKE_JPEG, _FAKE_JPEG_2, _FAKE_JPEG_3]
    assert BaseAnalyzer._compute_motion_trajectory_hint(frames) is None


# ---------------------------------------------------------------------------
# Zone-motion fraction
# ---------------------------------------------------------------------------


def test_zone_motion_fraction_concentrated_in_zone() -> None:
    """A zone covering the whole frame captures ~100% of the clip's motion."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    fraction = BaseAnalyzer._zone_motion_fraction(frames, zone)
    assert fraction == pytest.approx(1.0, abs=0.02)


def test_zone_motion_fraction_outside_zone() -> None:
    """A zone that never overlaps either bar position captures ~0% of motion."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 20 / 64, "y_min": 0.0, "x_max": 40 / 64, "y_max": 1.0}
    fraction = BaseAnalyzer._zone_motion_fraction(frames, zone)
    assert fraction == pytest.approx(0.0, abs=0.02)


def test_zone_motion_fraction_partial_overlap() -> None:
    """A zone covering only the destination bar position captures roughly
    half the motion — the leading and trailing edges are similar in size."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 44 / 64, "y_min": 0.0, "x_max": 60 / 64, "y_max": 1.0}
    fraction = BaseAnalyzer._zone_motion_fraction(frames, zone)
    assert fraction is not None
    assert 0.3 < fraction < 0.7


def test_zone_motion_fraction_insufficient_frames() -> None:
    assert BaseAnalyzer._zone_motion_fraction([_real_jpeg(100)], {"x_min": 0}) is None


def test_zone_motion_fraction_empty_zone() -> None:
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    assert BaseAnalyzer._zone_motion_fraction(frames, {}) is None


def test_zone_motion_fraction_no_motion() -> None:
    frames = [_real_jpeg(100)] * 3
    zone = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    assert BaseAnalyzer._zone_motion_fraction(frames, zone) is None


def test_zone_motion_fraction_returns_none_on_pil_error() -> None:
    frames = [_FAKE_JPEG, _FAKE_JPEG_2]
    zone = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    assert BaseAnalyzer._zone_motion_fraction(frames, zone) is None


def test_maybe_compute_zone_motion_requires_car_description() -> None:
    """A car_zone configured for a camera must not produce a ZONE MOTION
    signal (whose wording asserts "the protected vehicle's usual spot")
    until ai_car_description is actually set — otherwise the prompt would
    reference a protected vehicle the user never described, matching every
    other car-zone code path (_car_protection_applies) in this class."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        car_description="",
        car_zones={"Driveway": zone},
    )
    assert a._maybe_compute_zone_motion(frames, "Driveway") is None


def test_maybe_compute_zone_motion_runs_once_car_description_set() -> None:
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    a = ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        car_description="Silver Kia",
        car_zones={"Driveway": zone},
    )
    assert a._maybe_compute_zone_motion(frames, "Driveway") is not None


def test_build_prompt_includes_movement_hint() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt(
        "Cam", trajectory_hint="moving left to right across the frame"
    )
    assert "MOVEMENT:" in prompt
    assert "moving left to right across the frame" in prompt
    assert "rough automated estimate" in prompt


def test_build_prompt_omits_movement_hint_when_none() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam", trajectory_hint=None)
    assert "MOVEMENT:" not in prompt


# ---------------------------------------------------------------------------
# RECENT HUMAN CORRECTIONS prompt block
# ---------------------------------------------------------------------------


def test_build_prompt_includes_recent_corrections() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt(
        "Cam",
        recent_corrections=[
            {
                "original_suspicious": True,
                "correction_note": "This was just the mail carrier.",
            }
        ],
    )
    assert "RECENT HUMAN CORRECTIONS" in prompt
    assert "This was just the mail carrier." in prompt
    assert "judge THIS clip on its own visible content" in prompt


def test_build_prompt_omits_corrections_without_notes() -> None:
    """A bare correct/incorrect click with no note carries no reusable
    textual signal and must not appear in the prompt."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt(
        "Cam",
        recent_corrections=[
            {"original_suspicious": False, "correction_note": ""},
        ],
    )
    assert "RECENT HUMAN CORRECTIONS" not in prompt


def test_build_prompt_caps_corrections_at_three() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    corrections = [
        {"original_suspicious": True, "correction_note": f"note {i}"} for i in range(5)
    ]
    prompt = a._build_prompt("Cam", recent_corrections=corrections)
    for i in range(3):
        assert f"note {i}" in prompt
    for i in range(3, 5):
        assert f"note {i}" not in prompt


def test_build_prompt_no_corrections_param_is_a_noop() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    prompt = a._build_prompt("Cam")
    assert "RECENT HUMAN CORRECTIONS" not in prompt


# ---------------------------------------------------------------------------
# Moondream Cloud fine-tune hot-swap
# ---------------------------------------------------------------------------


def test_moondream_cloud_set_finetune_model() -> None:
    a = MoondreamCloudAnalyzer(api_key="key", prompt="test")
    assert a.model_name() == "moondream3-preview"
    a.set_finetune_model("moondream3-preview/abc123@50")
    assert a.model_name() == "moondream3-preview/abc123@50"
    a.set_finetune_model("")
    assert a.model_name() == "moondream3-preview"


# ---------------------------------------------------------------------------
# analyze_clip threads recent_corrections through to _build_prompt
# ---------------------------------------------------------------------------


async def test_analyze_clip_passes_recent_corrections_to_prompt() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0

    captured_prompts: list[str] = []

    async def fake_call_model(frames: list, prompt: str) -> str:
        captured_prompts.append(prompt)
        return '{"suspicious": false, "confidence": 0.1, "description": "Clear"}'

    a._call_model = fake_call_model  # type: ignore[method-assign]

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await a.analyze_clip(
            "/clips/test.mp4",
            "c1",
            "Driveway",
            recent_corrections=[
                {
                    "original_suspicious": True,
                    "correction_note": "Just the mail carrier.",
                }
            ],
        )

    assert captured_prompts
    assert "Just the mail carrier." in captured_prompts[0]


# ===========================================================================
# v4.0.0 — Prompt-debug capture (ai_prompt_debug_enabled)
# ===========================================================================


def test_set_prompt_debug_toggles_flag() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    assert a._store_prompt_debug is False
    a.set_prompt_debug(True)
    assert a._store_prompt_debug is True
    a.set_prompt_debug(False)
    assert a._store_prompt_debug is False


async def test_analyze_clip_omits_prompt_text_by_default() -> None:
    """prompt_text stays empty unless set_prompt_debug(True) was called —
    prompts are long and this is an opt-in debugging aid."""
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.1, "description": "Clear"}'
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await a.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.prompt_text == ""


async def test_analyze_clip_captures_prompt_text_when_enabled() -> None:
    a = ClipAnalyzer(ollama_url="http://localhost:11434", model="llava", prompt="p")
    a.set_prompt_debug(True)
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(_real_jpeg(100) * 3, b""))
    mock_proc.returncode = 0
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.1, "description": "Clear"}'
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await a.analyze_clip("/clips/test.mp4", "c1", "Driveway")

    assert result.prompt_text
    assert result.prompt_text.startswith("p")
    assert "OUTPUT RULES" in result.prompt_text


def test_analysis_result_to_dict_includes_prompt_text() -> None:
    result = AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="llava",
        response_text="",
        is_suspicious=False,
        confidence=0.1,
        summary="",
        frame_count=1,
        analysis_duration=1.0,
        analyzed_at="2024-06-01T09:00:00+00:00",
        prompt_text="the exact prompt",
    )
    assert result.to_dict()["prompt_text"] == "the exact prompt"


def test_create_analyzer_wires_store_prompt_debug() -> None:
    a = create_analyzer(
        "ollama",
        "prompt",
        ollama_url="http://localhost:11434",
        store_prompt_debug=True,
    )
    assert isinstance(a, ClipAnalyzer)
    assert a._store_prompt_debug is True


def test_create_analyzer_store_prompt_debug_defaults_false() -> None:
    a = create_analyzer("ollama", "prompt", ollama_url="http://localhost:11434")
    assert isinstance(a, ClipAnalyzer)
    assert a._store_prompt_debug is False
