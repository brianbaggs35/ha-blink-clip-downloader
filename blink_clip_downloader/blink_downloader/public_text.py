"""Picking text to show a person from a list the add-on wrote itself.

An exception's message can carry more than the add-on put there (a library's
own wording, a path, a value from the request). Where only a fixed set of
sentences should ever reach the browser, :func:`known_text` hands back the
add-on's own copy of the sentence that matches, or a fallback, so nothing but
text written in this repository can be shown. Stdlib only, like
``log_text``, so any module can use it.
"""

from __future__ import annotations

from collections.abc import Iterable


def known_text(candidate: str, known: Iterable[str], fallback: str) -> str:
    """The entry of *known* equal to *candidate*, else *fallback*.

    The result is always one of the add-on's own strings, never *candidate*
    itself, even when they are equal.
    """
    for entry in known:
        if entry == candidate:
            return entry
    return fallback
