"""HTTP server: OpenAI-compatible API endpoints."""
import ipaddress
import json
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
from .multimodal import detect_image_mime, fetch_image_bytes, upload_image, validate_edit_mask
from .image_ops import ImageOpsUnavailable, composite_crop_edit, prepare_crop_edit
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


def _generated_image_payload(image, response_format: str, transform=None):
    """Resolve an image, falling back to the preview URL if full-size delivery fails."""
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
            import base64
            image_bytes, mime = download_generated_image(source_url)
            if transform is not None:
                image_bytes, mime = transform(image_bytes, mime)
            encoded = base64.b64encode(image_bytes).decode("ascii")
            if response_format == "url":
                data = {"url": "data:%s;base64,%s" % (mime, encoded)}
            else:
                data = {"b64_json": encoded}
            return data, source_kind, source_url
        except Exception as exc:
            last_error = exc
            if index + 1 < len(candidates):
                log("image_generation_fallback from=%s to=%s error_type=%s" % (
                    source_kind, candidates[index + 1][0], type(exc).__name__))
    raise last_error


def _generated_image_output(prompt: str, response_format: str):
    result = generate_image_structured(prompt)
    if not result.images:
        raise RuntimeError("Gemini returned no generated image metadata")
    data, source_kind, source_url = _generated_image_payload(result.images[0], response_format)
    log("image_generation_result images=%s selected_full_size=%s response_format=%s source_host=%s source_path=%s" % (
        len(result.images), source_kind == "full_size", response_format,
        (urlsplit(source_url).hostname or "-"), (urlsplit(source_url).path or "/")))
    return result.text, data


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
            if len(data) > 10 * 1024 * 1024:
                raise RuntimeError("image exceeds 10 MiB size limit")
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


def _multipart_text(fields: dict, name: str, required: bool = False) -> str:
    parts = fields.get(name, [])
    if len(parts) != 1:
        if required:
            raise ValueError(f"{name} is required")
        raise ValueError(f"{name} must be provided at most once")
    part = parts[0]
    if part.get("filename") is not None:
        raise ValueError(f"{name} must be a text field")
    try:
        value = part["data"].decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if required and not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _multipart_file(fields: dict, name: str, required: bool = False):
    parts = fields.get(name, [])
    if not parts:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if len(parts) != 1:
        raise ValueError(f"{name} must be provided at most once")
    part = parts[0]
    data = part.get("data")
    if not isinstance(data, bytes) or not data:
        raise ValueError(f"{name} must be a non-empty image file")
    if len(data) > 10 * 1024 * 1024:
        raise ValueError(f"{name} exceeds 10 MiB size limit")
    filename = part.get("filename")
    if not isinstance(filename, str) or not filename:
        filename = "image.png" if name == "image" else "mask.png"
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1] or (
        "image.png" if name == "image" else "mask.png"
    )
    declared_mime = part.get("content_type") or "image/png"
    if ";" in declared_mime:
        declared_mime = declared_mime.split(";", 1)[0].strip()
    mime = detect_image_mime(data, declared_mime if declared_mime.startswith("image/") else "image/png")
    if not isinstance(mime, str) or not mime.startswith("image/"):
        raise ValueError(f"{name} must be an image file")
    return data, mime, filename


