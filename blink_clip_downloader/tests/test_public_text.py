"""``known_text`` only ever hands back text the add-on wrote itself."""

from __future__ import annotations

from blink_downloader.public_text import known_text

_KNOWN = ("The disk is full.", "Camera not found")


def test_a_known_sentence_is_handed_back() -> None:
    assert known_text("Camera not found", _KNOWN, "Something failed.") == (
        "Camera not found"
    )


def test_it_is_the_add_ons_copy_that_is_returned_not_the_candidate() -> None:
    pieces = ["Camera ", "not found"]
    candidate = "".join(pieces)
    result = known_text(candidate, _KNOWN, "Something failed.")
    assert result == candidate
    assert result is _KNOWN[1]


def test_anything_else_gets_the_fallback() -> None:
    assert known_text("/data/secret.json: boom", _KNOWN, "Something failed.") == (
        "Something failed."
    )
    assert known_text("", (), "Something failed.") == "Something failed."
