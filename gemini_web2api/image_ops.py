"""Crop-and-composite helpers for masked image edits.

Gemini Web accepts images but has no native mask parameter. Sending the full
image plus a mask image is therefore only a soft instruction, and the model is
free to place new content elsewhere in the frame.

For a narrower edit we crop the source and mask to the mask's transparent
bounding box, let Gemini edit only that crop, then composite the result back
through the mask alpha. The original image is never replaced outside the
transparent region.

ImageMagick is used for the crop and composite steps. It is available on the
Serv00 production host; when it is unavailable the server falls back to the
legacy full-image soft-mask path.
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_EDIT_PIXELS = 40_000_000
_MAGICK_TIMEOUT_SEC = 120

# Gemini image generation is most predictable at these ratios. The crop box is
# expanded from the mask bounds to the closest ratio so the returned crop does
# not need an arbitrary stretch.
_SUPPORTED_ASPECTS = (
    (1, 1),
    (4, 3), (3, 4),
    (3, 2), (2, 3),
    (5, 4), (4, 5),
    (16, 9), (9, 16),
    (21, 9), (9, 21),
)


class ImageOpsUnavailable(RuntimeError):
    """Raised when the host has no supported image processing backend."""


@dataclass(frozen=True)
class MaskGeometry:
    width: int
    height: int
    bounds: tuple
    transparent_pixels: int
    total_pixels: int


@dataclass(frozen=True)
class CropEditPlan:
    crop_box: tuple
    crop_size: tuple
    crop_image: bytes
    crop_mask: bytes
    geometry: MaskGeometry


def _png_alpha(mask_bytes: bytes):
    """Decode an 8-bit non-interlaced PNG mask and return its alpha channel."""
    if not isinstance(mask_bytes, (bytes, bytearray)) or not mask_bytes.startswith(PNG_SIGNATURE):
        raise ValueError("mask must be a PNG image")

    data = bytes(mask_bytes)
    offset = 8
    width = height = bit_depth = color_type = interlace = None
    idat = []
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        if payload_end > len(data):
            raise ValueError("mask PNG is truncated")
        payload = data[payload_start:payload_end]
        if kind == b"IHDR":
            if length != 13:
                raise ValueError("mask PNG has an invalid header")
            (width, height, bit_depth, color_type, compression,
             filter_method, interlace) = struct.unpack(">IIBBBBB", payload)
            if compression != 0 or filter_method != 0:
                raise ValueError("mask PNG uses an unsupported compression method")
        elif kind == b"IDAT":
            idat.append(payload)
        elif kind == b"IEND":
            break
        offset = payload_end + 4  # length + type + payload + CRC

    if width is None or height is None or not idat:
        raise ValueError("mask PNG is missing image data")
    if width < 1 or height < 1:
        raise ValueError("mask PNG has invalid dimensions")
    if width * height > MAX_EDIT_PIXELS:
        raise ValueError("image is too large for masked editing")
    if bit_depth != 8:
        raise ValueError("mask PNG must use 8-bit channels")
    if interlace != 0:
        raise ValueError("interlaced mask PNG is not supported")
    if color_type == 6:
        bytes_per_pixel = 4
        alpha_index = 3
    elif color_type == 4:
        bytes_per_pixel = 2
        alpha_index = 1
    else:
        raise ValueError("mask PNG must be RGBA or grayscale+alpha")

    stride = width * bytes_per_pixel
    expected = height * (stride + 1)
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(b"".join(idat), expected + 1)
    except zlib.error as exc:
        raise ValueError("mask PNG has invalid compressed data") from exc
    if len(raw) > expected:
        raise ValueError("mask PNG decompresses to an unexpected size")
    raw += decompressor.flush()
    if len(raw) != expected:
        raise ValueError("mask PNG has an unexpected data length")

    alpha = bytearray(width * height)
    previous = bytearray(stride)
    offset = 0
    for y in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset:offset + stride])
        offset += stride
        if filter_type == 1:
            for index in range(bytes_per_pixel, stride):
                row[index] = (row[index] + row[index - bytes_per_pixel]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                row[index] = (row[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                up = previous[index]
                upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                estimate = left + up - upper_left
                distance_left = abs(estimate - left)
                distance_up = abs(estimate - up)
                distance_upper_left = abs(estimate - upper_left)
                if distance_left <= distance_up and distance_left <= distance_upper_left:
                    predictor = left
                elif distance_up <= distance_upper_left:
                    predictor = up
                else:
                    predictor = upper_left
                row[index] = (row[index] + predictor) & 0xFF
        elif filter_type != 0:
            raise ValueError("mask PNG uses an invalid row filter")

        base = y * width
        alpha[base:base + width] = row[alpha_index::bytes_per_pixel]
        previous = row

    return width, height, bytes(alpha)


def mask_geometry(mask_bytes: bytes) -> MaskGeometry:
    """Return the fully transparent bounding box of a PNG edit mask."""
    width, height, alpha = _png_alpha(mask_bytes)
    transparent_pixels = alpha.count(0)
    if transparent_pixels == 0:
        raise ValueError("mask has no fully transparent editable region")

    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    for y in range(height):
        row = alpha[y * width:(y + 1) * width]
        first = row.find(0)
        if first < 0:
            continue
        last = row.rfind(0)
        if first < min_x:
            min_x = first
        if last > max_x:
            max_x = last
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y

    if max_x < 0:
        raise ValueError("mask has no fully transparent editable region")
    return MaskGeometry(
        width=width,
        height=height,
        bounds=(min_x, min_y, max_x + 1, max_y + 1),
        transparent_pixels=transparent_pixels,
        total_pixels=width * height,
    )


def choose_crop_box(bounds: tuple, image_width: int, image_height: int) -> tuple:
    """Expand mask bounds to the closest supported aspect ratio.

    The returned box always contains the mask bounds and stays inside the image.
    A large expansion is avoided for very thin selections; in that case the
    original bounds are used and the edited crop is center-cropped on return.
    """
    x0, y0, x1, y1 = bounds
    box_width = x1 - x0
    box_height = y1 - y0
    if box_width < 1 or box_height < 1:
        raise ValueError("mask has an empty editable region")
    if x0 < 0 or y0 < 0 or x1 > image_width or y1 > image_height:
        raise ValueError("mask bounds exceed the source image")

    aspect = box_width / box_height
    target = min(
        _SUPPORTED_ASPECTS,
        key=lambda ratio: abs(math.log(aspect / (ratio[0] / ratio[1]))),
    )
    target_aspect = target[0] / target[1]
    if aspect >= target_aspect:
        crop_width = box_width
        crop_height = max(1, int(math.ceil(crop_width / target_aspect)))
    else:
        crop_height = box_height
        crop_width = max(1, int(math.ceil(crop_height * target_aspect)))

    # Do not turn a thin selection into a mostly-opaque crop. The composite
    # still enforces the mask; the crop is only a placement aid.
    if crop_width * crop_height > box_width * box_height * 4:
        crop_width = box_width
        crop_height = box_height

    crop_width = min(crop_width, image_width)
    crop_height = min(crop_height, image_height)
    crop_width = max(crop_width, box_width)
    crop_height = max(crop_height, box_height)

    center_x = (x0 + x1) // 2
    center_y = (y0 + y1) // 2
    crop_x0 = center_x - crop_width // 2
    crop_y0 = center_y - crop_height // 2
    crop_x0 = max(0, min(crop_x0, image_width - crop_width))
    crop_y0 = max(0, min(crop_y0, image_height - crop_height))
    if crop_x0 > x0:
        crop_x0 = x0
    if crop_y0 > y0:
        crop_y0 = y0
    if crop_x0 + crop_width < x1:
        crop_x0 = x1 - crop_width
    if crop_y0 + crop_height < y1:
        crop_y0 = y1 - crop_height
    return (crop_x0, crop_y0, crop_x0 + crop_width, crop_y0 + crop_height)


def _magick_command() -> str:
    for candidate in ("magick", "convert"):
        path = shutil.which(candidate)
        if path:
            return path
    raise ImageOpsUnavailable("ImageMagick is required for crop-and-composite edits")


def _run_magick(arguments, timeout=_MAGICK_TIMEOUT_SEC):
    command = [_magick_command()] + [str(value) for value in arguments]
    try:
        completed = subprocess.run(
            command,
            check=True,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ImageOpsUnavailable("ImageMagick is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ImageMagick timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            "ImageMagick failed" + (": " + detail[:500] if detail else "")
        ) from exc
    return completed.stdout


def _mime_suffix(mime_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }.get(mime_type, ".img")


def _crop_to_png(source_bytes: bytes, suffix: str, crop_box: tuple, temp_dir: Path) -> bytes:
    source_path = temp_dir / ("source" + suffix)
    output_path = temp_dir / "crop.png"
    source_path.write_bytes(source_bytes)
    x0, y0, x1, y1 = crop_box
    _run_magick([
        source_path,
        "-crop", "%dx%d+%d+%d" % (x1 - x0, y1 - y0, x0, y0),
        "+repage",
        output_path,
    ])
    if not output_path.is_file():
        raise RuntimeError("ImageMagick did not create the edit crop")
    return output_path.read_bytes()


def prepare_crop_edit(image_bytes: bytes, mask_bytes: bytes, image_mime: str) -> CropEditPlan:
    """Build the crop and cropped mask used for a local masked edit.

    Raises ImageOpsUnavailable when ImageMagick is missing, allowing the caller
    to fall back to the legacy full-image soft-mask behavior.
    """
    geometry = mask_geometry(mask_bytes)
    # The geometry check is deliberately done first: a mask with no editable
    # pixels is invalid regardless of whether ImageMagick is installed.
    _magick_command()
    crop_box = choose_crop_box(geometry.bounds, geometry.width, geometry.height)
    crop_x0, crop_y0, crop_x1, crop_y1 = crop_box
    crop_size = (crop_x1 - crop_x0, crop_y1 - crop_y0)

    with tempfile.TemporaryDirectory(prefix="gemini-crop-") as temp_name:
        temp_dir = Path(temp_name)
        crop_image = _crop_to_png(
            image_bytes, _mime_suffix(image_mime), crop_box, temp_dir
        )
        mask_source = temp_dir / "mask.png"
        mask_source.write_bytes(mask_bytes)
        mask_output = temp_dir / "crop-mask.png"
        _run_magick([
            mask_source,
            "-crop", "%dx%d+%d+%d" % (crop_size[0], crop_size[1], crop_x0, crop_y0),
            "+repage",
            mask_output,
        ])
        if not mask_output.is_file():
            raise RuntimeError("ImageMagick did not create the edit mask crop")
        crop_mask = mask_output.read_bytes()

    return CropEditPlan(
        crop_box=crop_box,
        crop_size=crop_size,
        crop_image=crop_image,
        crop_mask=crop_mask,
        geometry=geometry,
    )


def composite_crop_edit(
    original_bytes: bytes,
    original_mime: str,
    edited_bytes: bytes,
    edited_mime: str,
    crop_mask_bytes: bytes,
    crop_box: tuple,
    crop_size: tuple,
) -> tuple:
    """Paste the edited crop back through the mask alpha.

    Pixels where the mask is transparent are replaced by the edited crop.
    Opaque mask pixels keep the original image. Anti-aliased mask edges blend
    naturally because the mask alpha is used as the composite alpha.
    """
    output_mime = "image/jpeg" if original_mime == "image/jpeg" else "image/png"
    crop_width, crop_height = crop_size
    x0, y0, _, _ = crop_box

    with tempfile.TemporaryDirectory(prefix="gemini-composite-") as temp_name:
        temp_dir = Path(temp_name)
        original_path = temp_dir / ("original" + _mime_suffix(original_mime))
        edited_path = temp_dir / ("edited" + _mime_suffix(edited_mime))
        mask_path = temp_dir / "crop-mask.png"
        fit_path = temp_dir / "edited-fit.png"
        alpha_path = temp_dir / "edit-alpha.png"
        edited_alpha_path = temp_dir / "edited-alpha.png"
        original_path.write_bytes(original_bytes)
        edited_path.write_bytes(edited_bytes)
        mask_path.write_bytes(crop_mask_bytes)

        # Resize to fill the crop without distorting it, then center-crop any
        # excess when Gemini returned a different aspect ratio.
        _run_magick([
            edited_path,
            "-resize", "%dx%d^" % (crop_width, crop_height),
            "-gravity", "center",
            "-extent", "%dx%d" % (crop_width, crop_height),
            fit_path,
        ])
        # The mask uses alpha=0 for editable and alpha=255 for preserved. The
        # edited crop needs the inverse: opaque where it may replace the source.
        _run_magick([
            mask_path,
            "-alpha", "extract",
            "-negate",
            alpha_path,
        ])
        _run_magick([
            fit_path,
            alpha_path,
            "-alpha", "off",
            "-compose", "CopyOpacity",
            "-composite",
            edited_alpha_path,
        ])

        if output_mime == "image/jpeg":
            output_path = temp_dir / "output.jpg"
            output_arguments = [original_path, edited_alpha_path,
                                "-geometry", "+%d+%d" % (x0, y0),
                                "-composite", "-quality", "95", output_path]
        else:
            output_path = temp_dir / "output.png"
            output_arguments = [original_path, edited_alpha_path,
                                "-geometry", "+%d+%d" % (x0, y0),
                                "-composite", output_path]
        _run_magick(output_arguments)
        if not output_path.is_file():
            raise RuntimeError("ImageMagick did not create the composited image")
        return output_path.read_bytes(), output_mime


# Gemini often regenerates or restyles the whole frame instead of editing the
# requested area. That is invisible after the alpha composite, but the masked
# region still carries the regenerated content, which is what causes the
# "different background inside the patch" look. Comparing a small thumbnail of
# the raw output against the source outside the mask gives a cheap drift score.
_DRIFT_GRID = 128
_DRIFT_TIMEOUT_SEC = 60


def thumbnail_drift(source_bytes, source_mime, edited_bytes, edited_mime, mask_bytes,
                    grid=_DRIFT_GRID):
    """Mean per-channel difference outside the mask on a downscaled thumbnail.

    Returns a 0-255 score. Values near 0 mean the model returned the original
    pixels; large values mean it repainted the whole frame. Raises
    ImageOpsUnavailable when ImageMagick is missing.
    """
    with tempfile.TemporaryDirectory(prefix="gemini-drift-") as temp_name:
        temp_dir = Path(temp_name)
        source_path = temp_dir / ("source" + _mime_suffix(source_mime))
        edited_path = temp_dir / ("edited" + _mime_suffix(edited_mime))
        mask_path = temp_dir / "mask.png"
        source_path.write_bytes(source_bytes)
        edited_path.write_bytes(edited_bytes)
        mask_path.write_bytes(mask_bytes)

        size = "%dx%d!" % (grid, grid)
        source_rgb = _run_magick(
            [source_path, "-resize", size, "-depth", "8", "rgb:-"], timeout=_DRIFT_TIMEOUT_SEC)
        edited_rgb = _run_magick(
            [edited_path, "-resize", size, "-depth", "8", "rgb:-"], timeout=_DRIFT_TIMEOUT_SEC)
        mask_alpha = _run_magick(
            [mask_path, "-alpha", "extract", "-filter", "point",
             "-resize", size, "-depth", "8", "gray:-"], timeout=_DRIFT_TIMEOUT_SEC)

    expected = grid * grid
    if len(source_rgb) < expected * 3 or len(edited_rgb) < expected * 3:
        raise RuntimeError("ImageMagick returned incomplete drift thumbnails")
    if len(mask_alpha) < expected:
        raise RuntimeError("ImageMagick returned an incomplete mask thumbnail")

    total = 0
    samples = 0
    for index in range(expected):
        # Only compare pixels the mask says must stay untouched.
        if mask_alpha[index] < 250:
            continue
        offset = index * 3
        total += (abs(source_rgb[offset] - edited_rgb[offset])
                  + abs(source_rgb[offset + 1] - edited_rgb[offset + 1])
                  + abs(source_rgb[offset + 2] - edited_rgb[offset + 2]))
        samples += 1
    if samples < 64:
        return 0.0
    return total / (samples * 3)
