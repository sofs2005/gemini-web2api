"""HTTP server: OpenAI-compatible API endpoints."""
import base64
import ipaddress
import json
import math
import os
import time
import uuid
import re
import select
import socket
import threading
import queue
from urllib.parse import urlsplit
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from .config import CONFIG
from .models import get_models, resolve_model
from .gemini import (GeminiUpstreamError, generate, generate_stream, generate_image_structured,
                     get_full_size_image, log, _session_hash, safe_error_message)
from .generated_image import download_generated_image
from .tools import has_tool_call_marker, valid_tool_call_candidate, validate_tool_calls
from .tools import messages_to_prompt, parse_tool_calls, google_contents_to_prompt, parse_google_function_calls
from .multimodal import (detect_image_mime, fetch_image_bytes, image_dimensions as native_image_dimensions,
                         upload_image, validate_edit_mask)
from .image_ops import (
    ImageOpsUnavailable, build_marker_locator, composite_crop_edit, fit_image_size,
    image_dimensions, mask_geometry, match_crop_tone, prepare_context_crop,
    prepare_crop_edit, strip_edit_marker, thumbnail_mask_stats,
)
from .cache import CACHE
from .rotation import rotation_worker_alive
from . import __version__


_CHAT_IMAGE_REQUEST = re.compile(
    r"\b(?:generate|create|make|draw|render|paint)\s+"
    r"(?:(?:me|us)\s+)?(?:(?:an?|the)\s+)?"
    r"(?:image|picture|photo|illustration|artwork|icon|logo|portrait)\b",
    re.IGNORECASE,
)


def _latest_user_text(messages) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part.get("text", "") for part in content
                if isinstance(part, dict)
                and part.get("type") in ("text", "input_text")
                and isinstance(part.get("text"), str)
            )
        return ""
    return ""


def _chat_image_prompt(request: dict):
    """Return an explicit image-generation prompt from the latest user turn."""
    text = _latest_user_text(request.get("messages"))
    modalities = request.get("modalities")
    if (isinstance(modalities, list) and "image" in modalities) or _CHAT_IMAGE_REQUEST.search(text):
        return text.strip() or None
    return None


def _image_request_deadline():
    """Return the aggregate deadline for one Images request, if configured."""
    budget = CONFIG.get("image_request_budget_sec", 900)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        return None
    if budget <= 0:
        return None
    return time.monotonic() + float(budget)


