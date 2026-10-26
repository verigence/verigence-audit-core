"""Is a photographed gate pass good enough to keep? (2026-10-26)

The gate pass is hand written and photographed, so the check is about the
photo: large enough to read, in focus, and neither too dark nor washed out.
It runs on the server, so the website and the mobile app get the same answer.

Sharpness is the spread (variance) of a Laplacian-filtered grey copy, scaled to
a common size, taken over a grid of tiles. A page of handwriting is mostly
blank paper, so the whole-image figure would be low even for a sharp photo;
the 90th-percentile tile is the sharpest part of the page, which is what a
blurred photo lacks everywhere. The limits are settings, not constants of
nature: they were set on synthetic pages and need confirming against real
gate-pass photos (GATE_PASS_MIN_SHARPNESS and friends).
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps, ImageStat

ALLOWED_FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}
MAX_BYTES = 20 * 1024 * 1024
_NORMAL_LONG_SIDE = 1600
_GRID = 6
_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)


def _setting(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class ImageAssessment:
    ok: bool
    code: str | None
    message: str | None
    content_type: str | None
    extension: str | None
    width: int
    height: int
    sharpness: float
    brightness: float


def _refuse(code: str, message: str, *, width: int = 0, height: int = 0, sharpness: float = 0.0,
            brightness: float = 0.0) -> ImageAssessment:
    return ImageAssessment(False, code, message, None, None, width, height, sharpness, brightness)


def tile_sharpness(gray: Image.Image) -> float:
    """90th-percentile tile variance of the Laplacian (see module docstring)."""
    width, height = gray.size
    scale = _NORMAL_LONG_SIDE / max(width, height)
    if scale < 1:
        gray = gray.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
        width, height = gray.size
    edges = gray.filter(_LAPLACIAN)
    variances: list[float] = []
    for row in range(_GRID):
        for col in range(_GRID):
            box = (col * width // _GRID, row * height // _GRID, (col + 1) * width // _GRID, (row + 1) * height // _GRID)
            if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                continue
            variances.append(float(ImageStat.Stat(edges.crop(box)).var[0]))
    if not variances:
        return 0.0
    variances.sort()
    return variances[min(len(variances) - 1, int(0.9 * len(variances)))]


def assess_gate_pass_image(data: bytes) -> ImageAssessment:
    min_long = int(_setting("GATE_PASS_MIN_LONG_SIDE", 1600))
    min_short = int(_setting("GATE_PASS_MIN_SHORT_SIDE", 1200))
    min_sharp = _setting("GATE_PASS_MIN_SHARPNESS", 150.0)
    dark, bright = _setting("GATE_PASS_MIN_BRIGHTNESS", 50.0), _setting("GATE_PASS_MAX_BRIGHTNESS", 240.0)
    if not data:
        return _refuse("GATE_PASS_IMAGE_EMPTY", "The file is empty. Take the photo again.")
    if len(data) > MAX_BYTES:
        return _refuse("GATE_PASS_IMAGE_TOO_LARGE", "The photo is larger than 20 MB. Take it again at a normal camera setting.")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:  # noqa: BLE001 - any undecodable file is simply "not a usable photo"
        return _refuse("GATE_PASS_IMAGE_UNREADABLE", "This file is not a photo that can be read. Upload a JPEG or PNG photo.")
    fmt = (image.format or "").upper()
    if fmt not in ALLOWED_FORMATS:
        return _refuse("GATE_PASS_IMAGE_FORMAT", "Upload the gate pass as a JPEG or PNG photo.")
    image = ImageOps.exif_transpose(image)
    width, height = image.size
    long_side, short_side = max(width, height), min(width, height)
    if long_side < min_long or short_side < min_short:
        return _refuse(
            "GATE_PASS_IMAGE_LOW_RESOLUTION",
            f"The photo is too small ({width} x {height}). Take it closer, at the camera's full resolution; "
            f"at least {min_long} x {min_short} is needed so the handwriting can be read.",
            width=width, height=height,
        )
    gray = image.convert("L")
    brightness = float(ImageStat.Stat(gray).mean[0])
    sharpness = round(tile_sharpness(gray), 2)
    if brightness < dark:
        return _refuse("GATE_PASS_IMAGE_TOO_DARK", "The photo is too dark. Take it in better light.",
                       width=width, height=height, sharpness=sharpness, brightness=brightness)
    if brightness > bright:
        return _refuse("GATE_PASS_IMAGE_TOO_BRIGHT", "The photo is washed out. Avoid glare and direct light on the paper.",
                       width=width, height=height, sharpness=sharpness, brightness=brightness)
    if sharpness < min_sharp:
        return _refuse("GATE_PASS_IMAGE_BLURRED",
                       "The photo is blurred. Hold the phone steady, let it focus on the paper, and take it again.",
                       width=width, height=height, sharpness=sharpness, brightness=brightness)
    content_type, extension = ALLOWED_FORMATS[fmt]
    return ImageAssessment(True, None, None, content_type, extension, width, height, sharpness, brightness)
