"""On-screen text recognition via the built-in Windows OCR (WinRT).

Uses ``Windows.Media.Ocr`` through ``winsdk`` — offline, ships with Windows,
no external binary to bundle. Recognized words carry bounding boxes in the
captured frame's pixel coordinates, which the action layer uses to click the
right button/row by its label.

Input is the in-memory BGRA bytes produced by the capturer (no temp files).
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image
from winsdk.windows.graphics.imaging import BitmapDecoder
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream


@dataclass(frozen=True)
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


@dataclass(frozen=True)
class Line:
    text: str
    words: tuple[Word, ...]


@dataclass
class OcrResult:
    width: int
    height: int
    lines: tuple[Line, ...]

    @property
    def words(self) -> list[Word]:
        return [w for line in self.lines for w in line.words]

    @property
    def full_text(self) -> str:
        return "\n".join(line.text for line in self.lines)


class OcrError(Exception):
    """Raised when the OCR engine is unavailable."""


@lru_cache(maxsize=1)
def _engine() -> OcrEngine:
    eng = OcrEngine.try_create_from_user_profile_languages()
    if eng is None:
        raise OcrError(
            "No Windows OCR language is installed. Add one under "
            "Settings > Time & Language > Language > Optional features."
        )
    return eng


def _bgra_to_png(bgra: bytes, width: int, height: int) -> bytes:
    img = Image.frombuffer("RGBA", (width, height), bgra, "raw", "BGRA", 0, 1)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def _recognize_png(png: bytes) -> tuple[int, int, tuple[Line, ...]]:
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await _engine().recognize_async(bitmap)

    lines: list[Line] = []
    for line in result.lines:
        words = tuple(
            Word(
                text=w.text,
                x=round(w.bounding_rect.x),
                y=round(w.bounding_rect.y),
                w=round(w.bounding_rect.width),
                h=round(w.bounding_rect.height),
            )
            for w in line.words
        )
        lines.append(Line(text=line.text, words=words))
    return bitmap.pixel_width, bitmap.pixel_height, tuple(lines)


def recognize_bgra(bgra: bytes, width: int, height: int) -> OcrResult:
    """Recognize text in an in-memory BGRA frame. Boxes are in frame pixels."""
    png = _bgra_to_png(bgra, width, height)
    w, h, lines = asyncio.run(_recognize_png(png))
    return OcrResult(width=w, height=h, lines=lines)


def recognize_bgra_region(
    bgra: bytes,
    width: int,
    height: int,
    region: tuple[float, float, float, float],
    upscale: int = 3,
) -> OcrResult:
    """Recognize text inside a normalized sub-region of a BGRA frame.

    ``region`` is ``(x0, y0, x1, y1)`` in 0..1 client fractions, so one definition
    works at any window resolution. Cropping to just the target (e.g. the timer)
    removes surrounding HUD noise and lets us upscale the few glyphs, which WinRT
    OCR reads far more reliably than a full, busy frame. Boxes are in the upscaled
    crop's pixels (callers that only parse the text don't care).
    """
    x0, y0, x1, y1 = region
    img = Image.frombuffer("RGBA", (width, height), bgra, "raw", "BGRA", 0, 1)
    px0, py0 = max(0, int(x0 * width)), max(0, int(y0 * height))
    px1, py1 = min(width, int(x1 * width)), min(height, int(y1 * height))
    if px1 - px0 < 2 or py1 - py0 < 2:
        return OcrResult(width=0, height=0, lines=())
    crop = img.crop((px0, py0, px1, py1))
    if upscale > 1:
        crop = crop.resize(
            (crop.width * upscale, crop.height * upscale), Image.LANCZOS
        )
    buf = io.BytesIO()
    crop.save(buf, "PNG")
    w, h, lines = asyncio.run(_recognize_png(buf.getvalue()))
    return OcrResult(width=w, height=h, lines=lines)


def recognize_file(path: str) -> OcrResult:
    """Recognize text in an image file (for testing / template authoring)."""
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    bgra = img.tobytes("raw", "BGRA")
    return recognize_bgra(bgra, w, h)