def _image_budget_remaining(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("image request budget exhausted")
    return remaining


def _common_actual_size(sizes):
    """Return a response-level size only when every output agrees on it."""
    values = list(sizes)
    if not values or any(size is None for size in values):
        return None
    first = values[0]
    return first if all(size == first for size in values) else None


def _download_generated_image_bytes(image, *, deadline=None):
    """Download a generated image, falling back to the preview URL.

    Returns ``(image_bytes, mime, source_kind, source_url)``.
    """
    full_size_url = get_full_size_image(image)
    candidates = []
    if full_size_url:
        candidates.append(("full_size", full_size_url))
    if isinstance(image.url, str) and image.url and image.url != full_size_url:
        candidates.append(("preview", image.url))
    if not candidates:
        raise RuntimeError("Gemini returned no usable generated image URL")

    last_error = None
    for index, (source_kind, source_url) in enumerate(candidates):
        try:
            image_bytes, mime = download_generated_image(
                source_url, deadline=deadline)
            return image_bytes, mime, source_kind, source_url
        except Exception as exc:
            last_error = exc
            if index + 1 < len(candidates):
                log("image_generation_fallback from=%s to=%s error_type=%s" % (
                    source_kind, candidates[index + 1][0], type(exc).__name__))
    raise last_error


def _edit_attempt_rank(drift: float, change: float, drift_limit: float, min_change: float):
    """Rank a raw edit attempt; lower is better.

    An attempt that both keeps the frame anchored and changes the selection
    wins; its drift decides the order. When no attempt satisfies both, the
    attempt that at least performed the edit wins.
    """
    if drift <= drift_limit and change >= min_change:
        return (0, drift)
    return (1, -change)


def _frame_matches(size: tuple, expected: tuple, tolerance: float) -> bool:
    """True when a returned image keeps the source aspect ratio.

    A reframed or square generation cannot be composited back through the mask
    without cropping real content, so those attempts are discarded instead of
    being resized to fit.
    """
    width, height = size
    expected_width, expected_height = expected
    if min(width, height, expected_width, expected_height) <= 0:
        return False
    ratio = (width / height) / (expected_width / expected_height)
    return abs(ratio - 1.0) <= tolerance


def _generated_image_payload(image, response_format: str, transform=None):
    """Resolve an image, falling back to the preview URL if full-size delivery fails."""
    image_bytes, mime, source_kind, source_url = _download_generated_image_bytes(image)
    if transform is not None:
        image_bytes, mime = transform(image_bytes, mime)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    if response_format == "url":
        data = {"url": "data:%s;base64,%s" % (mime, encoded)}
    else:
        data = {"b64_json": encoded}
    return data, source_kind, source_url


def _generated_image_output(prompt: str, response_format: str):
    result = generate_image_structured(prompt)
    if not result.images:
        raise RuntimeError("Gemini returned no generated image metadata")
    data, source_kind, source_url = _generated_image_payload(result.images[0], response_format)
    log("image_generation_result images=%s selected_full_size=%s response_format=%s source_host=%s source_path=%s" % (
        len(result.images), source_kind == "full_size", response_format,
        (urlsplit(source_url).hostname or "-"), (urlsplit(source_url).path or "/")))
    return result.text, data


class _ImageRequestError(ValueError):
    """A request validation error carrying OpenAI error metadata."""

    def __init__(self, message, param=None, code="invalid_value"):
        super().__init__(message)
        self.param = param
        self.code = code


_MAX_IMAGE_PROMPT_CHARS = 32000
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_MASK_BYTES = 4 * 1024 * 1024
_IMAGE_INPUT_MIMES = {"image/png", "image/jpeg", "image/webp"}
_IMAGE_QUALITIES = {"low", "medium", "high", "xhigh", "max", "auto", "standard", "hd"}
_IMAGE_BACKGROUNDS = {"transparent", "opaque", "auto"}
_IMAGE_MODERATIONS = {"low", "auto"}
_IMAGE_OUTPUT_FORMATS = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
_IMAGE_INPUT_FIDELITY = {"high", "low"}
_IMAGE_STYLES = {"vivid", "natural"}
_IMAGE_MODEL_ALIASES = {
    "gpt-image-1.5",
    "gpt-image-2",
    "gpt-image-2-2026-04-21",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-sunburst-2026-09-08",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-flare-2026-09-08",
    "gpt-image-1",
    "gpt-image-1-mini",
    "chatgpt-image-latest",
    "dall-e-2",
    "dall-e-3",
}


def _image_model_error(model_name):
    """Return an error for names outside the supported image namespace."""
    if not isinstance(model_name, str) or not model_name.strip():
        return "model must be a non-empty string"
    name = model_name.strip()
    base = name.split("@think=", 1)[0]
    if (base in _IMAGE_MODEL_ALIASES or base.startswith("gemini")
            or re.fullmatch(r"[a-f0-9]{16}", base)):
        return None
    return "model is not supported for image generation: " + name


def _parse_n(value, default=1):
    if value is None:
        return default
    if isinstance(value, bool):
        raise _ImageRequestError("n must be an integer between 1 and 10", "n")
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip().lower()
        if text in ("", "null"):
            return default
        try:
            number = int(text)
        except ValueError as exc:
            raise _ImageRequestError(
                "n must be an integer between 1 and 10", "n") from exc
    if not 1 <= number <= 10:
        raise _ImageRequestError(
            "n must be an integer between 1 and 10", "n")
    return number


def _parse_size(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "null", "auto"):
        return None
    match = re.fullmatch(r"(\d{1,5})x(\d{1,5})", text)
    if not match:
        raise _ImageRequestError(
            "size must be auto or WIDTHxHEIGHT", "size")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 1 or height < 1:
        raise _ImageRequestError(
            "size must be auto or WIDTHxHEIGHT", "size")
    if width % 16 or height % 16:
        raise _ImageRequestError(
            "size width and height must be divisible by 16", "size")
    if not 1 / 3 <= width / height <= 3:
        raise _ImageRequestError(
            "size aspect ratio must be between 1:3 and 3:1", "size")
    if max(width, height) > 3840 or min(width, height) > 2160:
        raise _ImageRequestError(
            "size exceeds the maximum supported resolution", "size")
    return width, height


def _parse_enum(value, allowed, param, default=None):
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("", "null"):
        return default
    if text not in allowed:
        raise _ImageRequestError(
            "%s must be one of: %s" % (param, ", ".join(sorted(allowed))), param)
    return text


def _parse_response_format(value):
    response_format = _parse_enum(
        value, {"b64_json", "url"}, "response_format", default="b64_json")
    if response_format == "url":
        raise _ImageRequestError(
            "response_format 'url' is not supported; use 'b64_json'",
            "response_format", "unsupported_value")
    return response_format


def _parse_bool(value, param, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("", "null"):
        return default
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise _ImageRequestError("%s must be a boolean" % param, param)


def _parse_output_compression(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise _ImageRequestError(
            "output_compression must be an integer between 0 and 100",
            "output_compression")
    text = str(value).strip().lower()
    if text in ("", "null"):
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise _ImageRequestError(
            "output_compression must be an integer between 0 and 100",
            "output_compression") from exc
    if not 0 <= number <= 100:
        raise _ImageRequestError(
            "output_compression must be an integer between 0 and 100",
            "output_compression")
    return number


def _size_instruction(size):
    if size is None:
        return ""
    width, height = size
    divisor = math.gcd(width, height)
    return (
        "\n\nRender the final image at %dx%d pixels with a %d:%d aspect ratio."
        % (width, height, width // divisor, height // divisor)
    )


def _mime_output_format(mime):
    return {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/webp": "webp",
    }.get(mime)


def _apply_output_size(image_bytes, mime, size=None, output_format=None,
                       output_compression=None):
    """Fit an image to the requested dimensions/format when ImageMagick is available."""
    if size is None and output_format is None:
        if output_compression is None or mime not in ("image/jpeg", "image/webp"):
            return image_bytes, mime
    try:
        width, height = size if size is not None else image_dimensions(image_bytes, mime)
        return fit_image_size(image_bytes, mime, width, height, output_format,
                              output_compression)
    except ImageOpsUnavailable:
        log("image_output_size_fallback reason=imagemagick_unavailable")
    except (RuntimeError, ValueError) as exc:
        log("image_output_size_fallback reason=%s" % type(exc).__name__)
    return image_bytes, mime


def _image_data(image_bytes, response_format=None):
    if response_format not in (None, "b64_json"):
        raise RuntimeError("only b64_json is supported")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {"b64_json": encoded}


_agent_slot = threading.BoundedSemaphore(1)


_allowlist_cache = {"path": None, "mtime": 0, "ips": frozenset()}


def _client_allowed(handler) -> bool:
    path = CONFIG.get("client_allowlist_file")
    if not path:
        return True
    client_ip = handler.client_address[0]
    if client_ip in ("127.0.0.1", "::1"):
        forwarded_ip = handler.headers.get("CF-Connecting-IP", "").strip()
        if forwarded_ip:
            try:
                client_ip = str(ipaddress.ip_address(forwarded_ip))
            except ValueError:
                return False
        else:
            return True
    try:
        mtime = os.path.getmtime(path)
        if path != _allowlist_cache["path"] or mtime != _allowlist_cache["mtime"]:
            with open(path, encoding="utf-8") as source:
                data = json.load(source)
            ips = frozenset(str(ipaddress.ip_address(value)) for value in data.get("ips", []))
            _allowlist_cache.update(path=path, mtime=mtime, ips=ips)
        return client_ip in _allowlist_cache["ips"]
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _is_upstream_placeholder_refusal(text: str) -> bool:
    """Recognize Gemini overload/fallback text before it contaminates history."""
    normalized = " ".join((text or "").lower().split())
    if not normalized or len(normalized) > 240:
        return False
    markers = (
        "i couldn't do that because i'm getting a lot of requests right now. please try again later.",
        "i'm having a hard time fulfilling your request. can i help you with something else instead?",
        "sorry, something went wrong. please try your request again.",
        "i seem to be encountering an error. can i try something else for you?",
        "i encountered an error doing what you asked. please try again.",
    )
    return normalized in markers


def _has_complete_tool_call(text: str) -> bool:
    return valid_tool_call_candidate(text)


def _classify_response_quality(prompt: str, text: str, tools: bool) -> str:
    """Classify common upstream failures without another model request."""
    context_class = "oversized_context" if len(prompt or "") > 60000 else ""
    if tools and _is_upstream_placeholder_refusal(text):
        return "temporary_refusal"
    if tools and has_tool_call_marker(text):
        return "normal" if _has_complete_tool_call(text) else "malformed_or_unparsed_tool_call"
    if not (text or "").strip():
        return "empty_response"
    return context_class or "normal"


def _usage(prompt: str, text: str) -> dict:
    p = len(prompt) // 4
    c = len(text or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


_IMAGE_TOKEN_PATCH = 32


def _estimate_text_tokens(text: str) -> int:
    """Deterministic proxy-side estimate: one token per 4 UTF-8 bytes."""
    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _estimate_image_tokens(size) -> int:
    """Estimate image tokens as one token per 32x32 pixel patch."""
    if not size:
        return 0
    width, height = size
    if width <= 0 or height <= 0:
        return 0
    return math.ceil(width / _IMAGE_TOKEN_PATCH) * math.ceil(height / _IMAGE_TOKEN_PATCH)


def _image_usage(prompt: str, input_sizes=(), output_sizes=()) -> dict:
    """Build an ImagesUsage object from proxy-side deterministic estimates."""
    text_tokens = _estimate_text_tokens(prompt)
    input_image_tokens = sum(_estimate_image_tokens(size) for size in input_sizes)
    output_image_tokens = sum(_estimate_image_tokens(size) for size in output_sizes)
    input_tokens = text_tokens + input_image_tokens
    return {
        "total_tokens": input_tokens + output_image_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_image_tokens,
        "input_tokens_details": {
            "text_tokens": text_tokens,
            "image_tokens": input_image_tokens,
        },
        "output_tokens_details": {
            "text_tokens": 0,
            "image_tokens": output_image_tokens,
        },
    }


def _native_image_size(image_bytes):
    try:
        return native_image_dimensions(image_bytes)
    except (TypeError, ValueError):
        return None


def _actual_image_size(image_bytes, mime):
    """Read the final image size without requiring ImageMagick."""
    size = _native_image_size(image_bytes)
    if size is not None:
        return size
    try:
        return image_dimensions(image_bytes, mime)
    except (ImageOpsUnavailable, RuntimeError, ValueError):
        return None


def _attachment_sizes(attachments) -> list:
    sizes = []
    for item in attachments or []:
        if not isinstance(item, (tuple, list)) or not item:
            continue
        data = item[0]
        if isinstance(data, str):
            try:
                data = fetch_image_bytes(data)
            except Exception:
                continue
        size = _native_image_size(data)
        if size is not None:
            sizes.append(size)
    return sizes


def _cached_tokens(messages: list, tools=None, tool_choice=None) -> int:
    """Estimate tokens contributed by a locally cached message prefix."""
    if not messages:
        return 0
    cached_prompt, _ = messages_to_prompt(messages, tools, tool_choice)
    return len(cached_prompt) // 4


def _auto_cache_prefix(messages: list) -> list:
    prefix = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        prefix.append(message)
    if prefix:
        return prefix
    # For multi-turn requests, preserve all prior turns and leave the current
    # user message uncached so each new question remains dynamic.
    return messages[:-1] if len(messages) > 1 else []


def _cache_scope(handler) -> str:
    return handler.headers.get("Authorization", "") or handler.client_address[0]


def _upload_images(images: list) -> list:
    """Upload images and return list of file references. Returns None if no images."""
    if not images:
        return None
    file_refs = []
    for item in images:
        if not (isinstance(item, (tuple, list)) and len(item) in (2, 3)):
            continue
        data, mime = item[:2]
        filename = item[2] if len(item) == 3 else "image.png"
        if not isinstance(filename, str) or not filename:
            filename = "image.png"
        try:
            if isinstance(data, str):
                data = fetch_image_bytes(data)
            if not data:
                raise RuntimeError("image data is empty")
            if len(data) > _MAX_IMAGE_BYTES:
                raise RuntimeError("image exceeds 50 MiB size limit")
            mime = detect_image_mime(data, mime or "image/png")
            ref = upload_image(data, filename, mime)
            file_refs.append((ref, filename))
        except Exception as e:
            raise RuntimeError(f"image upload failed: {safe_error_message(e)}") from e
    return file_refs if file_refs else None


def _parse_multipart_form(body: bytes, content_type: str) -> dict:
    """Parse a bounded multipart/form-data body without losing binary file bytes."""
    if not isinstance(content_type, str) or not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("Content-Type must be multipart/form-data")
    try:
        header = content_type.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid multipart Content-Type") from exc
    message = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + header + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise ValueError("invalid multipart body")

    fields = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            raise ValueError("multipart parts must use form-data disposition")
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name:
            raise ValueError("multipart part is missing a field name")
        value = part.get_payload(decode=True)
        if value is None:
            value = b""
        fields.setdefault(name, []).append({
            "data": value,
            "filename": part.get_filename(),
            "content_type": part.get_content_type(),
        })
    return fields


def _multipart_text(fields: dict, name: str, required: bool = False):
    parts = fields.get(name, [])
    if len(parts) != 1:
        if required:
            raise _ImageRequestError(
                f"{name} is required", name, "missing_required_parameter")
        raise _ImageRequestError(
            f"{name} must be provided at most once", name)
    part = parts[0]
    if part.get("filename") is not None:
        raise _ImageRequestError(f"{name} must be a text field", name)
    try:
        value = part["data"].decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise _ImageRequestError(f"{name} must be valid UTF-8", name) from exc
    if required and not value.strip():
        raise _ImageRequestError(f"{name} must not be empty", name)
    return value


def _optional_text(fields: dict, name: str):
    if name not in fields:
        return None
    return _multipart_text(fields, name)


def _file_part(part, name: str, max_bytes: int = _MAX_IMAGE_BYTES):
    data = part.get("data")
    if not isinstance(data, bytes) or not data:
        raise _ImageRequestError(
            f"{name} must be a non-empty image file", name)
    if len(data) > max_bytes:
        raise _ImageRequestError(
            f"{name} exceeds %d MiB size limit" % (max_bytes // (1024 * 1024)), name)
    filename = part.get("filename")
    if not isinstance(filename, str) or not filename:
        filename = "image.png" if name == "image" else "mask.png"
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1] or (
        "image.png" if name == "image" else "mask.png"
    )
    mime = detect_image_mime(data, "")
    if mime not in _IMAGE_INPUT_MIMES:
        raise _ImageRequestError(
            f"{name} must be a PNG, JPEG, or WebP image", name)
    return data, mime, filename


def _multipart_file(fields: dict, name: str, required: bool = False,
                    max_bytes: int = _MAX_IMAGE_BYTES):
    parts = fields.get(name, [])
    if not parts:
        if required:
            raise _ImageRequestError(
                f"{name} is required", name, "missing_required_parameter")
        return None
    if len(parts) != 1:
        raise _ImageRequestError(
            f"{name} must be provided at most once", name)
    return _file_part(parts[0], name, max_bytes)


def _selection_description(geometry) -> str:
    """Describe the mask region in words the image model can act on."""
    width, height = geometry.width, geometry.height
    x0, y0, x1, y1 = geometry.bounds
    percent_width = round(100 * (x1 - x0) / width)
    percent_height = round(100 * (y1 - y0) / height)
    center_x = (x0 + x1) / 2 / width
    center_y = (y0 + y1) / 2 / height
    horizontal = "left" if center_x < 0.34 else ("right" if center_x > 0.66 else "center")
    vertical = "upper" if center_y < 0.34 else ("lower" if center_y > 0.66 else "middle")
    return (
        f"- It covers about {percent_width}% of the frame width and "
        f"{percent_height}% of the frame height.\n"
        f"- It sits in the {vertical} {horizontal} of the frame.\n"
        "- It is a small local area inside a much larger scene."
    )


def _edit_prompt(prompt: str, geometry=None, locator: bool = False) -> str:
    if geometry is None:
        return prompt
    if locator:
        images = (
            "Image 1 is the source image.\n"
            "Image 2 is the same photo with a thin red outline around the editable "
            "area. The outline is a locator, not part of the scene: never copy its "
            "red pixels and never paint a shape that follows it.\n"
            "Image 3 is the same photo again without any marker, for reference.\n\n"
        )
        locator_rule = (
            "- Do not add anything outside the red outline, even if it looks more "
            "natural there.\n"
        )
    else:
        images = (
            "Image 1 is the source image. Image 2 is a selection mask: the transparent "
            "area marks the only pixels you may change. The mask is a location "
            "reference only.\n\n"
        )
        locator_rule = ""
    return (
        "You are editing one small selected area of Image 1.\n\n"
        + images
        + "Where the selection is:\n"
        + _selection_description(geometry)
        + "\n\n"
        "How to edit:\n"
        "- Make the requested change inside the selected area only.\n"
        + locator_rule
        + "- Change only the pixels needed for that content. Leave the rest of the "
        "selected area's original texture intact: existing grain, streaks, fog, spray, "
        "color and lighting must continue unchanged inside the selection.\n"
        "- The change must look like a local retouch of Image 1, not like a patch, "
        "cut-out, sticker, or collage.\n"
        "- The result must be seamless: at the selection boundary there must be no "
        "visible edge, outline, halo, color step, brightness step, blur, or "
        "lasso-shaped border.\n"
        "- Do not invent an object whose outline follows the selection shape. Do not "
        "fill the selection with a flat color, gradient, cloud blob, sky, fog, or "
        "blurred texture.\n"
        "- Never reproduce the mask image in the output. Its black or transparent "
        "pixels must not appear as black, white, a hole, or a silhouette in your "
        "result.\n"
        "- Never treat Image 2 as a shape to draw or a layer to paste. It only tells "
        "you where the edit may happen.\n\n"
        "Hard constraints:\n"
        "- Keep the exact framing, resolution, perspective, camera angle and crop of "
        "Image 1.\n"
        "- Every pixel outside the selected area must stay exactly as in Image 1: no "
        "relighting, recoloring, regrading, denoising, sharpening, blurring, or "
        "restyling.\n"
        "- Do not add text, watermarks, logos, borders, frames, vignettes, or extra "
        "objects.\n"
        "- The requested change is required: an unchanged selected area is a failed "
        "edit. If the requested content cannot fit at the size you want, make it "
        "smaller so that it fits.\n\n"
        "Before answering, verify:\n"
        "- Did the selected area change as requested?\n"
        "- Is the boundary invisible, with no visible lasso edge?\n"
        "- Is the original texture inside the selection still recognizable?\n"
        "- Is everything outside the selection identical to Image 1?\n"
        "Revise until all answers are yes, then return the edited image.\n\n"
        "User instruction:\n"
        + prompt
    )


def _context_edit_prompt(prompt: str, geometry, crop_box: tuple, crop_size: tuple) -> str:
    """Prompt for a context crop that is sent without any mask attachment."""
    crop_width, crop_height = crop_size
    x0, y0, _, _ = crop_box
    mask_x0, mask_y0, mask_x1, mask_y1 = geometry.bounds
    center_x = (mask_x0 + mask_x1) / 2 - x0
    center_y = (mask_y0 + mask_y1) / 2 - y0
    horizontal = "left" if center_x < crop_width / 3 else (
        "right" if center_x > 2 * crop_width / 3 else "center")
    vertical = "upper" if center_y < crop_height / 3 else (
        "lower" if center_y > 2 * crop_height / 3 else "middle")
    percent_width = round(100 * (mask_x1 - mask_x0) / crop_width)
    percent_height = round(100 * (mask_y1 - mask_y0) / crop_height)
    return (
        "This is a photograph. Edit this photo directly.\n\n"
        "Where the change belongs:\n"
        f"- It sits in the {vertical} {horizontal} of the frame.\n"
        f"- It covers roughly {percent_width}% of the frame width and "
        f"{percent_height}% of the frame height.\n\n"
        "What to do:\n"
        f"- {prompt}\n"
        "- Change only that part of the photo and nothing else.\n"
        "- Keep the same framing, resolution, perspective, camera angle and crop.\n"
        "- Keep the surrounding photo's rain, grain, spray, fog, color and lighting "
        "continuous through the change.\n"
        "- Blend the new content into its surroundings: no visible edge, seam, "
        "patch, sticker, halo, brightness step or shape outline.\n"
        "- Do not fill the area with a flat color, gradient, blurred texture, or "
        "an object whose outline follows a selection.\n"
        "- The edit is required: an unchanged photo is a failed edit.\n"
    )


def _crop_edit_prompt(prompt: str, crop_size: tuple) -> str:
    width, height = crop_size
    return (
        "You are editing a cropped region of a larger image.\n\n"
        "Image 1 is the source crop. Image 2 is a pixel-aligned edit mask for this crop; "
        "it is not a style reference and only defines the editable region.\n\n"
        f"The output must represent exactly this {width}x{height} crop. Do not expand the "
        "canvas, add borders, or return the surrounding full image. Continue the existing "
        "scene naturally.\n\n"
        "Mask rules:\n"
        "- The transparent region of the mask (alpha = 0) is the ONLY editable area.\n"
        "- The opaque region of the mask (alpha = 255) must remain completely unchanged.\n"
        "- Treat partially transparent edge pixels as a natural blend between the edited "
        "and preserved areas.\n"
        "- Preserve the crop's framing, perspective, lighting, colors, and all opaque content.\n\n"
        "Placement rules:\n"
        "- Put every requested object or change entirely inside the transparent region.\n"
        "- Scale or adjust the requested subject as needed so it fits completely inside "
        "that region.\n"
        "- Do not place the requested subject outside the transparent region, even if it "
        "looks more natural there.\n\n"
        "User instruction:\n"
        + prompt
    )


_UPSTREAM_RESPONSE_STARTED = object()


def _stream_with_heartbeat(upstream):
    """Yield upstream deltas while keeping idle SSE connections active.

    An iterator factory may accept a callback to signal the first upstream data
    independently of model text. Events are consumed by the HTTP handler so only
    that thread writes to the client connection.

    Some Serv00/front-door proxies close an otherwise healthy stream after about
    30 seconds without bytes.  Run the blocking upstream iterator separately so
    the HTTP handler can emit SSE comments while Gemini is still preparing the
    first token.
    """
    events = queue.Queue(maxsize=16)
    stopped = threading.Event()

    def publish(event):
        while not stopped.is_set():
            try:
                events.put(event, timeout=0.5)
                return
            except queue.Full:
                continue

    def consume():
        iterator = None
        notified = False

        def notify_first_response():
            nonlocal notified
            if not notified:
                notified = True
                publish(("item", _UPSTREAM_RESPONSE_STARTED))

        try:
            iterator = upstream(notify_first_response) if callable(upstream) else upstream
            for item in iterator:
                if stopped.is_set():
                    break
                publish(("item", item))
        except Exception as error:
            publish(("error", error))
        finally:
            try:
                close = getattr(iterator, "close", None)
                if close:
                    close()
            except Exception as error:
                publish(("error", error))
            finally:
                publish(("done", None))

    threading.Thread(target=consume, daemon=True).start()
    interval = max(0.01, float(CONFIG.get("stream_heartbeat_sec", 10)))
    try:
        while True:
            try:
                kind, value = events.get(timeout=interval)
            except queue.Empty:
                yield None
                continue
            if kind == "item":
                yield value
            elif kind == "error":
                raise value
            else:
                return
    finally:
        stopped.set()


_IMAGE_GENERATION_FIELDS = {
    "prompt", "model", "n", "response_format", "size", "quality", "style",
    "background", "moderation", "output_format", "output_compression", "user",
    "safety_identifier", "stream", "partial_images",
}
_IMAGE_EDIT_MULTIPART_FIELDS = {
    "prompt", "image", "mask", "model", "n", "response_format", "stream",
    "size", "quality", "background", "moderation", "output_format",
    "output_compression", "input_fidelity", "user", "safety_identifier",
    "markup", "partial_images",
}
_IMAGE_EDIT_JSON_FIELDS = {
    "prompt", "images", "mask", "model", "n", "response_format", "stream",
    "size", "quality", "background", "moderation", "output_format",
    "output_compression", "input_fidelity", "user", "safety_identifier",
    "markup", "partial_images",
}


def _parse_optional_string(value, param, max_length=None):
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ImageRequestError("%s must be a string" % param, param)
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise _ImageRequestError(
            "%s must be at most %d characters" % (param, max_length), param)
    return value or None


def _parse_partial_images(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        raise _ImageRequestError(
            "partial_images must be an integer between 0 and 3", "partial_images")
    text = str(value).strip().lower()
    if text in ("", "null"):
        return 0
    try:
        number = int(text)
    except ValueError as exc:
        raise _ImageRequestError(
            "partial_images must be an integer between 0 and 3",
            "partial_images") from exc
    if not 0 <= number <= 3:
        raise _ImageRequestError(
            "partial_images must be an integer between 0 and 3", "partial_images")
    return number


def _parse_stream(value):
    if _parse_bool(value, "stream"):
        raise _ImageRequestError(
            "stream is not supported", "stream", "unsupported_value")
    return False


def _parse_prompt(value):
    if not isinstance(value, str) or not value.strip():
        raise _ImageRequestError(
            "prompt is required", "prompt", "missing_required_parameter")
    value = value.strip()
    if len(value) > _MAX_IMAGE_PROMPT_CHARS:
        raise _ImageRequestError(
            "prompt must be at most %d characters" % _MAX_IMAGE_PROMPT_CHARS,
            "prompt")
    return value


def _parse_common_image_options(get, *, allow_style):
    ignored = []
    stream = _parse_stream(get("stream"))
    partial_images = _parse_partial_images(get("partial_images"))
    size = _parse_size(get("size"))
    quality = _parse_enum(get("quality"), _IMAGE_QUALITIES, "quality")
    background = _parse_enum(get("background"), _IMAGE_BACKGROUNDS, "background")
    if background == "transparent":
        raise _ImageRequestError(
            "background 'transparent' is not supported; use 'opaque' or 'auto'",
            "background", "unsupported_value")
    moderation = _parse_enum(get("moderation"), _IMAGE_MODERATIONS, "moderation")
    output_format = _parse_enum(
        get("output_format"), set(_IMAGE_OUTPUT_FORMATS), "output_format")
    output_compression = _parse_output_compression(get("output_compression"))
    input_fidelity = _parse_enum(
        get("input_fidelity"), _IMAGE_INPUT_FIDELITY, "input_fidelity")
    style = _parse_enum(get("style"), _IMAGE_STYLES, "style") if allow_style else None
    user = _parse_optional_string(get("user"), "user")
    safety_identifier = _parse_optional_string(
        get("safety_identifier"), "safety_identifier", 128)
    markup = get("markup")
    if markup is not None and not (
            isinstance(markup, str) and markup.strip().lower() in ("", "null")):
        raise _ImageRequestError(
            "markup is not supported by the Gemini backend",
            "markup", "unsupported_value")
    for name, value in (
            ("quality", quality), ("background", background),
            ("moderation", moderation), ("input_fidelity", input_fidelity),
            ("style", style), ("output_compression", output_compression),
            ("user", user), ("safety_identifier", safety_identifier),
            ("partial_images", partial_images)):
        if value is not None and value is not False and value != 0:
            ignored.append(name)
    return {
        "stream": stream,
        "partial_images": partial_images,
        "size": size,
        "quality": quality,
        "background": background,
        "moderation": moderation,
        "output_format": output_format,
        "output_compression": output_compression,
        "input_fidelity": input_fidelity,
        "safety_identifier": safety_identifier,
        "ignored_fields": ignored,
    }


def _parse_image_generation_request(body: bytes):
    try:
        request = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _ImageRequestError("invalid JSON", code="invalid_json") from exc
    if not isinstance(request, dict):
        raise _ImageRequestError("invalid image generation request")
    unexpected = sorted(set(request) - _IMAGE_GENERATION_FIELDS)
    if unexpected:
        raise _ImageRequestError(
            "unexpected field: " + unexpected[0], unexpected[0], "unknown_parameter")
    prompt = _parse_prompt(request.get("prompt"))
    if "model" not in request:
        raise _ImageRequestError(
            "model is required", "model", "missing_required_parameter")
    model = request["model"]
    if not isinstance(model, str) or not model.strip():
        raise _ImageRequestError("model must be a string", "model")
    response_format = _parse_response_format(request.get("response_format"))
    options = _parse_common_image_options(request.get, allow_style=True)
    options.update({
        "prompt": prompt,
        "model": model.strip(),
        "n": _parse_n(request.get("n")),
        "response_format": response_format,
    })
    return options


def _parse_multipart_edit_request(body: bytes, content_type: str):
    try:
        fields = _parse_multipart_form(body, content_type)
    except ValueError as exc:
        raise _ImageRequestError(str(exc)) from exc
    unexpected = sorted(set(fields) - _IMAGE_EDIT_MULTIPART_FIELDS)
    if unexpected:
        raise _ImageRequestError(
            "unexpected field: " + unexpected[0], unexpected[0], "unknown_parameter")
    prompt = _parse_prompt(_multipart_text(fields, "prompt", required=True))
    if len(fields.get("image", [])) > 1:
        raise _ImageRequestError(
            "only one image is supported", "image", "unsupported_value")
    image = _multipart_file(fields, "image", required=True)
    images = [image]
    mask = _multipart_file(fields, "mask", max_bytes=_MAX_MASK_BYTES)
    if mask is not None:
        try:
            validate_edit_mask(images[0][0], mask[0])
        except ValueError as exc:
            raise _ImageRequestError(str(exc), "mask") from exc
    options = _parse_common_image_options(
        lambda name: _optional_text(fields, name), allow_style=False)
    model = _parse_optional_string(
        _multipart_text(fields, "model", required=True), "model")
    options.update({
        "prompt": prompt,
        "images": images,
        "mask": mask,
        "model": model,
        "n": _parse_n(_optional_text(fields, "n")),
        "response_format": _parse_response_format(
            _optional_text(fields, "response_format")),
    })
    return options


def _fetch_json_image_reference(reference, param):
    if isinstance(reference, str):
        url = reference
    elif isinstance(reference, dict):
        if "file_id" in reference:
            raise _ImageRequestError(
                "file_id image references are not supported; use image_url with "
                "an HTTPS URL or data URL",
                param, "unsupported_value")
        url = reference.get("image_url")
    else:
        raise _ImageRequestError("invalid image reference", param)
    if not isinstance(url, str) or not url:
        raise _ImageRequestError(
            "%s.image_url is required" % param, param,
            "missing_required_parameter")
    if url.startswith("data:"):
        header, separator, payload = url.partition(",")
        if not separator or ";base64" not in header.lower():
            raise _ImageRequestError("invalid image data URL", param)
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError) as exc:
            raise _ImageRequestError("invalid image data URL", param) from exc
        mime = detect_image_mime(data, "")
    else:
        try:
            data = fetch_image_bytes(url)
        except Exception as exc:
            raise _ImageRequestError(
                "could not fetch image: %s" % safe_error_message(exc),
                param, "invalid_value") from exc
        mime = detect_image_mime(data, "")
    if not data:
        raise _ImageRequestError("image is empty", param)
    if mime not in _IMAGE_INPUT_MIMES:
        raise _ImageRequestError(
            "image must be a PNG, JPEG, or WebP file", param)
    if len(data) > _MAX_IMAGE_BYTES:
        raise _ImageRequestError("image exceeds 50 MiB size limit", param)
    return data, mime


def _parse_json_edit_request(body: bytes):
    try:
        request = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _ImageRequestError("invalid JSON", code="invalid_json") from exc
    if not isinstance(request, dict):
        raise _ImageRequestError("invalid image edit request")
    unexpected = sorted(set(request) - _IMAGE_EDIT_JSON_FIELDS)
    if unexpected:
        raise _ImageRequestError(
            "unexpected field: " + unexpected[0], unexpected[0], "unknown_parameter")
    prompt = _parse_prompt(request.get("prompt"))
    raw_images = request.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise _ImageRequestError(
            "images is required", "images", "missing_required_parameter")
    if len(raw_images) != 1:
        raise _ImageRequestError(
            "only one image is supported", "images", "unsupported_value")
    images = []
    for index, reference in enumerate(raw_images):
        data, mime = _fetch_json_image_reference(
            reference, "images[%d]" % index)
        images.append((data, mime, "image-%d.png" % index))
    mask = None
    if request.get("mask") is not None:
        data, mime = _fetch_json_image_reference(request["mask"], "mask")
        mask = (data, mime, "mask.png")
        try:
            validate_edit_mask(images[0][0], mask[0])
        except ValueError as exc:
            raise _ImageRequestError(str(exc), "mask") from exc
    options = _parse_common_image_options(request.get, allow_style=False)
    model = (_parse_optional_string(request.get("model"), "model")
             or "gpt-image-2.5-sunburst")
    options.update({
        "prompt": prompt,
        "images": images,
        "mask": mask,
        "model": model,
        "n": _parse_n(request.get("n")),
        "response_format": _parse_response_format(
            request.get("response_format")),
    })
    return options


_OPENAI_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    409: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
}


def _openai_error_type(status):
    if status >= 500:
        return "server_error"
    return _OPENAI_ERROR_TYPES.get(status, "invalid_request_error")


_GOOGLE_ERROR_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}


def _google_error_payload(message, status=400):
    return {"error": {
        "code": status,
        "message": message,
        "status": _GOOGLE_ERROR_STATUS.get(
            status, "INTERNAL" if status >= 500 else "INVALID_ARGUMENT"),
    }}


def _openai_error_payload(message, status=400, *, param=None, code=None,
                          error_type=None, retryable=None):
    payload = {"error": {
        "type": error_type or _openai_error_type(status),
        "message": message,
        "param": param,
        "code": code,
    }}
    if retryable is not None:
        payload["error"]["retryable"] = bool(retryable)
    return payload


class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    def send_json(self, data, status=200):
        if getattr(self, "_sse_started", False):
            self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            if self.path == "/v1/chat/completions":
                self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("x-request-id", self._request_id_value())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _request_id_value(self):
        if not getattr(self, "_request_id", None):
            self._request_id = "req_" + uuid.uuid4().hex
        return self._request_id

    def send_openai_error(self, message, status=400, *, param=None, code=None,
                          error_type=None, retryable=None):
        """Send an OpenAI-compatible ErrorResponse."""
        self.send_json(_openai_error_payload(
            message, status, param=param, code=code,
            error_type=error_type, retryable=retryable), status)

    def _is_google_route(self):
        path = self.path.split("?", 1)[0]
        return path == "/v1beta" or path.startswith("/v1beta/")

    def send_google_error(self, message, status=400):
        """Send a Gemini/Google-style error for native /v1beta routes."""
        self.send_json(_google_error_payload(message, status), status)

    def send_route_error(self, message, status=400, *, param=None, code=None,
                         retryable=None):
        if self._is_google_route():
            self.send_google_error(message, status)
        else:
            self.send_openai_error(
                message, status, param=param, code=code, retryable=retryable)

    def _client_disconnected(self):
        readable, _, _ = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _generate_buffered(self, prompt, model_id, think_mode, file_refs,
                           extra_fields, model_name, request_id, tools_enabled,
                           on_first_response=None):
        kwargs = {"session_hash": _session_hash(model_name), "request_id": request_id}
        if on_first_response is not None:
            kwargs["on_first_response"] = on_first_response
        if not tools_enabled:
            return generate(prompt, model_id, think_mode, file_refs, extra_fields, **kwargs)
        if not _agent_slot.acquire(blocking=False):
            raise BlockingIOError("Another agent generation is still running; retry later")
        try:
            return generate(prompt, model_id, think_mode, file_refs, extra_fields,
                            **kwargs, budget_sec=max(1, CONFIG["agent_request_budget_sec"]),
                            max_attempts=1, cancelled=self._client_disconnected)
        finally:
            _agent_slot.release()

    def _start_sse(self):
        if getattr(self, "_sse_started", False):
            return
        self._sse_started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("x-request-id", self._request_id_value())
        self.end_headers()

    def _send_chat_stream_chunk(self, request_id, model_name, delta=None, finish_reason=None):
        """Send a valid OpenAI SSE chunk, including empty chunks used as transport keepalive."""
        chunk = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish_reason,
            }],
        }
        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def _send_chat_stream_keepalive(self, request_id, model_name, initial=False):
        self._send_chat_stream_chunk(
            request_id,
            model_name,
            {"role": "assistant"} if initial else {},
            None,
        )
        if not initial:
            log(f"stream_keepalive id={request_id} model={model_name} kind=data_chunk")

    def _send_sse_keepalive_comment(self):
        self.wfile.write(b": keep-alive\n\n")
        self.wfile.flush()

    def _wait_with_heartbeat(self, operation, heartbeat=None, on_first_response=None):
        self._start_sse()

        def result(notify=None):
            yield operation(notify) if on_first_response is not None else operation()

        upstream = result if on_first_response is not None else result()
        for value in _stream_with_heartbeat(upstream):
            if value is _UPSTREAM_RESPONSE_STARTED:
                on_first_response()
            elif value is None:
                if heartbeat is None:
                    self._send_sse_keepalive_comment()
                else:
                    heartbeat()
            else:
                return value

    def _parse_body(self, body: bytes) -> dict:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks = []
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size_text = size_line.split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    raise ValueError("invalid chunked request body")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)

        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _access_allowed(self):
        if not _client_allowed(self):
            self.send_route_error("client IP is not allowed", 403, code="access_denied")
            return False
        return True

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        # Authorization: Bearer <key>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in keys:
            return True
        # header keys (OpenAI x-api-key / Google x-goog-api-key)
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h, "") in keys:
                return True
        # query param ?key= (Gemini CLI native style)
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key=") and pair[4:] in keys:
                    return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            if not self._access_allowed():
                return
            if self.path.startswith("/v1") and not self._authorized():
                self.send_route_error("invalid api key", 401, code="invalid_api_key")
                return
            if self.path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                     for n, c in get_models().items()
                ]})
            elif self.path.startswith("/v1/caches/"):
                cache_id = self.path.rsplit("/", 1)[-1]
                cached = CACHE.get(cache_id)
                if not cached:
                    self.send_openai_error("cache not found", 404, param="cache_id", code="not_found")
                    return
                self.send_json({"object": "cache", **cached})
            elif self.path.startswith("/v1beta/models"):
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in get_models().items()
                ]})
            elif self.path == "/":
                healthy = rotation_worker_alive()
                self.send_json({"status": "ok" if healthy else "unhealthy",
                                "version": __version__, "models": list(get_models().keys())},
                               200 if healthy else 503)
            else:
                self.send_route_error("not found", 404, code="not_found")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        try:
            if not self._access_allowed():
                return
            self.send_route_error("not found", 404, code="not_found")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        self._sse_started = False
        try:
            if not self._access_allowed():
                return
            if self.path.startswith("/v1") and not self._authorized():
                self.send_route_error("invalid api key", 401, code="invalid_api_key")
                return
            body = self._read_request_body()
            if self.path == "/v1/chat/completions":
                self._handle_chat(body)
            elif self.path == "/v1/caches":
                self._handle_cache_create(body)
            elif self.path == "/v1/images/edits":
                self._handle_image_edit(body)
            elif self.path == "/v1/images/generations":
                self._handle_image_generation(body)
            elif self.path == "/v1/responses":
                self._handle_responses(body)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            else:
                self.send_route_error("not found", 404, code="not_found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_route_error(str(e), 500, code="internal_error")
            except Exception:
                pass

    def do_DELETE(self):
        if not self.path.startswith("/v1/caches/"):
            self.send_route_error("not found", 404, code="not_found")
            return
        if not _client_allowed(self):
            self.send_openai_error("client IP is not allowed", 403, code="access_denied")
            return
        if not self._authorized():
            self.send_openai_error("invalid api key", 401, code="invalid_api_key")
            return
        cache_id = self.path.rsplit("/", 1)[-1]
        if not CACHE.delete(cache_id):
            self.send_openai_error("cache not found", 404, param="cache_id", code="not_found")
            return
        self.send_json({"id": cache_id, "deleted": True})

    def _handle_cache_create(self, body: bytes):
        req = self._parse_body(body)
        messages = req.get("messages") if req else None
        if not isinstance(messages, list) or not messages:
            self.send_openai_error("messages must be a non-empty array", 400,
                               param="messages", code="invalid_value")
            return
        ttl = req.get("ttl_seconds", CONFIG["cache_default_ttl_sec"])
        if not isinstance(ttl, int) or ttl < 1 or ttl > CONFIG["cache_max_ttl_sec"]:
            self.send_openai_error("invalid ttl_seconds", 400,
                               param="ttl_seconds", code="invalid_value")
            return
        self.send_json({"object": "cache", **CACHE.create(messages, ttl)}, 201)

    # ─── /v1/images/edits ─────────────────────────────────────────────────────

    def _handle_image_edit(self, body: bytes):
        try:
            content_type = self.headers.get("Content-Type", "")
            if content_type.lower().startswith("application/json"):
                request = _parse_json_edit_request(body)
            else:
                request = _parse_multipart_edit_request(body, content_type)
        except _ImageRequestError as exc:
            self.send_openai_error(str(exc), 400, param=exc.param, code=exc.code)
            return

        err = _image_model_error(request["model"])
        if err:
            self.send_openai_error(err, 400, param="model", code="model_not_found")
            return

        deadline = _image_request_deadline()
        prompt = request["prompt"]
        images = request["images"]
        image = images[0]
        mask = request["mask"]
        n = request["n"]
        size = request["size"]
        output_format = request["output_format"]
        response_format = request["response_format"]
        if request["ignored_fields"]:
            log("image_edit_compat ignored_fields=%s"
                % ",".join(request["ignored_fields"]))

        crop_plan = None
        context_plan = None
        mask_geometry_info = None
        if mask is not None:
            if CONFIG.get("image_edit_context_crop", True):
                try:
                    context_plan = prepare_context_crop(image[0], mask[0], image[1])
                except ImageOpsUnavailable:
                    log("image_edit_context_crop_fallback reason=imagemagick_unavailable")
                except ValueError as exc:
                    self.send_openai_error(str(exc), 400, param="mask")
                    return
                except RuntimeError as exc:
                    self.send_openai_error(
                        "image edit preparation failed: " + safe_error_message(exc, 500),
                        500, code="upstream_error")
                    return
                if context_plan is not None:
                    mask_geometry_info = context_plan.geometry
            if context_plan is None and CONFIG.get("image_edit_crop", False):
                try:
                    crop_plan = prepare_crop_edit(image[0], mask[0], image[1])
                except ImageOpsUnavailable:
                    log("image_edit_crop_fallback reason=imagemagick_unavailable")
                except ValueError as exc:
                    self.send_openai_error(str(exc), 400, param="mask")
                    return
                except RuntimeError as exc:
                    self.send_openai_error(
                        "image edit preparation failed: " + safe_error_message(exc, 500),
                        500, code="upstream_error")
                    return
            if crop_plan is None:
                try:
                    mask_geometry_info = mask_geometry(mask[0])
                except ValueError as exc:
                    self.send_openai_error(str(exc), 400, param="mask")
                    return

        drift_limit = None
        min_change = 0.0
        frame_expected = None
        stats_ref = None
        size_hint = _size_instruction(size)
        if context_plan is not None:
            frame_expected = context_plan.crop_size
            # No mask and no marker: the model gets a normal photo crop with real
            # surroundings, which is the only setup where it edits instead of
            # filling the marked shape.
            attachments = [(context_plan.crop_image, "image/png", "crop.png")]
            edit_prompt = _context_edit_prompt(
                prompt, context_plan.geometry, context_plan.crop_box,
                context_plan.crop_size,
            ) + size_hint
            strip_marker = None
            stats_ref = (context_plan.crop_image, "image/png", context_plan.crop_mask)
            drift_limit = float(CONFIG.get("image_edit_drift_limit", 40.0))
            min_change = float(CONFIG.get("image_edit_min_change", 8.0))

            def transform(edited_bytes, edited_mime):
                if CONFIG.get("image_edit_tone_match", True):
                    try:
                        edited_bytes, edited_mime = match_crop_tone(
                            edited_bytes, edited_mime, image[0], image[1],
                            context_plan.crop_box, context_plan.crop_mask,
                        )
                    except (ImageOpsUnavailable, RuntimeError, ValueError) as exc:
                        log("image_edit_tone_match_failed error_type=%s"
                            % type(exc).__name__)
                return composite_crop_edit(
                    image[0], image[1], edited_bytes, edited_mime,
                    context_plan.crop_mask, context_plan.crop_box,
                    context_plan.crop_size,
                )

            log("image_edit_mode=context_crop box=%s size=%s mask_pixels=%d" % (
                context_plan.crop_box, context_plan.crop_size,
                context_plan.geometry.transparent_pixels,
            ))
        elif crop_plan is not None:
            frame_expected = crop_plan.crop_size
            attachments = [
                (crop_plan.crop_image, "image/png", "crop.png"),
                (crop_plan.crop_mask, "image/png", "mask.png"),
            ]
            edit_prompt = _crop_edit_prompt(prompt, crop_plan.crop_size) + size_hint
            strip_marker = None
            transform = lambda edited_bytes, edited_mime: composite_crop_edit(
                image[0], image[1], edited_bytes, edited_mime,
                crop_plan.crop_mask, crop_plan.crop_box, crop_plan.crop_size,
            )
            log("image_edit_crop box=%s size=%s mask_pixels=%d" % (
                crop_plan.crop_box, crop_plan.crop_size,
                crop_plan.geometry.transparent_pixels,
            ))
        elif mask is not None:
            width = mask_geometry_info.width
            height = mask_geometry_info.height
            full_box = (0, 0, width, height)
            full_size = (width, height)
            locator_plan = None
            if CONFIG.get("image_edit_locator", True):
                try:
                    locator_plan = build_marker_locator(image[0], image[1], mask[0])
                except ImageOpsUnavailable:
                    log("image_edit_locator_fallback reason=imagemagick_unavailable")
                except (RuntimeError, ValueError) as exc:
                    log("image_edit_locator_fallback reason=%s" % type(exc).__name__)
            if locator_plan is not None:
                # Gemini copies the colors of an alpha mask into the edit, so the
                # mask stays server-side. The locator and a clean copy of the
                # source go along with the original image instead.
                attachments = [
                    image,
                    (locator_plan.locator_image, "image/png", "locator.png"),
                    image,
                ]
                edit_prompt = _edit_prompt(prompt, mask_geometry_info, locator=True) + size_hint
                strip_marker = (
                    lambda edited_bytes, edited_mime: strip_edit_marker(
                        edited_bytes, edited_mime, image[0], image[1],
                        locator_plan.marker_alpha,
                    )
                )
                log("image_edit_mode=locator_composite size=%s mask_pixels=%d" % (
                    full_size, mask_geometry_info.transparent_pixels,
                ))
            else:
                attachments = [image, mask]
                edit_prompt = _edit_prompt(prompt, mask_geometry_info) + size_hint
                strip_marker = None
                log("image_edit_mode=prompt_composite size=%s mask_pixels=%d" % (
                    full_size, mask_geometry_info.transparent_pixels,
                ))
            transform = lambda edited_bytes, edited_mime: composite_crop_edit(
                image[0], image[1], edited_bytes, edited_mime,
                mask[0], full_box, full_size,
            )
            frame_expected = full_size
            stats_ref = (image[0], image[1], mask[0])
            drift_limit = float(CONFIG.get("image_edit_drift_limit", 40.0))
            min_change = float(CONFIG.get("image_edit_min_change", 8.0))
        else:
            attachments = [image]
            edit_prompt = prompt + size_hint
            transform = None
            strip_marker = None
        input_sizes = _attachment_sizes(attachments)
        attempts = 1
        if drift_limit is not None:
            attempts = max(1, min(3, int(CONFIG.get("image_edit_attempts", 2))))
        try:
            _image_budget_remaining(deadline)
            file_refs = _upload_images(attachments)
            outputs = []
            output_mimes = []
            actual_sizes = []
            output_sizes = []
            for output_index in range(n):
                _image_budget_remaining(deadline)
                best = None
                best_rank = None
                drift = change = None
                rejected_frames = 0
                rejected_size = None
                for attempt in range(1, attempts + 1):
                    attempt_prompt = edit_prompt
                    if attempt > 1:
                        # Re-sending the identical prompt tends to reproduce the same
                        # no-op. Nudge the model to actually perform the edit.
                        attempt_prompt += (
                            "\n\nDo not return the photo unchanged. Make the requested "
                            "change inside the selected area now."
                        )
                    result = generate_image_structured(
                        attempt_prompt, file_refs=file_refs,
                        budget_sec=_image_budget_remaining(deadline))
                    if not result.images:
                        raise RuntimeError("Gemini returned no generated image metadata")
                    edited_bytes, edited_mime, source_kind, source_url = (
                        _download_generated_image_bytes(
                            result.images[0], deadline=deadline)
                    )
                    if frame_expected is not None and CONFIG.get("image_edit_frame_gate", True):
                        # Gemini sometimes re-frames the scene. Fitting that back into
                        # the source box would crop real content, so refuse it.
                        try:
                            actual_size = image_dimensions(edited_bytes, edited_mime)
                        except (ImageOpsUnavailable, RuntimeError) as exc:
                            actual_size = None
                            log("image_edit_frame_check_failed error_type=%s"
                                % type(exc).__name__)
                        if actual_size is not None and not _frame_matches(
                            actual_size, frame_expected,
                            float(CONFIG.get("image_edit_frame_tolerance", 0.02)),
                        ):
                            rejected_frames += 1
                            rejected_size = actual_size
                            log("image_edit_attempt attempt=%d/%d rejected=framing "
                                "size=%dx%d expected=%dx%d" % (
                                    attempt, attempts, actual_size[0], actual_size[1],
                                    frame_expected[0], frame_expected[1]))
                            continue
                    if strip_marker is not None:
                        try:
                            edited_bytes, edited_mime = strip_marker(edited_bytes, edited_mime)
                        except ImageOpsUnavailable:
                            log("image_edit_marker_strip_failed reason=imagemagick_unavailable")
                        except (RuntimeError, ValueError) as exc:
                            log("image_edit_marker_strip_failed reason=%s"
                                % type(exc).__name__)
                    stats = None
                    if drift_limit is not None:
                        try:
                            stats = thumbnail_mask_stats(
                                stats_ref[0], stats_ref[1], edited_bytes, edited_mime,
                                stats_ref[2],
                            )
                        except ImageOpsUnavailable:
                            stats = None
                        except (RuntimeError, ValueError) as exc:
                            log("image_edit_stats_unavailable error_type=%s"
                                % type(exc).__name__)
                    if stats is not None:
                        drift, change = stats
                        rank = _edit_attempt_rank(drift, change, drift_limit, min_change)
                        if best is None or rank < best_rank:
                            best_rank = rank
                            best = (drift, change, edited_bytes, edited_mime,
                                    source_kind, source_url)
                        log("image_edit_attempt attempt=%d/%d mask_drift=%.1f "
                            "selection_change=%.1f" % (attempt, attempts, drift, change))
                        if rank[0] == 0:
                            break
                    elif best is None:
                        best = (None, None, edited_bytes, edited_mime,
                                source_kind, source_url)
                        break
                    if attempt == attempts:
                        break
                if best is None:
                    if rejected_frames and rejected_size and frame_expected:
                        raise RuntimeError(
                            "Gemini reframed the photo (%dx%d instead of %dx%d); the edit "
                            "was discarded instead of returning a cropped image"
                            % (rejected_size + tuple(frame_expected)))
                    raise RuntimeError("Gemini returned no generated image metadata")
                drift, change, edited_bytes, edited_mime, source_kind, source_url = best
                if transform is not None:
                    edited_bytes, edited_mime = transform(edited_bytes, edited_mime)
                edited_bytes, edited_mime = _apply_output_size(
                    edited_bytes, edited_mime, size, output_format,
                    request["output_compression"])
                output_mimes.append(edited_mime)
                outputs.append(_image_data(edited_bytes, response_format))
                output_sizes.append(_native_image_size(edited_bytes))
                actual_sizes.append(_actual_image_size(edited_bytes, edited_mime))
                log(
                    "image_edit_result output=%d/%d selected_full_size=%s response_format=%s "
                    "mask_drift=%s selection_change=%s source_host=%s source_path=%s"
                    % (
                        output_index + 1, n, source_kind == "full_size", response_format,
                        "n/a" if drift is None else "%.1f" % drift,
                        "n/a" if change is None else "%.1f" % change,
                        (urlsplit(source_url).hostname or "-"),
                        (urlsplit(source_url).path or "/"),
                    )
                )
        except TimeoutError as exc:
            self.send_openai_error(
                "image request timed out: " + safe_error_message(exc, 504),
                504, code="request_timeout")
            return
        except ValueError as exc:
            self.send_openai_error(str(exc), 400, code="invalid_value")
            return
        except ImageOpsUnavailable:
            self.send_openai_error(
                "image processing is unavailable", 500, code="upstream_error")
            return
        except Exception as exc:
            self.send_openai_error(
                "upstream error: " + safe_error_message(exc, 500), 500,
                code="upstream_error")
            return
        response = {"created": int(time.time()), "data": outputs}
        actual_size = _common_actual_size(actual_sizes)
        if actual_size is not None:
            response["size"] = "%dx%d" % actual_size
        if output_mimes:
            output_format_value = _mime_output_format(output_mimes[0])
            if output_format_value:
                response["output_format"] = output_format_value
        response["usage"] = _image_usage(
            edit_prompt, input_sizes=input_sizes, output_sizes=output_sizes)
        self.send_json(response)

    # ─── /v1/images/generations ───────────────────────────────────────────────

    def _handle_image_generation(self, body: bytes):
        try:
            request = _parse_image_generation_request(body)
        except _ImageRequestError as exc:
            self.send_openai_error(str(exc), 400, param=exc.param, code=exc.code)
            return
        err = _image_model_error(request["model"])
        if err:
            self.send_openai_error(err, 400, param="model", code="model_not_found")
            return
        if request["ignored_fields"]:
            log("image_generation_compat ignored_fields=%s"
                % ",".join(request["ignored_fields"]))
        prompt = request["prompt"] + _size_instruction(request["size"])
        deadline = _image_request_deadline()
        try:
            outputs = []
            output_mimes = []
            actual_sizes = []
            output_sizes = []
            for _ in range(request["n"]):
                result = generate_image_structured(
                    prompt, budget_sec=_image_budget_remaining(deadline))
                if not result.images:
                    raise RuntimeError("Gemini returned no generated image metadata")
                image_bytes, mime, source_kind, source_url = (
                    _download_generated_image_bytes(
                        result.images[0], deadline=deadline)
                )
                image_bytes, mime = _apply_output_size(
                    image_bytes, mime, request["size"], request["output_format"],
                    request["output_compression"])
                output_mimes.append(mime)
                outputs.append(_image_data(image_bytes, request["response_format"]))
                output_sizes.append(_native_image_size(image_bytes))
                actual_sizes.append(_actual_image_size(image_bytes, mime))
                log("image_generation_result selected_full_size=%s response_format=%s "
                    "source_host=%s source_path=%s" % (
                        source_kind == "full_size", request["response_format"],
                        (urlsplit(source_url).hostname or "-"),
                        (urlsplit(source_url).path or "/")))
        except TimeoutError as exc:
            self.send_openai_error(
                "image request timed out: " + safe_error_message(exc, 504),
                504, code="request_timeout")
            return
        except Exception as exc:
            self.send_openai_error(
                "upstream error: " + safe_error_message(exc, 500), 500,
                code="upstream_error")
            return
        response = {"created": int(time.time()), "data": outputs}
        actual_size = _common_actual_size(actual_sizes)
        if actual_size is not None:
            response["size"] = "%dx%d" % actual_size
        if output_mimes:
            output_format_value = _mime_output_format(output_mimes[0])
            if output_format_value:
                response["output_format"] = output_format_value
        response["usage"] = _image_usage(prompt, output_sizes=output_sizes)
        self.send_json(response)

    # ─── /v1/chat/completions ─────────────────────────────────────────────────

    def _handle_chat(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_openai_error("invalid JSON", 400, code="invalid_json")
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_openai_error(err, 400, param="model", code="model_not_found")
            return

        messages = req.get("messages", [])
        cache_id = req.get("cache_id")
        cached = CACHE.get(cache_id) if cache_id else None
        prefix = _auto_cache_prefix(messages)
        if not cache_id and CONFIG.get("auto_cache") and prefix:
            cached = CACHE.get_or_create_auto(prefix, CONFIG["cache_default_ttl_sec"], _cache_scope(self))
            cache_id = cached["id"]
        if cached:
            if prefix and cached["messages"] == prefix:
                messages = cached["messages"] + messages[len(prefix):]
            else:
                messages = cached["messages"] + messages
        elif req.get("cache_id"):
            self.send_openai_error("cache not found", 404, param="cache_id", code="not_found")
            return
        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        cached_tokens = _cached_tokens(cached["messages"], tools, tool_choice) if cache_id else 0
        if not prompt.strip():
            self.send_openai_error("empty prompt", 400, param="messages", code="invalid_value")
            return

        stream = req.get("stream", False)
        image_prompt = _chat_image_prompt(req)
        precomputed_text = None
        if image_prompt:
            try:
                generated_text, image_data = _generated_image_output(image_prompt, "url")
                image_markdown = f"![Generated image]({image_data['url']})"
                precomputed_text = "\n\n".join(
                    part for part in (generated_text, image_markdown) if part
                )
                tools = None
                tool_choice = "none"
                images = []
            except Exception as e:
                self.send_openai_error(
                    f"upstream error: {safe_error_message(e, 500)}", 502,
                    code="upstream_error")
                return
        tools_enabled = bool(tools) and tool_choice != "none"
        request_budget = max(1, int(CONFIG.get("agent_request_budget_sec", 300))) if tools_enabled else None
        request_attempts = 1 if tools_enabled else max(1, int(CONFIG.get("retry_attempts", 3)))
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        started_at = time.monotonic()
        log(
            f"request_start id={cid} endpoint=chat model={model_name} "
            f"request_bytes={len(body)} prompt_chars={len(prompt)} "
            f"prompt_tokens_est={len(prompt)//4} stream={bool(stream)} "
            f"tools={bool(req.get('tools'))} tools_enabled={tools_enabled} "
            f"request_timeout_sec={int(CONFIG.get('request_timeout_sec', 300))} "
            f"budget_sec={request_budget if request_budget is not None else 'none'} "
            f"max_attempts={request_attempts}"
        )
        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
            self.send_openai_error(f"upstream error: {e}", 502, code="upstream_error")
            return

        stream_started = False

        def start_chat_stream(source="upstream_data"):
            nonlocal stream_started
            if not stream_started:
                self._send_chat_stream_keepalive(cid, model_name, initial=True)
                stream_started = True
                log(f"stream_preamble id={cid} endpoint=chat kind=role_chunk source={source} "
                    f"duration_ms={int((time.monotonic() - started_at) * 1000)}")

        def keep_chat_stream_alive():
            if stream_started:
                self._send_chat_stream_keepalive(cid, model_name)
            else:
                self._send_sse_keepalive_comment()

        if stream and (not tools or tool_choice == "none"):
            stream_text = ""
            try:
                self._start_sse()
                self._send_sse_keepalive_comment()

                def upstream(notify):
                    if precomputed_text is not None:
                        yield precomputed_text
                        return
                    yield from generate_stream(
                        prompt, model_id, think_mode, file_refs, extra_fields,
                        session_hash=_session_hash(model_name), request_id=cid,
                        on_first_response=notify,
                    )

                for delta in _stream_with_heartbeat(upstream):
                    if delta is _UPSTREAM_RESPONSE_STARTED:
                        start_chat_stream()
                        continue
                    if delta is None:
                        keep_chat_stream_alive()
                        continue
                    start_chat_stream(source="content")
                    stream_text += delta
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                start_chat_stream(source="complete")
                end = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
                usage_chunk = {
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": model_name, "choices": [],
                    "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(stream_text)//4,
                               "total_tokens": (len(prompt)+len(stream_text))//4,
                               "prompt_tokens_details": {"cached_tokens": cached_tokens}},
                }
                self.wfile.write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                log(
                    f"request_finish id={cid} endpoint=chat status=ok "
                    f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                    f"output_chars={len(stream_text)} output_tokens_est={len(stream_text)//4}"
                )
            except (BrokenPipeError, ConnectionResetError):
                log(
                    f"request_finish id={cid} endpoint=chat status=client_disconnect "
                    f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                    f"output_chars={len(stream_text)}"
                )
            except Exception as e:
                error = safe_error_message(e, 500)
                log(
                    f"request_finish id={cid} endpoint=chat status=error "
                    f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                    f"http_status=200_or_stream_error error_type={type(e).__name__} "
                    f"output_chars={len(stream_text)} error={error}"
                )
                try:
                    error_event = _openai_error_payload(
                        "upstream stream interrupted", 500,
                        code="upstream_stream_error",
                        retryable=not bool(stream_text) and not isinstance(e, GeminiUpstreamError),
                    )
                    self.wfile.write(f"data: {json.dumps(error_event)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        try:
            def operation(notify=None):
                if precomputed_text is not None:
                    return precomputed_text
                return self._generate_buffered(
                    prompt, model_id, think_mode, file_refs, extra_fields,
                    model_name, cid, tools_enabled, on_first_response=notify,
                )

            if stream:
                self._start_sse()
                self._send_sse_keepalive_comment()
                text = self._wait_with_heartbeat(
                    operation, heartbeat=keep_chat_stream_alive,
                    on_first_response=start_chat_stream,
                )
            else:
                text = operation()
        except Exception as e:
            error = safe_error_message(e, 500)
            status_code = 503 if isinstance(e, BlockingIOError) else 504 if isinstance(e, TimeoutError) else 502
            log(
                f"request_finish id={cid} endpoint=chat status=error "
                f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                f"http_status={status_code} error_type={type(e).__name__} "
                f"tools_enabled={tools_enabled} error={error}"
            )
            self.send_openai_error(
                f"upstream error: {error}", status_code, code="upstream_error",
                retryable=not isinstance(e, GeminiUpstreamError))
            return

        quality = _classify_response_quality(prompt, text, bool(tools) and tool_choice != "none")
        log(f"response_quality id={cid} category={quality} prompt_chars={len(prompt)} output_chars={len(text or '')}")
        if quality == "malformed_or_unparsed_tool_call":
            log(f"request_finish id={cid} endpoint=chat status=tool_parse_error")
            self.send_openai_error(
                "Gemini returned an incomplete or malformed tool_call", 502,
                code="tool_call_parse_error", retryable=True)
            return
        if quality == "temporary_refusal":
            log(f"request_finish id={cid} endpoint=chat status=temporary_refusal")
            self.send_openai_error(
                "Gemini returned a temporary refusal; retry the current task", 503,
                code="upstream_temporary_refusal", retryable=True)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
            try:
                validate_tool_calls(tool_calls, tools)
            except ValueError as e:
                self.send_openai_error(
                    str(e), 502, code="tool_call_parse_error", retryable=True)
                return
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            self._start_sse()
            start_chat_stream(source="complete")
            delta = {"content": text or None}
            if tool_calls:
                delta["tool_calls"] = [
                    {**tc, "index": index}
                    for index, tc in enumerate(tool_calls)
                ]
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            usage_chunk = {
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model_name, "choices": [],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text or "")//4,
                           "total_tokens": (len(prompt)+len(text or ""))//4,
                           "prompt_tokens_details": {"cached_tokens": cached_tokens}},
            }
            self.wfile.write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            log(
                f"request_finish id={cid} endpoint=chat status=ok "
                f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                f"output_chars={len(text or '')} output_tokens_est={len(text or '')//4} "
                f"finish={finish} short_output={len(text or '') < 200} buffered_for_tools=true"
            )
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text or "")//4,
                          "total_tokens": (len(prompt)+len(text or ""))//4,
                          "prompt_tokens_details": {"cached_tokens": cached_tokens}},
            })
            log(
                f"request_finish id={cid} endpoint=chat status=ok "
                f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                f"output_chars={len(text or '')} output_tokens_est={len(text or '')//4} "
                f"finish={finish} short_output={len(text or '') < 200}"
            )

    # ─── /v1/responses (Codex CLI) ───────────────────────────────────────────

    def _handle_responses(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_openai_error("invalid JSON", 400, code="invalid_json")
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_openai_error(err, 400, param="model", code="model_not_found")
            return

        input_items = req.get("input", [])
        raw_tools = req.get("tools")
        image_generation_requested = isinstance(raw_tools, list) and any(
            isinstance(tool, dict) and tool.get("type") == "image_generation"
            for tool in raw_tools
        )
        # Image generation is a native request signal, not an emulated function.
        tools = ([tool for tool in raw_tools
                  if isinstance(tool, dict) and tool.get("type") != "image_generation"]
                 if isinstance(raw_tools, list) else raw_tools)
        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("type") == "function_call":
                        messages.append({
                            "role": "assistant", "content": None,
                            "tool_calls": [{
                                "id": item.get("call_id", item.get("id", "")),
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": item.get("arguments", "{}"),
                                },
                            }],
                        })
                    elif item.get("type") in ("input_text", "input_image", "image"):
                        messages.append({"role": "user", "content": [item]})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text":
                                        text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call":
                                        tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        messages.append({"role": role, "content": item.get("content", "")})

        cache_id = req.get("cache_id")
        cached = CACHE.get(cache_id) if cache_id else None
        prefix = _auto_cache_prefix(messages)
        if not cache_id and CONFIG.get("auto_cache") and prefix:
            cached = CACHE.get_or_create_auto(prefix, CONFIG["cache_default_ttl_sec"], _cache_scope(self))
            cache_id = cached["id"]
        if cached:
            if prefix and cached["messages"] == prefix:
                messages = cached["messages"] + messages[len(prefix):]
            else:
                messages = cached["messages"] + messages
        elif req.get("cache_id"):
            self.send_openai_error("cache not found", 404, param="cache_id", code="not_found")
            return

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        cached_tokens = _cached_tokens(cached["messages"], tools, tool_choice) if cache_id else 0
        if not prompt.strip():
            self.send_openai_error("empty input", 400, param="input", code="invalid_value")
            return

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        tools_enabled = bool(tools) and tool_choice != "none"
        request_budget = max(1, int(CONFIG.get("agent_request_budget_sec", 300))) if tools_enabled else None
        request_attempts = 1 if tools_enabled else max(1, int(CONFIG.get("retry_attempts", 3)))
        started_at = time.monotonic()
        log(
            f"request_start id={rid} endpoint=responses model={model_name} "
            f"request_bytes={len(body)} prompt_chars={len(prompt)} "
            f"prompt_tokens_est={len(prompt)//4} stream={bool(req.get('stream'))} "
            f"tools={bool(tools)} tools_enabled={tools_enabled} "
            f"request_timeout_sec={int(CONFIG.get('request_timeout_sec', 300))} "
            f"budget_sec={request_budget if request_budget is not None else 'none'} "
            f"max_attempts={request_attempts}"
        )
        if image_generation_requested and images:
            self.send_openai_error(
                "image generation with input images is not supported", 400,
                param="input", code="unsupported_value")
            return

        generated_image_call = None
        try:
            file_refs = _upload_images(images)
            if image_generation_requested:
                result = generate_image_structured(prompt)
                text = result.text
                if not result.images:
                    raise RuntimeError("Gemini returned no generated image metadata")
                image_data, _source_kind, _source_url = _generated_image_payload(result.images[0], "b64_json")
                generated_image_call = {
                    "type": "image_generation_call",
                    "id": f"imggen_{uuid.uuid4().hex[:12]}",
                    "status": "completed",
                    "result": image_data["b64_json"],
                }
            else:
                operation = lambda: self._generate_buffered(
                    prompt, model_id, think_mode, file_refs, extra_fields,
                    model_name, rid, tools_enabled,
                )
                text = self._wait_with_heartbeat(operation) if req.get("stream") else operation()
        except Exception as e:
            error = safe_error_message(e, 500)
            status_code = 503 if isinstance(e, BlockingIOError) else 504 if isinstance(e, TimeoutError) else 502
            log(
                f"request_finish id={rid} endpoint=responses status=error "
                f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                f"http_status={status_code} error_type={type(e).__name__} "
                f"tools_enabled={tools_enabled} error={error}"
            )
            self.send_openai_error(
                f"upstream error: {error}", status_code, code="upstream_error",
                retryable=not isinstance(e, GeminiUpstreamError))
            return

        quality = _classify_response_quality(prompt, text, bool(tools) and tool_choice != "none")
        log(f"response_quality id={rid} category={quality} prompt_chars={len(prompt)} output_chars={len(text or '')}")
        if quality == "malformed_or_unparsed_tool_call":
            log(f"request_finish id={rid} endpoint=responses status=tool_parse_error")
            self.send_openai_error(
                "Gemini returned an incomplete or malformed tool_call", 502,
                code="tool_call_parse_error", retryable=True)
            return
        if quality == "temporary_refusal":
            log(f"request_finish id={rid} endpoint=responses status=temporary_refusal")
            self.send_openai_error(
                "Gemini returned a temporary refusal; retry the current task", 503,
                code="upstream_temporary_refusal", retryable=True)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
            try:
                validate_tool_calls(tool_calls, tools)
            except ValueError as e:
                self.send_openai_error(
                    str(e), 502, code="tool_call_parse_error", retryable=True)
                return
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or (not tool_calls and not generated_image_call):
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})
        if generated_image_call:
            output.append(generated_image_call)

        if req.get("stream"):
            self._start_sse()
            sequence_number = 0

            def emit(event_type, **fields):
                nonlocal sequence_number
                sequence_number += 1
                event = {
                    "type": event_type,
                    "sequence_number": sequence_number,
                    **fields,
                }
                self.wfile.write(
                    f"event: {event_type}\ndata: {json.dumps(event)}\n\n".encode()
                )

            usage = {
                "input_tokens": len(prompt) // 4,
                "output_tokens": len(text or "") // 4,
                "total_tokens": (len(prompt) + len(text or "")) // 4,
                "input_tokens_details": {"cached_tokens": cached_tokens},
            }
            base_response = {
                "id": rid,
                "object": "response",
                "created_at": int(time.time()),
                "model": model_name,
            }
            emit(
                "response.created",
                response={
                    **base_response,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            )
            emit(
                "response.in_progress",
                response={
                    **base_response,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            )
            for output_index, item in enumerate(output):
                if item["type"] == "function_call":
                    pending_item = {
                        "type": "function_call",
                        "id": item["id"],
                        "call_id": item["call_id"],
                        "name": item["name"],
                        "arguments": "",
                        "status": "in_progress",
                    }
                    emit(
                        "response.output_item.added",
                        output_index=output_index,
                        item=pending_item,
                    )
                    emit(
                        "response.function_call_arguments.delta",
                        item_id=item["id"],
                        output_index=output_index,
                        delta=item["arguments"],
                    )
                    emit(
                        "response.function_call_arguments.done",
                        item_id=item["id"],
                        output_index=output_index,
                        arguments=item["arguments"],
                    )
                    emit(
                        "response.output_item.done",
                        output_index=output_index,
                        item=item,
                    )
                elif item["type"] == "image_generation_call":
                    emit(
                        "response.output_item.added",
                        output_index=output_index,
                        item={"type": "image_generation_call", "id": item["id"], "status": "in_progress"},
                    )
                    emit(
                        "response.output_item.done",
                        output_index=output_index,
                        item=item,
                    )
                elif item["type"] == "message":
                    pending_item = {
                        "type": "message",
                        "id": item["id"],
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    }
                    emit(
                        "response.output_item.added",
                        output_index=output_index,
                        item=pending_item,
                    )
                    for content_index, content_part in enumerate(item["content"]):
                        event_fields = {
                            "item_id": item["id"],
                            "output_index": output_index,
                            "content_index": content_index,
                        }
                        emit(
                            "response.content_part.added",
                            **event_fields,
                            part={
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        )
                        emit(
                            "response.output_text.delta",
                            **event_fields,
                            delta=content_part["text"],
                        )
                        emit(
                            "response.output_text.done",
                            **event_fields,
                            text=content_part["text"],
                        )
                        emit(
                            "response.content_part.done",
                            **event_fields,
                            part=content_part,
                        )
                    emit(
                        "response.output_item.done",
                        output_index=output_index,
                        item=item,
                    )
            emit(
                "response.completed",
                response={
                    **base_response,
                    "status": "completed",
                    "output": output,
                    "usage": usage,
                },
            )
            self.wfile.flush()
            log(
                f"request_finish id={rid} endpoint=responses status=ok "
                f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                f"output_chars={len(text or '')} output_tokens_est={len(text or '')//4}"
            )
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text or "")//4,
                                      "total_tokens": (len(prompt)+len(text or ""))//4,
                                      "input_tokens_details": {"cached_tokens": cached_tokens}}})
            log(
                f"request_finish id={rid} endpoint=responses status=ok "
                f"duration_ms={int((time.monotonic() - started_at) * 1000)} "
                f"output_chars={len(text or '')} output_tokens_est={len(text or '')//4}"
            )

    # ─── /v1beta/models (Google Gemini CLI) ──────────────────────────────────

    def _handle_google_generate(self, body: bytes, stream: bool):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        model_name = m.group(1) if m else CONFIG["default_model"]
        model_name, model_id, think_mode, err, extra_fields = resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tool_config = req.get("toolConfig", {})
        fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"
        prompt, images = google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        log(f"Google API: model={model_name} stream={stream} tools={has_tools} prompt_len={len(prompt)}")

        if stream and not has_tools:
            try:
                self._start_sse()
                full_text = ""
                for delta in _stream_with_heartbeat(generate_stream(prompt, model_id, think_mode, file_refs, extra_fields, session_hash=_session_hash(model_name), request_id=f"google-{uuid.uuid4().hex[:10]}")):
                    if delta is None:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    full_text += delta
                    chunk_obj = {
                        "candidates": [{"content": {"parts": [{"text": delta}], "role": "model"}, "index": 0}],
                        "modelVersion": model_name,
                    }
                    self.wfile.write(f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                final_chunk = {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": {
                        "promptTokenCount": len(prompt) // 4,
                        "candidatesTokenCount": len(full_text) // 4,
                        "totalTokenCount": (len(prompt) + len(full_text)) // 4,
                    },
                    "modelVersion": model_name,
                }
                self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Google stream error: {e}")
            return

        try:
            operation = lambda: generate(prompt, model_id, think_mode, file_refs, extra_fields, session_hash=_session_hash(model_name), request_id=f"google-{uuid.uuid4().hex[:10]}")
            text = self._wait_with_heartbeat(operation) if stream else operation()
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if not text:
            log("Warning: empty response from Gemini")

        response_parts = []
        if has_tools and text:
            clean_text, function_calls = parse_google_function_calls(text)
            if function_calls:
                if clean_text:
                    response_parts.append({"text": clean_text})
                for fc in function_calls:
                    response_parts.append({"functionCall": {"name": fc["name"], "args": fc["args"]}})
            else:
                response_parts.append({"text": text})
        else:
            response_parts.append({"text": text or "I apologize, but I was unable to generate a response. Please try again."})

        candidate = {
            "content": {"parts": response_parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text or "") // 4,
            "totalTokenCount": (len(prompt) + len(text or "")) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self._start_sse()
            self.wfile.write(f"data: {json.dumps(response_obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
