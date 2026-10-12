"""Object detection on Hugging Face's servers, against a stand-in for them.

``RemoteObjectDetector`` takes its base URL as an argument, so these run the
real client against a small aiohttp server that answers the way the
serverless ``object-detection`` task does (a list of ``label``/``score``/
``box`` objects), and the real :class:`VisionPipeline` against a patched
detector. What they pin is the behaviour that makes the remote mode safe to
turn on: a frame that could not be examined fails the clip's detection
instead of reading as an empty driveway, and depth and contact segmentation
are neither run nor loaded.
"""

from __future__ import annotations

import base64
import io
import logging
import sys
from collections.abc import AsyncGenerator, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from blink_downloader.vision import (
    SOURCE_CONTACT_SEGMENTATION,
    SOURCE_DEPTH_ESTIMATION,
    ContactSegmenter,
    DepthEstimator,
    DetectedObject,
    ObjectDetector,
    RemoteObjectDetector,
    VisionConfig,
    VisionPipeline,
    remote_detection,
)


def _jpeg(shade: int = 128) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 60), (shade, shade, shade)).save(buf, format="JPEG")
    return buf.getvalue()


def _box(xmin: Any, ymin: Any, xmax: Any, ymax: Any) -> dict[str, Any]:
    return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}


def _hit(label: str, score: float, box: dict[str, Any]) -> dict[str, Any]:
    return {"label": label, "score": score, "box": box}


Reply = Callable[[bytes], tuple[int, Any]]