def _edit_prompt(prompt: str, has_mask: bool) -> str:
    if not has_mask:
        return prompt
    return (
        "You are performing a strict masked image edit.\n\n"
        "Image 1 is the source image. Image 2 is a pixel-aligned edit mask; "
        "it is not a style reference and only defines the editable region.\n\n"
        "Mask rules:\n"
        "- The transparent region of the mask (alpha = 0) is the ONLY editable area.\n"
        "- The opaque region of the mask (alpha = 255) must remain completely unchanged.\n"
        "- Treat partially transparent edge pixels as a natural blend between the edited "
        "and preserved areas.\n"
        "- Do not crop, resize, reframe, rotate, zoom, relight, recolor, or restyle the "
        "overall image.\n"
        "- Do not repaint, move, remove, or add anything outside the transparent region.\n"
        "- Preserve the original framing, camera angle, perspective, lighting, colors, "
        "and all opaque content exactly as they are.\n\n"
        "Placement rules:\n"
        "- Put every requested object or change entirely inside the transparent region.\n"
        "- Scale or adjust the requested subject as needed so it fits completely inside "
        "that region.\n"
        "- Do not place the requested subject outside the transparent region, even if it "
        "looks more natural there.\n"
        "- If the requested change cannot fit, keep it inside the transparent region as "
        "much as possible and leave the opaque region untouched.\n\n"
        "User instruction:\n"
        + prompt
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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            self.send_json({"error": {"message": "client IP is not allowed"}}, 403)
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
                self.send_json({"error": {"message": "invalid api key"}}, 401)
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
                    self.send_json({"error": {"message": "cache not found"}}, 404)
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
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        self._sse_started = False
        try:
            if not self._access_allowed():
                return
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
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
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    def do_DELETE(self):
        if not self.path.startswith("/v1/caches/"):
            self.send_json({"error": "not found"}, 404)
            return
        if not _client_allowed(self):
            self.send_json({"error": {"message": "client IP is not allowed"}}, 403)
            return
        if not self._authorized():
            self.send_json({"error": {"message": "invalid api key"}}, 401)
            return
        cache_id = self.path.rsplit("/", 1)[-1]
        if not CACHE.delete(cache_id):
            self.send_json({"error": {"message": "cache not found"}}, 404)
            return
        self.send_json({"id": cache_id, "deleted": True})

    def _handle_cache_create(self, body: bytes):
        req = self._parse_body(body)
        messages = req.get("messages") if req else None
        if not isinstance(messages, list) or not messages:
            self.send_json({"error": {"message": "messages must be a non-empty array"}}, 400)
            return
        ttl = req.get("ttl_seconds", CONFIG["cache_default_ttl_sec"])
        if not isinstance(ttl, int) or ttl < 1 or ttl > CONFIG["cache_max_ttl_sec"]:
            self.send_json({"error": {"message": "invalid ttl_seconds"}}, 400)
            return
        self.send_json({"object": "cache", **CACHE.create(messages, ttl)}, 201)

    # ─── /v1/images/edits ─────────────────────────────────────────────────────

    def _handle_image_edit(self, body: bytes):
        allowed_fields = {
            "prompt", "image", "mask", "model", "n", "response_format", "stream",
            "size", "quality", "style", "background", "moderation", "output_format",
            "output_compression", "input_fidelity", "user",
        }
        try:
            fields = _parse_multipart_form(body, self.headers.get("Content-Type", ""))
            unexpected = sorted(set(fields) - allowed_fields)
            if unexpected:
                raise ValueError("unexpected field: " + unexpected[0])
            prompt = _multipart_text(fields, "prompt", required=True).strip()
            image = _multipart_file(fields, "image", required=True)
            mask = _multipart_file(fields, "mask")
            if mask is not None:
                validate_edit_mask(image[0], mask[0])

            if "stream" in fields:
                raise ValueError("stream is not supported")
            if "n" in fields:
                n_value = _multipart_text(fields, "n").strip()
                try:
                    n = int(n_value)
                except ValueError as exc:
                    raise ValueError("n must be 1") from exc
                if n != 1:
                    raise ValueError("only n=1 is supported")
            response_format = "b64_json"
            if "response_format" in fields:
                response_format = _multipart_text(fields, "response_format").strip()
            if response_format not in ("b64_json", "url"):
                raise ValueError("response_format must be b64_json or url")
            if "model" in fields and not _multipart_text(fields, "model").strip():
                raise ValueError("model must not be empty")
            ignored_fields = sorted(
                set(fields).intersection(allowed_fields - {
                    "prompt", "image", "mask", "model", "n", "response_format", "stream"
                })
            )
        except ValueError as exc:
            self.send_json({"error": {"message": str(exc)}}, 400)
            return

        crop_plan = None
        if mask is not None and CONFIG.get("image_edit_crop", True):
            try:
                crop_plan = prepare_crop_edit(image[0], mask[0], image[1])
            except ImageOpsUnavailable:
                log("image_edit_crop_fallback reason=imagemagick_unavailable")
            except ValueError as exc:
                self.send_json({"error": {"message": str(exc)}}, 400)
                return
            except RuntimeError as exc:
                self.send_json({
                    "error": {"message": "image edit preparation failed: "
                                         + safe_error_message(exc, 500)}}, 502)
                return

        if ignored_fields:
            log("image_edit_compat ignored_fields=%s" % ",".join(ignored_fields))
        if crop_plan is not None:
            attachments = [
                (crop_plan.crop_image, "image/png", "crop.png"),
                (crop_plan.crop_mask, "image/png", "mask.png"),
            ]
            edit_prompt = _crop_edit_prompt(prompt, crop_plan.crop_size)
            transform = lambda edited_bytes, edited_mime: composite_crop_edit(
                image[0], image[1], edited_bytes, edited_mime,
                crop_plan.crop_mask, crop_plan.crop_box, crop_plan.crop_size,
            )
            log("image_edit_crop box=%s size=%s mask_pixels=%d" % (
                crop_plan.crop_box, crop_plan.crop_size,
                crop_plan.geometry.transparent_pixels,
            ))
        else:
            attachments = [image]
            if mask is not None:
                attachments.append(mask)
            edit_prompt = _edit_prompt(prompt, mask is not None)
            transform = None
        try:
            file_refs = _upload_images(attachments)
            result = generate_image_structured(edit_prompt, file_refs=file_refs)
            if not result.images:
                raise RuntimeError("Gemini returned no generated image metadata")
            data, source_kind, source_url = _generated_image_payload(
                result.images[0], response_format, transform=transform
            )
            log(
                "image_edit_result selected_full_size=%s response_format=%s source_host=%s source_path=%s"
                % (
                    source_kind == "full_size", response_format,
                    (urlsplit(source_url).hostname or "-"),
                    (urlsplit(source_url).path or "/"),
                )
            )
        except RuntimeError as exc:
            self.send_json({"error": {"message": f"upstream error: {safe_error_message(exc, 500)}"}}, 502)
            return
        except Exception as exc:
            self.send_json({"error": {"message": f"upstream error: {safe_error_message(exc, 500)}"}}, 502)
            return
        self.send_json({"created": int(time.time()), "data": [data]})

    # ─── /v1/images/generations ───────────────────────────────────────────────

    def _handle_image_generation(self, body: bytes):
        req = self._parse_body(body)
        if not isinstance(req, dict):
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        # OpenAI Images clients commonly send these options even when the backend
        # cannot control them. Accept them for compatibility and keep the current
        # Gemini generation behavior; the values are intentionally ignored.
        ignored_compat_fields = {
            "size", "quality", "style", "background", "moderation",
            "output_format", "output_compression", "user",
        }
        unsupported = [name for name in ("stream",) if name in req]
        unexpected = set(req) - {
            "prompt", "model", "n", "response_format", *ignored_compat_fields,
        }
        prompt = req.get("prompt")
        if (unsupported or unexpected or not isinstance(prompt, str) or not prompt.strip()):
            self.send_json({"error": {"message": "invalid image generation request"}}, 400)
            return
        ignored_fields = sorted(set(req).intersection(ignored_compat_fields))
        if ignored_fields:
            log("image_generation_compat ignored_fields=%s" % ",".join(ignored_fields))
        if "n" in req and (not isinstance(req["n"], int) or isinstance(req["n"], bool) or req["n"] != 1):
            self.send_json({"error": {"message": "only n=1 is supported"}}, 400)
            return
        response_format = req.get("response_format", "b64_json")
        if response_format not in ("b64_json", "url"):
            self.send_json({"error": {"message": "response_format must be b64_json or url"}}, 400)
            return
        model_value = req.get("model", CONFIG["default_model"])
        if not isinstance(model_value, str):
            self.send_json({"error": {"message": "invalid model"}}, 400)
            return
        _model_name, _model_id, _think_mode, err, _extra_fields = resolve_model(model_value)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return
        try:
            result = generate_image_structured(prompt)
            if not result.images:
                raise RuntimeError("Gemini returned no generated image metadata")
            data, _source_kind, _source_url = _generated_image_payload(result.images[0], response_format)
        except Exception as e:
            error = safe_error_message(e, 500)
            self.send_json({"error": {"message": f"upstream error: {error}"}}, 502)
            return
        self.send_json({"created": int(time.time()), "data": [data]})

    # ─── /v1/chat/completions ─────────────────────────────────────────────────

    def _handle_chat(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
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
            self.send_json({"error": {"message": "cache not found"}}, 404)
            return
        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        cached_tokens = _cached_tokens(cached["messages"], tools, tool_choice) if cache_id else 0
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
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
                self.send_json({"error": {"message": f"upstream error: {safe_error_message(e, 500)}"}}, 502)
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
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
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
                    error_event = {"error": {
                        "type": "upstream_stream_error",
                        "message": "upstream stream interrupted",
                        "request_id": cid,
                        "retryable": not bool(stream_text) and not isinstance(e, GeminiUpstreamError),
                    }}
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
            self.send_json({"error": {"message": f"upstream error: {error}", "request_id": cid,
                                     "retryable": not isinstance(e, GeminiUpstreamError)}},
                           status_code)
            return

        quality = _classify_response_quality(prompt, text, bool(tools) and tool_choice != "none")
        log(f"response_quality id={cid} category={quality} prompt_chars={len(prompt)} output_chars={len(text or '')}")
        if quality == "malformed_or_unparsed_tool_call":
            log(f"request_finish id={cid} endpoint=chat status=tool_parse_error")
            self.send_json({"error": {
                "type": "tool_call_parse_error",
                "message": "Gemini returned an incomplete or malformed tool_call",
                "request_id": cid,
                "retryable": True,
            }}, 502)
            return
        if quality == "temporary_refusal":
            log(f"request_finish id={cid} endpoint=chat status=temporary_refusal")
            self.send_json({"error": {
                "type": "upstream_temporary_refusal",
                "message": "Gemini returned a temporary refusal; retry the current task",
                "request_id": cid,
                "retryable": True,
            }}, 503)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
            try:
                validate_tool_calls(tool_calls, tools)
            except ValueError as e:
                self.send_json({"error": {"type": "tool_call_parse_error", "message": str(e),
                                          "request_id": cid, "retryable": True}}, 502)
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
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
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
            self.send_json({"error": {"message": "cache not found"}}, 404)
            return

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        cached_tokens = _cached_tokens(cached["messages"], tools, tool_choice) if cache_id else 0
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
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
            self.send_json({
                "error": {"message": "image generation with input images is not supported"}
            }, 400)
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
            self.send_json({"error": {"message": f"upstream error: {error}", "request_id": rid,
                                     "retryable": not isinstance(e, GeminiUpstreamError)}}, status_code)
            return

        quality = _classify_response_quality(prompt, text, bool(tools) and tool_choice != "none")
        log(f"response_quality id={rid} category={quality} prompt_chars={len(prompt)} output_chars={len(text or '')}")
        if quality == "malformed_or_unparsed_tool_call":
            log(f"request_finish id={rid} endpoint=responses status=tool_parse_error")
            self.send_json({"error": {
                "type": "tool_call_parse_error",
                "message": "Gemini returned an incomplete or malformed tool_call",
                "request_id": rid,
                "retryable": True,
            }}, 502)
            return
        if quality == "temporary_refusal":
            log(f"request_finish id={rid} endpoint=responses status=temporary_refusal")
            self.send_json({"error": {
                "type": "upstream_temporary_refusal",
                "message": "Gemini returned a temporary refusal; retry the current task",
                "request_id": rid,
                "retryable": True,
            }}, 503)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
            try:
                validate_tool_calls(tool_calls, tools)
            except ValueError as e:
                self.send_json({"error": {"type": "tool_call_parse_error", "message": str(e),
                                          "request_id": rid, "retryable": True}}, 502)
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
