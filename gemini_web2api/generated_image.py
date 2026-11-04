"""Parsing and bounded download helpers for Gemini-generated images."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover - exercised where optional dependency is absent
    curl_requests = None
    HAS_CURL_CFFI = False

from .config import CONFIG

MAX_GENERATED_IMAGE_BYTES = 10 * 1024 * 1024
MAX_GENERATED_IMAGE_REDIRECTS = 3
MAX_GENERATED_IMAGE_MEDIATORS = 2
MAX_GENERATED_IMAGE_URL_TEXT_BYTES = 8192
_ALLOWED_GENERATED_IMAGE_HOST = "googleusercontent.com"
_MEDIATOR_GENERATED_IMAGE_HOSTS = {
    "lh3.google.com",
    "work.fife.usercontent.google.com",
}
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_MAGIC_MIMES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _generated_image_id(rcid: str, variant: int, image_index: int) -> str:
    """Reproduce Gemini Web's fallback IMAGE_ID hash for c8o8Fe."""
    value = f"{rcid}_{variant}_{image_index}".encode("utf-16-le")
    hash_value = 0
    for offset in range(0, len(value), 2):
        code_unit = value[offset] | (value[offset + 1] << 8)
        hash_value = ((hash_value << 5) - hash_value + code_unit) & 0xFFFFFFFF
        if hash_value >= 0x80000000:
            hash_value -= 0x100000000
    number = abs(hash_value)
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    encoded = "0" if number == 0 else ""
    while number:
        number, remainder = divmod(number, 36)
        encoded = alphabet[remainder] + encoded
    encoded = encoded[-12:]
    return (encoded * 8)[:16]


def _diagnostic_log(message: str) -> None:
    """Write image-download diagnostics through the service logger."""
    try:
        # Import lazily because gemini.py imports this module during startup.
        from .gemini import log
        log("image_download " + message)
    except Exception:
        return


def _url_debug(url: str) -> str:
    """Return URL metadata without exposing signed query values."""
    try:
        parsed = urlsplit(url)
        query_keys = sorted({part.split("=", 1)[0] for part in parsed.query.split("&") if part})
        return "host=%s path_sha256=%s path_len=%s query_keys=%s url_sha256=%s" % (
            parsed.hostname or "", hashlib.sha256((parsed.path or "/").encode("utf-8")).hexdigest()[:12],
            len(parsed.path or "/"), ",".join(query_keys) or "-",
            hashlib.sha256(url.encode("utf-8")).hexdigest()[:12],
        )
    except Exception:
        return "url_sha256=%s" % hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:12]


def _response_debug(response) -> str:
    headers = getattr(response, "headers", {})
    content_type = headers.get("Content-Type", "-")
    content_length = headers.get("Content-Length", "-")
    location = headers.get("Location")
    location_debug = _url_debug(urljoin("https://placeholder.invalid/", location)) if location else "-"
    return "status=%s content_type=%s content_length=%s location=%s server=%s" % (
        getattr(response, "status_code", "-"), content_type, content_length, location_debug,
        headers.get("Server", "-"),
    )


@dataclass(frozen=True)
class GeneratedImage:
    """Image metadata carried by a Gemini candidate rich-content block."""

    url: str
    alt: str = ""
    image_id: str = ""
    cid: str = ""
    rid: str = ""
    rcid: str = ""
    rpc_payload: Any = None


@dataclass
class GenerationResult:
    """Structured Gemini result without changing the legacy ``generate`` API."""

    text: str = ""
    images: list[GeneratedImage] = field(default_factory=list)
    raw: str = ""


def _nested(value: Any, indexes: list[int], default: Any = None) -> Any:
    for index in indexes:
        if not isinstance(value, list) or index >= len(value):
            return default
        value = value[index]
    return default if value is None else value


def _jspb_field(container: Any, index: int, default: Any = None) -> Any:
    """Read a JSPB positional field or its trailing sparse-field representation."""
    if not isinstance(container, list):
        return default
    value = container[index] if index < len(container) else None
    if value in (None, [], {}) or isinstance(value, dict):
        sparse = container[-1] if container and isinstance(container[-1], dict) else None
        value = sparse.get(str(index + 1)) if sparse else None
    return default if value in (None, [], {}) else value


def _wrb_payloads(raw: str):
    for line in raw.splitlines():
        if '"wrb.fr"' not in line:
            continue
        try:
            envelope = json.loads(line)
            payload = _nested(envelope, [0, 2])
            if isinstance(payload, str):
                yield json.loads(payload)
        except (json.JSONDecodeError, TypeError, IndexError):
            continue