class FakeInference:
    """``router.huggingface.co/hf-inference/models/...`` on a local port."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: Reply = lambda _frame: (200, [])

    async def detect(self, request: web.Request) -> web.Response:
        body = await request.json()
        frame = base64.b64decode(body["inputs"])
        self.requests.append(
            {
                "path": request.path,
                "auth": request.headers.get("Authorization"),
                "content_type": request.headers.get("Content-Type"),
                "frame": frame,
                "parameters": body.get("parameters"),
            }
        )
        status, body = self.reply(frame)
        headers = {"Retry-After": "0"} if status == 429 else {}
        if isinstance(body, str):
            return web.Response(
                text=body, status=status, content_type="text/html", headers=headers
            )
        return web.json_response(body, status=status, headers=headers)


@pytest.fixture
async def inference() -> AsyncGenerator[tuple[FakeInference, TestServer]]:
    fake = FakeInference()
    app = web.Application()
    app.router.add_post("/hf-inference/models/{org}/{name}", fake.detect)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, server
    finally:
        await server.close()


@pytest.fixture(autouse=True)
def _decodable_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the pipeline read a frame's size without OpenCV installed.

    ``imaging._frame_dimensions`` decodes with OpenCV, an optional
    dependency that CI (and most dev machines) do not have. This stands in
    for the one call it makes by actually decoding the JPEG, so the sizes the
    security layer sees are real rather than invented.
    """

    def imdecode(buffer: Any, _flags: int) -> Any:
        image = Image.open(io.BytesIO(buffer.tobytes())).convert("RGB")
        return np.asarray(image)[:, :, ::-1]

    monkeypatch.setitem(
        sys.modules, "cv2", SimpleNamespace(IMREAD_COLOR=1, imdecode=imdecode)
    )


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries are real, but their pauses are not."""
    monkeypatch.setattr(remote_detection, "_MAX_WAIT_SECONDS", 0.0)


def _detector(
    server: TestServer,
    model: str = "facebook/detr-resnet-50",
    token: str = "hf_good",
) -> RemoteObjectDetector:
    base = f"{str(server.make_url('')).rstrip('/')}/hf-inference/models"
    return RemoteObjectDetector(model, token, base_url=base)


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------


async def test_detections_become_the_pipelines_own_objects_with_no_track_id(
    inference: tuple[FakeInference, TestServer],
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (
        200,
        [
            _hit("person", 0.93, _box(10, 12, 40, 58)),
            _hit("Car", 0.71, _box(35, 20, 95, 55)),
        ],
    )
    found = await _detector(server).detect([_jpeg()])
    assert found == [
        DetectedObject("person", 0.93, (10.0, 12.0, 40.0, 58.0), None, 0),
        DetectedObject("car", 0.71, (35.0, 20.0, 95.0, 55.0), None, 0),
    ]


async def test_each_detection_knows_which_frame_it_came_from_in_order(
    inference: tuple[FakeInference, TestServer],
) -> None:
    fake, server = inference
    by_frame = {_jpeg(shade): shade for shade in (10, 20, 30, 40, 50, 60)}
    fake.reply = lambda frame: (
        200,
        [_hit("person", 0.9, _box(by_frame[frame], 1, by_frame[frame] + 5, 9))],
    )
    found = await _detector(server).detect(list(by_frame))
    assert found is not None
    assert [(d.frame_index, d.box[0]) for d in found] == [
        (0, 10.0),
        (1, 20.0),
        (2, 30.0),
        (3, 40.0),
        (4, 50.0),
        (5, 60.0),
    ]


async def test_the_frame_goes_up_in_the_documented_json_form_with_our_threshold(
    inference: tuple[FakeInference, TestServer],
) -> None:
    """Raw image bytes are only accepted when no parameters are sent, so the
    request is the task's JSON form -- base64 ``inputs`` plus a ``threshold``
    -- which also makes the confidence floor ours rather than a server
    default."""
    fake, server = inference
    frame = _jpeg()
    await _detector(server, model="facebook/detr-resnet-101").detect([frame])
    (sent,) = fake.requests
    assert sent["path"] == "/hf-inference/models/facebook/detr-resnet-101"
    assert sent["auth"] == "Bearer hf_good"
    assert sent["content_type"].startswith("application/json")
    assert sent["frame"] == frame
    assert sent["parameters"] == {"threshold": 0.5}


async def test_only_what_the_security_layer_reads_is_kept(
    inference: tuple[FakeInference, TestServer],
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (
        200,
        [
            _hit("person", 0.95, _box(1, 1, 20, 50)),
            _hit("chair", 0.99, _box(1, 1, 20, 50)),  # not a class we surface
            _hit("dog", 0.49, _box(1, 1, 20, 50)),  # under the confidence floor
            _hit("dog", 0.5, _box(2, 2, 20, 50)),  # exactly at it
        ],
    )
    found = await _detector(server).detect([_jpeg()])
    assert found is not None
    assert [(d.label, d.confidence) for d in found] == [("person", 0.95), ("dog", 0.5)]


@pytest.mark.parametrize(
    "item",
    [
        "not an object",
        {"label": "person", "score": 0.9},  # no box
        {"label": "person", "score": 0.9, "box": "x"},
        {"label": "person", "score": "high", "box": _box(1, 1, 5, 5)},
        {"label": "person", "score": 0.9, "box": {"xmin": 1, "ymin": 1}},
        {"label": "person", "score": 0.9, "box": _box(1, 1, "wide", 5)},
        {"label": "person", "score": 0.9, "box": _box(5, 1, 5, 9)},  # no width
        {"label": "person", "score": 0.9, "box": _box(1, 9, 5, 4)},  # upside down
    ],
)
async def test_a_malformed_box_is_skipped_not_fatal(
    inference: tuple[FakeInference, TestServer], item: Any
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (200, [item, _hit("car", 0.8, _box(1, 1, 30, 20))])
    found = await _detector(server).detect([_jpeg()])
    assert found is not None
    assert [d.label for d in found] == ["car"]


async def test_looking_and_finding_nothing_is_not_the_same_as_not_looking(
    inference: tuple[FakeInference, TestServer],
) -> None:
    """``[]`` is evidence (an empty driveway); ``None`` is the absence of it."""
    assert await _detector(inference[1]).detect([_jpeg(), _jpeg(9)]) == []


async def test_no_frames_is_not_a_look_at_all(
    inference: tuple[FakeInference, TestServer],
) -> None:
    assert await _detector(inference[1]).detect([]) is None
    assert inference[0].requests == []


async def test_no_token_is_unavailable_and_says_why(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert await _detector(inference[1], token="").detect([_jpeg()]) is None
    assert "huggingface_api_key nor hf_token" in caplog.text
    assert inference[0].requests == []


# ---------------------------------------------------------------------------
# A frame that could not be examined fails the clip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, "rejected the token"),
        (402, "no credit left"),
        (403, "Make calls to Inference Providers"),
        (404, "facebook/detr-resnet-50"),
    ],
)
async def test_a_permanent_refusal_ends_the_clips_detection_at_once(
    inference: tuple[FakeInference, TestServer],
    caplog: pytest.LogCaptureFixture,
    status: int,
    needle: str,
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (status, {"error": "refused"})
    frames = [_jpeg(shade) for shade in range(10, 130, 10)]  # twelve frames
    with caplog.at_level(logging.WARNING):
        assert await _detector(server).detect(frames) is None
    assert needle in caplog.text
    # Nothing a later frame finds could rescue the clip, and each request is
    # billed: at most the frames already in flight were sent.
    assert len(fake.requests) <= remote_detection._CONCURRENCY


async def test_a_refusal_the_user_can_fix_is_reported_over_a_hiccup(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = inference
    flaky, refused = _jpeg(1), _jpeg(2)
    fake.reply = lambda frame: (500, {}) if frame == flaky else (401, {})
    with caplog.at_level(logging.WARNING):
        assert await _detector(server).detect([flaky, refused]) is None
    assert "rejected the token" in caplog.text


async def test_a_hiccup_is_retried_and_the_clip_still_succeeds(
    inference: tuple[FakeInference, TestServer],
) -> None:
    fake, server = inference
    answers = iter(
        [
            (503, {"error": "Model is currently loading", "estimated_time": 12.5}),
            (429, {"error": "slow down"}),
            (200, [_hit("person", 0.9, _box(1, 1, 9, 9))]),
        ]
    )
    fake.reply = lambda _f: next(answers)
    found = await _detector(server).detect([_jpeg()])
    assert found is not None
    assert [d.label for d in found] == ["person"]
    assert len(fake.requests) == 3


async def test_retries_run_out(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (503, {"error": {"message": "overloaded"}})
    with caplog.at_level(logging.WARNING):
        assert await _detector(server).detect([_jpeg()]) is None
    assert len(fake.requests) == remote_detection._ATTEMPTS
    assert "HTTP 503" in caplog.text
    assert "overloaded" in caplog.text


async def test_another_server_error_is_retried_too(
    inference: tuple[FakeInference, TestServer],
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (501, "<html>not implemented</html>")
    assert await _detector(server).detect([_jpeg()]) is None
    assert len(fake.requests) == remote_detection._ATTEMPTS


async def test_a_rejected_request_is_not_retried(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (400, {"error": "bad image"})
    with caplog.at_level(logging.WARNING):
        assert await _detector(server).detect([_jpeg()]) is None
    assert len(fake.requests) == 1
    assert "HTTP 400" in caplog.text


async def test_a_gateway_page_where_the_answer_should_be_is_retried(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (200, "<html>Bad gateway</html>")
    with caplog.at_level(logging.WARNING):
        assert await _detector(server).detect([_jpeg()]) is None
    assert "not JSON" in caplog.text
    assert len(fake.requests) == remote_detection._ATTEMPTS


async def test_a_json_null_is_not_an_answer_either(
    inference: tuple[FakeInference, TestServer],
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (200, None)
    assert await _detector(server).detect([_jpeg()]) is None
    assert len(fake.requests) == remote_detection._ATTEMPTS


async def test_a_refused_token_is_recognised_even_when_the_body_is_a_web_page(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    """The router answers a *missing* token with a CDN's HTML page, and a
    gateway can do the same to any error. Read as "could not reach Hugging
    Face" it would be retried three times and blamed on the network; it is a
    401, and the user needs to hear that."""
    fake, server = inference
    fake.reply = lambda _f: (401, "<html><body>Unauthorized</body></html>")
    with caplog.at_level(logging.WARNING):
        assert await _detector(server).detect([_jpeg()]) is None
    assert "rejected the token" in caplog.text
    assert len(fake.requests) == 1


async def test_a_gateway_error_page_is_retried_as_a_server_failure(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (502, "<html>Bad gateway</html>")
    with caplog.at_level(logging.WARNING):
        assert await _detector(server).detect([_jpeg()]) is None
    assert "HTTP 502" in caplog.text
    assert len(fake.requests) == remote_detection._ATTEMPTS


async def test_anything_unexpected_fails_the_look_softly(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    """Like the local detector, a surprise is "could not look" -- the clip is
    still analyzed -- and never an exception out of the pipeline."""
    detector = _detector(inference[1])
    with (
        patch.object(
            RemoteObjectDetector, "_post", AsyncMock(side_effect=RuntimeError("boom"))
        ),
        caplog.at_level(logging.WARNING),
    ):
        assert await detector.detect([_jpeg(), _jpeg(9)]) is None
    assert "unexpected RuntimeError: boom" in caplog.text


async def test_an_answer_that_is_not_a_list_of_boxes_is_not_an_answer(
    inference: tuple[FakeInference, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = inference
    fake.reply = lambda _f: (200, {"unexpected": "shape"})
    with caplog.at_level(logging.WARNING):
        assert await _detector(server).detect([_jpeg()]) is None
    assert "other than a list of boxes" in caplog.text


async def test_unreachable_servers_are_a_failed_look(
    inference: tuple[FakeInference, TestServer],
) -> None:
    detector = _detector(inference[1])
    await inference[1].close()
    assert await detector.detect([_jpeg()]) is None


# ---------------------------------------------------------------------------
# How long to wait before trying again
# ---------------------------------------------------------------------------


def test_a_servers_own_estimate_is_waited_out_up_to_a_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote_detection, "_MAX_WAIT_SECONDS", 20.0)
    assert remote_detection._wait_for(1, 12.5) == 12.5
    assert remote_detection._wait_for(1, 300.0) == 20.0


def test_without_an_estimate_the_wait_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_detection, "_MAX_WAIT_SECONDS", 20.0)
    assert [remote_detection._wait_for(n, None) for n in (1, 2, 3)] == [1.0, 2.0, 4.0]
    assert remote_detection._wait_for(2, 0.0) == 2.0
    assert remote_detection._wait_for(2, -3.0) == 2.0


def test_the_server_hint_is_read_from_the_body_or_the_retry_after_header() -> None:
    assert remote_detection._server_hint(503, {}, {"estimated_time": 7}) == 7.0
    assert remote_detection._server_hint(429, {"Retry-After": "4"}, {}) == 4.0
    assert remote_detection._server_hint(429, {"Retry-After": "soon"}, {}) is None
    assert remote_detection._server_hint(429, {}, None) is None
    assert remote_detection._server_hint(503, {"Retry-After": "4"}, {}) is None


def test_an_error_body_is_trimmed_to_one_short_line() -> None:
    assert remote_detection._error_text({"error": "plain"}) == "plain"
    assert remote_detection._error_text({"error": {"message": "nested"}}) == "nested"
    assert len(remote_detection._error_text({"error": "x" * 500})) == 200
    assert remote_detection._error_text({"error": None}) == ""
    assert remote_detection._error_text("not a dict") == ""


# ---------------------------------------------------------------------------
# In the pipeline
# ---------------------------------------------------------------------------

#: A person overlapping a car in a 100x60 frame, so a subject/vehicle pair
#: exists for the depth and contact stages to be asked about.
_PAIR = [
    DetectedObject("person", 0.9, (10.0, 12.0, 40.0, 58.0), None, 0),
    DetectedObject("car", 0.9, (35.0, 20.0, 95.0, 55.0), None, 0),
]


def _remote_config(**overrides: Any) -> VisionConfig:
    return VisionConfig(
        enhanced_detection_enabled=True,
        vision_backend="huggingface",
        remote_detection_token="hf_good",
        **overrides,
    )


def test_the_backend_chooses_the_detector() -> None:
    assert isinstance(VisionPipeline(VisionConfig())._detector, ObjectDetector)
    assert isinstance(VisionPipeline(_remote_config())._detector, RemoteObjectDetector)


async def test_remote_detection_runs_neither_depth_nor_contact_and_says_so() -> None:
    pipeline = VisionPipeline(_remote_config())
    refuse = AsyncMock(side_effect=AssertionError("must not run in remote mode"))
    with (
        patch.object(RemoteObjectDetector, "detect", AsyncMock(return_value=_PAIR)),
        patch.object(DepthEstimator, "compare", refuse),
        patch.object(ContactSegmenter, "check_contact", refuse),
    ):
        hints = await pipeline.process_clip(
            [_jpeg()], car_description="Silver Kia", car_protection_applies=True
        )

    assert hints.detections == _PAIR
    assert hints.detection_hint is not None
    assert hints.depth_hint is None
    assert hints.contact_hint is None
    assert SOURCE_DEPTH_ESTIMATION in hints.unavailable_sources
    assert SOURCE_CONTACT_SEGMENTATION in hints.unavailable_sources
    refuse.assert_not_called()


async def test_pose_estimation_keeps_running_here_in_the_remote_mode() -> None:
    """Only detection moves. Pose has its own switch and its own local model,
    and the documentation says it stays on this device; a regression that
    silently dropped it in the remote mode would take that evidence away."""
    from blink_downloader.vision import PoseEstimator, PostureResult

    pipeline = VisionPipeline(_remote_config(pose_estimation_enabled=True))
    posture = PostureResult(reaching=True, confidence=0.9)
    with (
        patch.object(RemoteObjectDetector, "detect", AsyncMock(return_value=_PAIR)),
        patch.object(PoseEstimator, "analyze", AsyncMock(return_value=posture)) as pose,
    ):
        hints = await pipeline.process_clip(
            [_jpeg()], car_description="Silver Kia", car_protection_applies=True
        )
    pose.assert_awaited_once()
    assert hints.posture is posture
    assert hints.posture_hint is not None


async def test_remote_tracks_are_inferred_not_measured() -> None:
    """No track id comes back from the server, and none is made up: the
    security layer's own overlap association builds the tracks and marks them
    ``tracked=False``, which is what keeps the rules that need a measured
    track (routine door use, "N separate people") from firing on a guess."""
    pipeline = VisionPipeline(_remote_config())
    with patch.object(RemoteObjectDetector, "detect", AsyncMock(return_value=_PAIR)):
        hints = await pipeline.process_clip([_jpeg()])
    assert hints.tracks
    assert all(not track.tracked for track in hints.tracks)
    assert hints.tracking_hint is None


async def test_a_failed_remote_look_leaves_every_detection_stage_unavailable() -> None:
    pipeline = VisionPipeline(_remote_config())
    with patch.object(RemoteObjectDetector, "detect", AsyncMock(return_value=None)):
        hints = await pipeline.process_clip([_jpeg()])
    assert hints.detections is None
    assert SOURCE_DEPTH_ESTIMATION in hints.unavailable_sources
    assert SOURCE_CONTACT_SEGMENTATION in hints.unavailable_sources


async def test_local_mode_still_runs_depth_and_contact() -> None:
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    depth = AsyncMock(return_value=None)
    contact = AsyncMock(return_value=None)
    with (
        patch.object(ObjectDetector, "detect", AsyncMock(return_value=_PAIR)),
        patch.object(DepthEstimator, "compare", depth),
        patch.object(ContactSegmenter, "check_contact", contact),
    ):
        hints = await pipeline.process_clip(
            [_jpeg()], car_description="Silver Kia", car_protection_applies=True
        )
    depth.assert_awaited_once()
    contact.assert_awaited_once()
    assert SOURCE_DEPTH_ESTIMATION in hints.unavailable_sources
