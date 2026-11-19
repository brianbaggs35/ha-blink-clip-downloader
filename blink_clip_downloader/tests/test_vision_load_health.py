"""Every optional stage handles a failed model load the same way.

The behaviour lives in ``runtime.LoadGuard``/``record_load_failure`` (tested in
``test_vision_runtime.py``); what is checked here is that each stage is wired to
it: one attempt rather than one per call, a retry once the wait is over, one
log warning per distinct reason, and a health report that says why.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from blink_downloader.vision import (
    ObjectDetector,
    PoseEstimator,
    runtime,
)


@dataclass(frozen=True)
class _Stage:
    """One stage: how to build it and how to make its model fail to load."""

    name: str
    make: Callable[[], Any]
    #: The optional package the stage imports its model from.
    package: str
    #: The attribute of that package whose call loads the model.
    loader: str
    stage_name: str


STAGES = [
    _Stage(
        "object detection",
        ObjectDetector,
        "ultralytics",
        "YOLO",
        runtime.STAGE_OBJECT_DETECTION,
    ),
    _Stage(
        "pose estimation",
        PoseEstimator,
        "ultralytics",
        "YOLO",
        runtime.STAGE_POSE_ESTIMATION,
    ),
]


def _failing_package(
    monkeypatch: pytest.MonkeyPatch, stage: _Stage, effect: Any
) -> MagicMock:
    package = MagicMock()
    getattr(package, stage.loader).side_effect = effect
    monkeypatch.setitem(sys.modules, stage.package, package)
    return getattr(package, stage.loader)


@pytest.fixture(params=STAGES, ids=lambda stage: stage.name)
def stage(request: pytest.FixtureRequest) -> _Stage:
    return request.param


async def test_a_failed_load_is_attempted_once_not_per_call(
    stage: _Stage, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    loader = _failing_package(monkeypatch, stage, RuntimeError("download blocked"))
    instance = stage.make()
    assert instance.health() is None
    with caplog.at_level("DEBUG", logger="blink_downloader.vision"):
        for _ in range(4):
            assert await instance.ensure_ready() is False
    assert loader.call_count == 1
    assert [r.levelname for r in caplog.records].count("WARNING") == 1
    health = instance.health()
    assert health is not None
    assert health.stage == stage.stage_name
    assert health.state == "failed"
    assert health.detail == "RuntimeError: download blocked"
    assert health.permanent is False


async def test_it_tries_again_once_the_wait_is_over(
    stage: _Stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = _failing_package(monkeypatch, stage, [RuntimeError("blip"), MagicMock()])
    now = [1000.0]
    instance = stage.make()
    instance._load_guard = runtime.LoadGuard(stage.stage_name, lambda: now[0])
    assert await instance.ensure_ready() is False
    assert await instance.ensure_ready() is False
    assert loader.call_count == 1
    now[0] += 61
    assert await instance.ensure_ready() is True
    health = instance.health()
    assert health is not None
    assert health.state == "ready"


async def test_calls_waiting_on_a_failing_load_do_not_each_retry_it(
    stage: _Stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = _failing_package(
        monkeypatch, stage, lambda *_a, **_k: (time.sleep(0.05), 1 / 0)[1]
    )
    instance = stage.make()
    results = await asyncio.gather(*(instance.ensure_ready() for _ in range(3)))
    assert results == [False] * 3
    loader.assert_called_once()


async def test_a_missing_package_is_permanent(
    stage: _Stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, stage.package, raising=False)
    instance = stage.make()
    with patch("builtins.__import__", side_effect=ImportError("no package")):
        assert await instance.ensure_ready() is False
    health = instance.health()
    assert health is not None
    assert health.permanent is True
    assert health.retry_in is None
    assert stage.package in health.detail


async def test_an_incompatible_cpu_is_permanent(
    stage: _Stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "torch_cpu_compatible", lambda: False)
    instance = stage.make()
    assert await instance.ensure_ready() is False
    health = instance.health()
    assert health is not None
    assert health.permanent is True