def _find_rpc_entry(value: Any):
    """Find the rich image record used by Gemini's c8o8Fe RPC."""
    best = None
    best_score = -1

    def visit(node):
        nonlocal best, best_score
        if not isinstance(node, list):
            return
        context = _nested(node, [3], None)
        refs = _nested(node, [1], [])
        content_id = _nested(node, [8], "")
        if (isinstance(context, list) and _nested(context, [0], None) == 22
                and isinstance(refs, list) and refs
                and isinstance(_nested(refs, [0], ""), str)
                and _nested(refs, [0], "").startswith("http://googleusercontent.com/image_generation_content/")
                and isinstance(content_id, str) and content_id.startswith("im_")):
            score = 2 if isinstance(_nested(context, [1], None), str) and _nested(context, [1], "") else 1
            if score > best_score:
                best, best_score = node, score
        for child in node:
            visit(child)

    visit(value)
    return best


def extract_generation_result(raw: str, clean_text) -> GenerationResult:
    """Parse text and generated-image metadata from StreamGenerate response frames.

    Gemini places candidates at frame field ``[4]``.  A candidate's rich content
    is field ``[12]``; generated images are rich-content field 7 (or sparse key
    ``"8"``), whose entries live at ``[0]``.  Preview URL, alt text, and image
    ID are respectively ``[0][3][3]``, ``[0][3][2]``, and ``[1][0]``.
    """
    bard_error = re.search(r"BardErrorInfo\s*\[(\d+)\]", raw)
    if bard_error:
        raise RuntimeError("Gemini upstream rejected request: BardErrorInfo [%s]" % bard_error.group(1))

    text = ""
    images: list[GeneratedImage] = []
    seen = set()
    cid = rid = ""
    for frame in _wrb_payloads(raw):
        rpc_prompt = _nested(frame, [0, 0, 2, 0, 0], "")
        rpc_entry = _find_rpc_entry(frame)
        metadata = _nested(frame, [1], [])
        if isinstance(metadata, list):
            cid = _nested(metadata, [0], cid) or cid
            rid = _nested(metadata, [1], rid) or rid
        cid = _nested(frame, [0, 0, 0, 0], cid) or cid
        rid = _nested(frame, [0, 0, 0, 1], rid) or rid
        candidates = _nested(frame, [4], [])
        if not isinstance(candidates, list) or not candidates:
            # Newer Gemini Web responses place candidates under the hNvQHb-style
            # envelope used by the HAR capture: frame[0][0][3][0].
            candidates = _nested(frame, [0, 0, 3, 0], [])
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, list):
                continue
            candidate_text = _nested(candidate, [1, 0], "")
            if isinstance(candidate_text, str) and len(candidate_text) > len(text):
                text = candidate_text
            rcid = _nested(candidate, [0], "")
            rich = _nested(candidate, [12], [])
            generated_block = _jspb_field(rich, 7, [])
            entries = _nested(generated_block, [0], [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                nested_entries = [
                    item for item in entry if isinstance(item, list)
                    and isinstance(_nested(item, [3, 3], ""), str)
                    and _nested(item, [3, 3], "")
                ] if isinstance(entry, list) else []
                image_entries = nested_entries or [entry]
                content_refs = _nested(entry, [1], []) if nested_entries else []
                for image_index, image_entry in enumerate(image_entries):
                    url = _nested(image_entry, [0, 3, 3], "")
                    if not isinstance(url, str) or not url:
                        url = _nested(image_entry, [3, 3], "")
                    if not isinstance(url, str) or not url:
                        continue
                    variant = _nested(entry, [1, 1], 0)
                    if not isinstance(variant, int) or isinstance(variant, bool):
                        variant = 0
                    image_id = _nested(rpc_entry, [9], "") if isinstance(rpc_entry, list) else ""
                    if not isinstance(image_id, str) or not image_id:
                        image_id = (_generated_image_id(rcid, variant, image_index)
                                    if isinstance(rcid, str) and rcid else "")
                    if not isinstance(image_id, str) or not image_id:
                        image_id = _nested(entry, [1, 0], "")
                    if nested_entries and (not isinstance(image_id, str) or not image_id):
                        image_id = _nested(content_refs, [image_index], image_id)
                    if not isinstance(image_id, str) or not image_id:
                        image_id = f"http://googleusercontent.com/image_generation_content/{image_index}"
                    key = (url, image_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    alt = _nested(image_entry, [0, 3, 2], "")
                    if not isinstance(alt, str) or not alt:
                        alt = _nested(image_entry, [3, 2], "")
                    rpc_payload = None
                    if isinstance(rpc_entry, list):
                        image_metadata = _nested(rpc_entry, [0, image_index, 3], [])
                        if not isinstance(image_metadata, list) or not image_metadata:
                            image_metadata = _nested(rpc_entry, [0, 3], [])
                        signature = _nested(image_metadata, [5], "")
                        context = _nested(rpc_entry, [3], None)
                        placeholder_url = _nested(rpc_entry, [1, 0], "")
                        content_id = _nested(rpc_entry, [8], "")
                        prompt = _nested(context, [1], "") if isinstance(context, list) else ""
                        if not prompt:
                            prompt = rpc_prompt if isinstance(rpc_prompt, str) else ""
                        if (isinstance(signature, str) and signature
                                and isinstance(context, list)):
                            rpc_payload = {
                                "signature": signature,
                                "context": context,
                                "prompt": prompt,
                                "placeholder_url": (placeholder_url
                                                     if isinstance(placeholder_url, str)
                                                     else ""),
                                "content_id": content_id if isinstance(content_id, str) else "",
                            }
                    images.append(GeneratedImage(
                        url=url, alt=alt if isinstance(alt, str) else "",
                        image_id=image_id,
                        cid=cid if isinstance(cid, str) else "",
                        rid=rid if isinstance(rid, str) else "",
                        rcid=rcid if isinstance(rcid, str) else "",
                        rpc_payload=rpc_payload,
                    ))
    return GenerationResult(text=clean_text(text), images=images, raw=raw)


def _validated_https_url(url: str, allowed_hosts: set[str]) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_GENERATED_IMAGE_URL_TEXT_BYTES:
        raise ValueError("invalid generated image URL")
    if any(ch.isspace() for ch in url):
        raise ValueError("invalid generated image URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid generated image URL") from exc
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)):
        raise ValueError("generated image URL is not allowed")
    try:
        ipaddress.ip_address(host)
        raise ValueError("generated image URL is not allowed")
    except ValueError as exc:
        if str(exc) == "generated image URL is not allowed":
            raise
    if host not in allowed_hosts:
        raise ValueError("generated image URL is not allowed")
    return url


def validate_generated_image_url(url: str) -> str:
    """Permit only HTTPS googleusercontent image URLs, never private targets."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid generated image URL") from exc
    if host != _ALLOWED_GENERATED_IMAGE_HOST and not host.endswith("." + _ALLOWED_GENERATED_IMAGE_HOST):
        raise ValueError("generated image URL is not allowed")
    return _validated_https_url(url, {host})


def _validate_mediator_url(url: str) -> str:
    return _validated_https_url(url, _MEDIATOR_GENERATED_IMAGE_HOSTS)


def _with_web_image_variant(url: str) -> str:
    """Match Gemini Web's ``=d-I`` variant before fetching a raw /gg URL."""
    parsed = urlsplit(url)
    path = parsed.path
    if path.startswith(("/gg/", "/gg-dl/")):
        segment = path.rsplit("/", 1)[-1]
        if "=" not in segment:
            path += "=d-I"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _with_alr(url: str) -> str:
    """Match Gemini Web's image-loader flag on every generated-image hop."""
    url = _with_web_image_variant(url)
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "alr" for key, _value in query):
        query.append(("alr", "yes"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _classify_image_url(url: str) -> tuple[str, bool]:
    """Validate a hop and report whether it is a text mediator."""
    url = _with_alr(url)
    try:
        return validate_generated_image_url(url), False
    except ValueError:
        return _validate_mediator_url(url), True


def _image_mime(data: bytes) -> str:
    for magic, mime in _MAGIC_MIMES:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("generated image has unsupported or invalid bytes")


def _limits() -> tuple[int, int]:
    configured_bytes = CONFIG.get("generated_image_max_bytes", MAX_GENERATED_IMAGE_BYTES)
    configured_redirects = CONFIG.get("generated_image_max_redirects", MAX_GENERATED_IMAGE_REDIRECTS)
    max_bytes = (min(configured_bytes, MAX_GENERATED_IMAGE_BYTES)
                 if isinstance(configured_bytes, int) and not isinstance(configured_bytes, bool)
                 else MAX_GENERATED_IMAGE_BYTES)
    max_redirects = (max(0, min(configured_redirects, MAX_GENERATED_IMAGE_REDIRECTS))
                     if isinstance(configured_redirects, int) and not isinstance(configured_redirects, bool)
                     else MAX_GENERATED_IMAGE_REDIRECTS)
    return max_bytes, max_redirects


def _request_timeout(deadline=None):
    configured = max(0.001, float(CONFIG["request_timeout_sec"]))
    if deadline is None:
        return configured
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("generated image request budget exhausted")
    return min(configured, remaining)


def _request_args(stream: bool, url: str | None = None, *, deadline=None) -> dict:
    """Build the minimal media request with host-scoped login Cookie."""
    headers = {}
    if url:
        host = (urlsplit(url).hostname or "").lower()
        if host in _MEDIATOR_GENERATED_IMAGE_HOSTS:
            try:
                from .gemini import load_cookie
                cookie, _ = load_cookie()
            except Exception:
                cookie = ""
            if cookie:
                headers["Cookie"] = cookie
    args = {
        "headers": headers,
        "timeout": _request_timeout(deadline),
        "impersonate": "chrome",
        "default_headers": False,
        "allow_redirects": False,
        "stream": stream,
    }
    if CONFIG.get("proxy"):
        args["proxy"] = CONFIG["proxy"]
    return args


def _read_mediator_url(response) -> str:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/plain":
        raise ValueError("generated image mediator did not return text/plain")
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_GENERATED_IMAGE_URL_TEXT_BYTES:
                raise ValueError("generated image mediator response is too large")
        except ValueError as exc:
            if str(exc) == "generated image mediator response is too large":
                raise
            raise ValueError("invalid generated image mediator content length") from exc
    body = bytearray()
    for chunk in response.iter_content(chunk_size=1024):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > MAX_GENERATED_IMAGE_URL_TEXT_BYTES:
            raise ValueError("generated image mediator response is too large")
    try:
        url = bytes(body).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("generated image mediator did not return a URL") from exc
    if not url or any(ch.isspace() for ch in url):
        raise ValueError("generated image mediator did not return one URL")
    return url


def resolve_generated_image_url(url: str, *, deadline=None) -> str:
    """Resolve Gemini's bounded text mediators to a final image URL.

    Gemini Web adds ``alr=yes`` and follows up to two text/plain hops before
    fetching the final image.  The current flow uses ``lh3.google.com``;
    older responses can use the exact ``work.fife`` mediator host.
    """
    if not HAS_CURL_CFFI:
        raise RuntimeError("curl_cffi is required for generated image download")
    current = _with_alr(validate_generated_image_url(url))
    _diagnostic_log("resolve_start " + _url_debug(current))
    _, max_redirects = _limits()
    redirects = mediators = 0
    current_is_mediator = False

    while True:
        _diagnostic_log("resolve_request hop=%s mediator=%s %s" % (
            redirects, current_is_mediator, _url_debug(current)))
        response = curl_requests.get(
            current, **_request_args(stream=True, url=current, deadline=deadline))
        try:
            _diagnostic_log("resolve_response hop=%s %s %s" % (
                redirects, _url_debug(current), _response_debug(response)))
            if response.status_code in _REDIRECT_STATUS:
                if redirects >= max_redirects:
                    raise ValueError("generated image exceeded redirect limit")
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("generated image redirect has no location")
                current, current_is_mediator = _classify_image_url(urljoin(current, location))
                redirects += 1
                continue
            if response.status_code != 200:
                _diagnostic_log("resolve_failure reason=http_status %s %s" % (
                    _url_debug(current), _response_debug(response)))
                raise RuntimeError("generated image download failed: HTTP %s" % response.status_code)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type.startswith("image/"):
                if current_is_mediator:
                    raise ValueError("generated image mediator returned image bytes")
                return current
            next_url = _with_alr(_read_mediator_url(response))
            _diagnostic_log("resolve_mediator_body source=%s target=%s" % (
                _url_debug(current), _url_debug(next_url)))
            mediators += 1
            if mediators > MAX_GENERATED_IMAGE_MEDIATORS:
                raise ValueError("generated image exceeded mediator limit")
            current, current_is_mediator = _classify_image_url(next_url)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()


def download_generated_image(url: str, *, deadline=None) -> tuple[bytes, str]:
    """Download a resolved generated image with verified image bytes."""
    final_url = resolve_generated_image_url(url, deadline=deadline)
    _diagnostic_log("download_resolved source=%s final=%s" % (_url_debug(url), _url_debug(final_url)))
    max_bytes, max_redirects = _limits()
    current = final_url
    for hop in range(max_redirects + 1):
        _diagnostic_log("download_request hop=%s %s" % (hop, _url_debug(current)))
        response = curl_requests.get(
            current, **_request_args(stream=True, url=current, deadline=deadline))
        try:
            _diagnostic_log("download_response hop=%s %s %s" % (
                hop, _url_debug(current), _response_debug(response)))
            if response.status_code in _REDIRECT_STATUS:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("generated image redirect has no location")
                current = _with_alr(validate_generated_image_url(urljoin(current, location)))
                continue
            if response.status_code != 200:
                _diagnostic_log("download_failure reason=http_status %s %s" % (
                    _url_debug(current), _response_debug(response)))
                raise RuntimeError("generated image download failed: HTTP %s" % response.status_code)
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise ValueError("generated image exceeds 10 MiB")
                except ValueError as exc:
                    if str(exc) == "generated image exceeds 10 MiB":
                        raise
                    raise ValueError("invalid generated image content length") from exc
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("generated image exceeds 10 MiB")
                chunks.append(chunk)
            data = b"".join(chunks)
            mime = _image_mime(data)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != mime:
                raise ValueError("generated image content type does not match bytes")
            return data, mime
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
    raise ValueError("generated image exceeded redirect limit")
