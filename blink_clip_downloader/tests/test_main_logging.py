"""Logging setup in `blink_downloader.__main__`.

The module itself is excluded from the coverage requirement (it is the
process entry point), but the log filter below is real behaviour a user
sees in Home Assistant's own add-on log, so it is pinned here.
"""

from __future__ import annotations

import logging

from blink_downloader.__main__ import _HF_REQUEST_FILTER, _setup_logging


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_model_cache_requests_are_dropped() -> None:
    # Loading the vision pipeline's two transformers models emits roughly
    # thirty of these in a row — every cached config file revalidated
    # against the Hub, each a redirect followed by a 200. All normal, none
    # of it actionable, and it buries the add-on's own startup lines.
    assert not _HF_REQUEST_FILTER.filter(
        _record(
            "HTTP Request: HEAD https://huggingface.co/depth-anything/"
            "Depth-Anything-V2-Base-hf/resolve/main/config.json "
            '"HTTP/1.1 307 Temporary Redirect"'
        )
    )
    assert not _HF_REQUEST_FILTER.filter(
        _record(
            "HTTP Request: GET https://huggingface.co/api/resolve-cache/"
            'models/facebook/sam2.1-hiera-tiny/config.json "HTTP/1.1 200 OK"'
        )
    )


def test_ai_provider_requests_still_get_through() -> None:
    """The reason httpx keeps its level rather than being silenced."""
    assert _HF_REQUEST_FILTER.filter(
        _record(
            'HTTP Request: POST https://api.openai.com/v1/responses "HTTP/1.1 200 OK"'
        )
    )
    assert _HF_REQUEST_FILTER.filter(
        _record(
            'HTTP Request: POST http://10.0.0.5:11434/api/generate "HTTP/1.1 200 OK"'
        )
    )


def test_setup_logging_attaches_the_filter_once() -> None:
    httpx_logger = logging.getLogger("httpx")
    before = list(httpx_logger.filters)
    try:
        _setup_logging("info")
        _setup_logging("debug")
        assert httpx_logger.filters.count(_HF_REQUEST_FILTER) == 1
        # ...and httpx itself is not silenced along with it.
        assert httpx_logger.level == logging.NOTSET
        assert logging.getLogger("huggingface_hub").level == logging.WARNING
    finally:
        httpx_logger.filters = before
        logging.getLogger("huggingface_hub").setLevel(logging.NOTSET)
