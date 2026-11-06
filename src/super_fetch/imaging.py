"""Turn downloaded image bytes into an image an agent's model can look at."""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass, field

_MODEL_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
"""Formats vision models take as they are; anything else (AVIF, BMP, TIFF, ICO...) is converted."""
_MAX_SIDE = 1568
"""Longest side sent, in pixels: models scale bigger images down to about this anyway."""
_MAX_BYTES = 1_000_000
"""Images larger than this are re-encoded (smaller) even when their size and format are fine."""
_MAX_PIXELS = 50_000_000
"""Images with more pixels than this aren't decoded (decompression bombs)."""
_JPEG_QUALITY = 85


class ImageError(ValueError):
    """Bytes that aren't an image this server can read."""


@dataclass
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    source_format: str
    notes: list[str] = field(default_factory=list)


def is_svg(data: bytes, content_type: str) -> bool:
    if content_type == "image/svg+xml":
        return True
    head = data[:1024].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def prepare_image(data: bytes) -> PreparedImage:
    """Pass the image through when a model can take it as is, else scale it down and re-encode it."""
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = _MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            source_format = image.format or "unknown"
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
            if source_format in _MODEL_FORMATS and max(width, height) <= _MAX_SIDE and len(data) <= _MAX_BYTES:
                image.verify()  # cheap check that the bytes aren't cut off or corrupt
                notes = [f"Animated ({frames} frames): you see the first frame."] if frames > 1 else []
                return PreparedImage(data, _MODEL_FORMATS[source_format], width, height, source_format, notes)
            image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageError(f"Not an image this server can read: {exc}") from exc
    except (OSError, SyntaxError, ValueError) as exc:  # Pillow's errors for truncated or corrupt files
        raise ImageError(f"The image could not be decoded: {exc}") from exc

    notes = []
    if frames > 1:
        notes.append(f"Animated ({frames} frames): this is the first frame.")
    scaled = max(width, height) > _MAX_SIDE
    if scaled:
        image.thumbnail((_MAX_SIDE, _MAX_SIDE))
        notes.append(f"Scaled down from {width}x{height}.")
    if source_format not in _MODEL_FORMATS:
        notes.append(f"Converted from {source_format}.")
    elif not scaled:
        notes.append(f"Re-encoded to make it smaller (was {len(data)} bytes).")

    encoded = None
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
    if has_alpha:
        encoded = _encode(image.convert("RGBA"), "PNG")
        if len(encoded) > _MAX_BYTES:
            encoded = None  # too big as PNG: lose the transparency for a JPEG
    if encoded is None:
        rgb = image.convert("RGBA") if has_alpha else image.convert("RGB")
        if has_alpha:
            background = Image.new("RGB", rgb.size, "white")
            background.paste(rgb, mask=rgb.getchannel("A"))
            rgb = background
        encoded, mime_type = _encode(rgb, "JPEG"), "image/jpeg"
    else:
        mime_type = "image/png"
    return PreparedImage(encoded, mime_type, image.width, image.height, source_format, notes)


def _encode(image, fmt: str) -> bytes:
    out = io.BytesIO()
    if fmt == "JPEG":
        image.save(out, "JPEG", quality=_JPEG_QUALITY, optimize=True)
    else:
        image.save(out, fmt, optimize=True)
    return out.getvalue()
