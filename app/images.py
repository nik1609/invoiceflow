"""Accept a photo of an invoice or receipt. It is turned into a one page PDF so every later step (OCR, vision, evidence boxes) works the same."""
from __future__ import annotations

import io

from PIL import Image, ImageOps

MAX_PIXELS = 40_000_000
MAX_SIDE = 3000


def sniff(data: bytes) -> str | None:
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def to_pdf(data: bytes) -> bytes:
    """Raises ValueError with a message fit for the user."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            raise ValueError("That photo is too large. Use one under 40 megapixels.")
        img = ImageOps.exif_transpose(img)           # phones store rotation in metadata
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = bg
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, "PDF", resolution=200.0)
        return buf.getvalue()
    except ValueError:
        raise
    except Exception:  # noqa: BLE001
        raise ValueError("That image could not be opened. Try a JPG, PNG or WebP photo.")
