"""Pictures for the Security Feed tests: a whole one, and the same one cut short."""

from __future__ import annotations

import io
import random

from PIL import Image


def whole_jpeg(seed: int = 1, size: tuple[int, int] = (64, 48)) -> bytes:
    """A complete JPEG. Different seeds give different pictures.

    Noise rather than a flat colour, so the compressed data is long enough that
    cutting it short loses real picture rather than only the end marker.
    """
    pixels = random.Random(seed).randbytes(size[0] * size[1] * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, pixels).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def cut_short(picture: bytes, keep: float = 0.6) -> bytes:
    """The first *keep* of *picture*: what arrives when a download ends early.

    A browser draws this without complaint, and leaves the rest of the tile black.
    """
    return picture[: int(len(picture) * keep)]
