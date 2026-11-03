"""Prepare verified PNG evidence for model requests.

The model gateway accepts inline PNG data URLs, so the byte limit must be
checked after encoding as well as before it.  Frames are compressed only for
the model payload; the native frame on disk is never changed.
"""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image


PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
DATA_URL_PREFIX = 'data:image/png;base64,'
# Keep a generous margin below both the local one-MiB check and the gateway
# data-URL limit.  This leaves room for future policy changes.
SAFE_PNG_BYTES = 900 * 1024
MAX_DATA_URL_CHARS = 1_398_126


def data_url(raw: bytes) -> str:
    """Return the bounded inline PNG data URL used by the providers."""
    return DATA_URL_PREFIX + base64.b64encode(raw).decode('ascii')


def _fits(raw: bytes, max_bytes: int, max_url_chars: int) -> bool:
    return len(raw) <= max_bytes and len(data_url(raw)) <= max_url_chars


def _encode(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format='PNG', optimize=True, compress_level=9)
    return out.getvalue()


def _quantize(image: Image.Image, colors: int) -> Image.Image:
    rgb = image.convert('RGB')
    # Pillow has changed enum names across the versions shipped with the
    # desktop bundle.  The integer fallback keeps this helper portable.
    method = getattr(getattr(Image, 'Quantize', None), 'MEDIANCUT', 0)
    dither = getattr(getattr(Image, 'Dither', None), 'NONE', 0)
    return rgb.quantize(colors=colors, method=method, dither=dither)


def _resize(image: Image.Image, scale: float) -> Image.Image:
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    resampling = getattr(getattr(Image, 'Resampling', None), 'LANCZOS', Image.LANCZOS)
    return image.resize((width, height), resampling)


def fit_png_bytes(raw: bytes, *, max_bytes: int = SAFE_PNG_BYTES,
                  max_url_chars: int = MAX_DATA_URL_CHARS) -> bytes:
    """Return *raw* or a compressed derived PNG that fits both limits.

    The input is expected to have passed the caller's source-hash check.  A
    derived payload is held in memory and is never written over the evidence
    file.  The image is reduced progressively only when lossless PNG
    optimization and palette quantization are insufficient.
    """
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError('Only PNG frames can be sent to the visual model')
    if _fits(raw, max_bytes, max_url_chars):
        return raw
    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except Exception as exc:  # pragma: no cover - depends on malformed input
        raise ValueError('Oversized PNG frame cannot be decoded for compression') from exc

    candidates = [image]
    if image.mode not in ('RGB', 'RGBA', 'L', 'P'):
        candidates.append(image.convert('RGBA'))
    if image.mode == 'RGBA':
        candidates.append(image.convert('RGB'))
    for candidate in candidates:
        encoded = _encode(candidate)
        if _fits(encoded, max_bytes, max_url_chars):
            return encoded

    # Product frames are normally 540x960, and a 256-color PNG preserves
    # their visual content well while cutting unoptimized captures sharply.
    base = image.convert('RGB')
    for colors in (256, 128, 64, 32, 16):
        encoded = _encode(_quantize(base, colors))
        if _fits(encoded, max_bytes, max_url_chars):
            return encoded

    # Extremely detailed or unusually large frames get smaller in a bounded
    # loop.  The gateway needs visual evidence, not the original pixel count.
    resized = base
    for _ in range(12):
        resized = _resize(resized, 0.85)
        for colors in (256, 128, 64, 32):
            encoded = _encode(_quantize(resized, colors))
            if _fits(encoded, max_bytes, max_url_chars):
                return encoded

    raise ValueError('PNG frame cannot be reduced below the model payload limit')
