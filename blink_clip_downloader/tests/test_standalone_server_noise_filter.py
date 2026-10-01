from __future__ import annotations

import importlib.util
import logging
from pathlib import Path


def _load_standalone_server_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "standalone_server.py"
    spec = importlib.util.spec_from_file_location("standalone_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expected_noise_filter_hides_missing_ffmpeg_paths() -> None:
    module = _load_standalone_server_module()
    record = logging.LogRecord(
        name="blink_downloader.ffmpeg_output",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=(
            "ffmpeg exited 254 extracting frames for "
            "/share/blink-clips/Garage/e2e-clip-008.mp4: Error opening input file"
        ),
        args=(),
        exc_info=None,
    )

    assert module._ExpectedE2ENoiseFilter().filter(record) is False
