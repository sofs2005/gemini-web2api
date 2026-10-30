"""Gemini StreamGenerate protocol implementation with httpx streaming."""
from __future__ import annotations
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import ssl
import os
import hashlib
import threading
import random
import secrets

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False

from .config import CONFIG
from .generated_image import GenerationResult, extract_generation_result
from .tools import has_tool_call_marker, valid_tool_call_candidate

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0, "path": None}
_httpx_client = None
_log_lock = threading.Lock()
_reqid_lock = threading.Lock()
_reqid = random.randint(1000, 9999)


def _next_reqid() -> int:
    """Match Gemini Web's four-digit base plus 100000 request sequence."""
    global _reqid
    with _reqid_lock:
        _reqid += 100000
        return _reqid


def _rotate_log_file(path: str, max_bytes: int, backup_count: int) -> None:
    if not path or max_bytes <= 0 or backup_count < 1:
        return
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
        for index in range(backup_count, 0, -1):
            source = f"{path}.{index - 1}" if index > 1 else path
            target = f"{path}.{index}"
            if os.path.exists(target):
                os.remove(target)
            if os.path.exists(source):
                os.replace(source, target)
    except OSError:
        pass


def log(msg: str, force: bool = False):
    if not force and not CONFIG["log_requests"]:
        return
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    path = CONFIG.get("log_file")
    if path:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with _log_lock:
                _rotate_log_file(
                    path,
                    int(CONFIG.get("log_max_bytes", 2 * 1024 * 1024)),
                    int(CONFIG.get("log_backup_count", 2)),
                )
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write(line)
            return
        except (OSError, ValueError):
            pass
    import sys
    sys.stderr.write(line)
    sys.stderr.flush()


def _write_image_rpc_diagnostic(record: dict) -> None:
    """Append bounded c8o8Fe diagnostics without credentials or signed URLs."""
    path = CONFIG.get("image_rpc_debug_file")
    if not path:
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except (OSError, TypeError, ValueError):
        return


def _hash_value(value) -> str:
    if isinstance(value, bytes):
        data = value
    else:
        data = str(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _image_rpc_url_metadata(url: str) -> dict:
    parsed = urllib.parse.urlsplit(url)
    return {
        "host": parsed.hostname or "",
        "path_len": len(parsed.path),
        "path_sha256": _hash_value(parsed.path)[:16],
        "url_len": len(url),
        "url_sha256": _hash_value(url)[:16],
        "query_keys": sorted({key for key, _value in urllib.parse.parse_qsl(parsed.query)}),
    }


def _build_image_rpc_diagnostic(image, payload: list, params: dict, body: str, headers: dict) -> dict:
    rpc_info = getattr(image, "rpc_payload", None)
    context = rpc_info.get("context") if isinstance(rpc_info, dict) else None
    signature = rpc_info.get("signature") if isinstance(rpc_info, dict) else ""
    header = json.loads(headers.get("x-goog-ext-525001261-jspb", "[]"))
    return {
        "event": "request",
        "payload_len": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        "payload_sha256": _hash_value(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))[:16],
        "form_len": len(body),
        "build": params.get("bl", ""),
        "source_path": params.get("source-path", ""),
        "reqid": params.get("_reqid"),
        "f_sid_sha256": _hash_value(params.get("f.sid", ""))[:16],
        "at_present": bool(body and "at=" in body),
        "placeholder": payload[0][1][0],
        "variant": payload[0][1][1],
        "artifact_id": payload[0][8],
        "image_id": payload[0][9],
        "rid": payload[1][0],
        "rcid": payload[1][1],
        "cid": payload[1][2],
        "context_len": len(context) if isinstance(context, list) else None,
        "context_sha256": _hash_value(json.dumps(context, ensure_ascii=False, separators=(",", ":")))[:16],
        "signature_sha256": _hash_value(signature)[:16],
        "model_header_len": len(header),
        "model_header_uuid": header[16] if len(header) > 16 else None,
        "model_header_timing": header[19] if len(header) > 19 else None,
        "x_client_data": headers.get("x-client-data", ""),
        "user_agent": headers.get("User-Agent", ""),
    }


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None and HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True)
    return _httpx_client


def _retry_delay(attempt: int) -> float:
    """Back off between upstream retries to avoid amplifying overload errors."""
    base = max(0.0, float(CONFIG.get("retry_delay_sec", 5)))
    cap = max(base, float(CONFIG.get("retry_delay_max_sec", 30)))
    return min(cap, base * (2 ** attempt)) + random.uniform(0, min(1.0, base / 4))


def safe_error_message(error: BaseException, limit: int = 300) -> str:
    """Return a bounded error message with credential-like values redacted."""
    message = str(error).replace("\n", " ").replace("\r", " ")
    message = re.sub(
        r"(?i)(cookie|token|api[_-]?key|authorization|secret|password|xsrf)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        message,
    )
    return message[:limit] or type(error).__name__


def _selection_log_fields(selection) -> str:
    """Describe dynamic model routing without logging the complete selection header."""
    if not isinstance(selection, dict):
        return "selection=legacy"
    header = selection.get("header")
    if not isinstance(header, list):
        return "selection=invalid"
    model_id = header[4] if len(header) > 4 and isinstance(header[4], str) else "unknown"
    capacity = header[11] if len(header) > 11 and isinstance(header[11], int) else "unknown"
    model_number = header[14] if len(header) > 14 and isinstance(header[14], int) else "unknown"
    return (
        f"selection=dynamic upstream_model_id={model_id} "
        f"selection_capacity={capacity} selection_number={model_number}"
    )

def load_cookie() -> tuple:
    """Load cookie from file with mtime-based caching."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        if cookie_file == _cookie_cache["path"] and mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
            return _cookie_cache["str"], _cookie_cache["sapisid"]
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
            for field in ("xsrf_token", "gemini_bl"):
                if data.get(field):
                    CONFIG[field] = data[field]
            if "auth_user" in data:
                CONFIG["auth_user"] = data["auth_user"]
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime, "path": cookie_file})
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return _cookie_cache["str"], _cookie_cache["sapisid"]


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


_IMAGE_MODEL_HEADER_KEY = "x-goog-ext-525001261-jspb"
_IMAGE_MODEL_FALLBACK = ("8c46e95b1a07cecc", "2", 6)


def build_model_header(model_id: str, capacity_tail: str | int, model_category: int) -> dict:
    """Build the Gemini Web model-selection headers for image generation."""
    return {
        _IMAGE_MODEL_HEADER_KEY: (
            f'[1,null,null,null,"{model_id}",null,null,0,[4,5,6,8],null,null,'
            f'{capacity_tail},null,null,{model_category}]'
        ),
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-73010990-jspb": "[0,0,0]",
    }


def _image_model_headers(page_tokens: dict, session_uuid: str = None) -> dict:
    """Use a discovered image route when available, with a bounded fallback."""
    discovered = page_tokens.get("image_model")
    if (isinstance(discovered, (tuple, list)) and len(discovered) == 3
            and re.fullmatch(r"[a-f0-9]{16}", str(discovered[0]))
            and str(discovered[1]).isdigit() and str(discovered[2]) == "6"):
        model_id, capacity_tail, category = discovered
    else:
        model_id, capacity_tail, category = _IMAGE_MODEL_FALLBACK
    headers = build_model_header(str(model_id), str(capacity_tail), int(category))
    model_header = json.loads(headers[_IMAGE_MODEL_HEADER_KEY])
    model_header.extend([1, session_uuid or str(uuid.uuid4()).upper()])
    headers[_IMAGE_MODEL_HEADER_KEY] = json.dumps(model_header)
    return headers


def _google_browser_headers() -> dict:
    """Return browser identity headers captured from the successful Gemini Web HAR."""
    defaults = {
        "x-browser-channel": "stable",
        "x-browser-copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
        "x-browser-validation": "YXjt/if5gRhK4M5qXvb22nUeBck=",
        "x-browser-year": "2026",
        "x-client-data": "CKmdygEIlaHLAQiGoM0B",
    }
    configured = CONFIG.get("google_browser_headers")
    if isinstance(configured, dict):
        defaults.update({key: value for key, value in configured.items()
                         if key in defaults and isinstance(value, str) and value})
    return defaults


def _google_user_agent() -> str:
    value = CONFIG.get("google_user_agent")
    return value if isinstance(value, str) and value else (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    )


def _build_headers(session_hash=None, request_uuid=None) -> dict:
    cookie_str, sapisid = load_cookie()
    account_prefix = _account_prefix()
    headers = {
        "Accept": "*/*",
        # urllib does not decode Content-Encoding; asking for br/zstd here makes
        # Google answer with Brotli, which parses to zero candidates and surfaces
        # as an empty assistant message with HTTP 200.
        "Accept-Encoding": "identity",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
        "Cache-Control": "no-cache",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Origin": "https://gemini.google.com",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Referer": f"https://gemini.google.com{account_prefix}/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Same-Domain": "1",
        "User-Agent": _google_user_agent(),
    }
    headers.update(_google_browser_headers())
    if request_uuid:
        headers["x-goog-ext-525005358-jspb"] = json.dumps([request_uuid, 1])
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    if isinstance(session_hash, dict):
        from .model_monitor import _scope
        if session_hash["scope"] != _scope(cookie_str, CONFIG.get("auth_user")):
            raise RuntimeError("Model selection belongs to a different session; refresh the model snapshot")
        headers["x-goog-ext-525001261-jspb"] = json.dumps(session_hash["header"])
        headers["x-goog-ext-73010989-jspb"] = "[0]"
        headers["x-goog-ext-73010990-jspb"] = "[0,0,0]"
    elif session_hash:
        # Compatibility with manually captured selection hashes.
        headers["x-goog-ext-525001261-jspb"] = json.dumps(
            [1, None, None, None, session_hash] + [None] * 13)
    return headers


def _session_hash(model_name: str):
    """Per-model session hash from config, captured from the web UI request headers."""
    return (CONFIG.get("session_hashes") or {}).get(model_name)


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _normalise_file_ref(file_ref) -> list:
    """Accept legacy references and preserve filenames in current attachments."""
    if isinstance(file_ref, (tuple, list)) and len(file_ref) == 2:
        ref, filename = file_ref
    else:
        ref, filename = file_ref, "image.png"
    if not isinstance(ref, str) or not ref.startswith("/"):
        raise ValueError("invalid uploaded file reference")
    if not isinstance(filename, str) or not filename:
        raise ValueError("invalid uploaded filename")
    return [[ref], filename]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                   extra_fields: dict = None, xsrf_token: str = None,
                   request_uuid: str = None) -> str:
    # Only file requests use the capture-derived 81-slot attachment protocol.
    inner = [None] * (81 if file_refs else 102)
    if file_refs:
        refs = [_normalise_file_ref(ref) for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = request_uuid or str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if file_refs:
        inner[80] = 1
    if extra_fields:
        for k, v in extra_fields.items():
            if k != "_model_selection":
                if k >= len(inner):
                    inner.extend([None] * (k + 1 - len(inner)))
                inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if xsrf_token or CONFIG.get("xsrf_token"):
        params["at"] = xsrf_token or CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _build_image_payload(prompt: str, request_uuid: str, xsrf_token: str = None,
                         file_refs: list = None) -> str:
    """Build the dedicated Gemini Web image-generation request body."""
    inner = [None] * 97
    refs = [_normalise_file_ref(ref) for ref in file_refs] if file_refs else None
    inner[0] = [prompt, 0, None, refs, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[3] = "!" + secrets.token_urlsafe(1903)
    inner[4] = uuid.uuid4().hex
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[0]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [1]
    inner[53] = 0
    inner[59] = request_uuid
    inner[61] = []
    inner[67] = 0
    inner[68] = 1
    inner[79] = 6
    inner[80] = 1
    inner[91] = 0
    inner[96] = 0
    params = {"f.req": json.dumps([None, json.dumps(inner)])}
    if xsrf_token or CONFIG.get("xsrf_token"):
        params["at"] = xsrf_token or CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url(session_id: str = None) -> str:
    reqid = _next_reqid()
    account_prefix = _account_prefix()
    params = {"bl": CONFIG["gemini_bl"], "hl": "en", "_reqid": reqid, "rt": "c"}
    if session_id:
        params["f.sid"] = session_id
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate?"
        f"{urllib.parse.urlencode(params)}"
    )


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def _extract_stream_deltas(line: str, emitted_raw_text: str) -> tuple:
    """Return the new cumulative text and incremental deltas from one line."""
    code = _bard_error_code(line)
    if code is not None:
        raise GeminiUpstreamError(f"Gemini upstream rejected request: BardErrorInfo [{code}]")

    deltas = []
    for text in _extract_texts_from_line(line):
        if text == emitted_raw_text or emitted_raw_text.startswith(text):
            continue
        if not text.startswith(emitted_raw_text):
            raise RuntimeError("Gemini stream content changed during retry")
        deltas.append(clean_text(text[len(emitted_raw_text):], strip=False))
        emitted_raw_text = text
    return emitted_raw_text, [delta for delta in deltas if delta]


def _iter_response_lines(response, on_chunk=None):
    """Yield complete lines, observing data before any line/text buffering."""
    buf = ""
    for chunk in response.iter_text():
        if chunk and on_chunk is not None:
            on_chunk()
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line
    if buf:
        yield buf


def _response_candidates(raw: str) -> list:
    candidates = []
    for line in raw.split("\n"):
        if '"wrb.fr"' in line:
            candidates.extend(_extract_texts_from_line(line))
    return candidates


def _valid_tool_call_candidate(text: str) -> bool:
    return valid_tool_call_candidate(text)


def _select_response_text(candidates: list) -> str:
    """Select a coherent final candidate, avoiding rewritten tool-call snapshots."""
    if not candidates:
        return ""
    tool_marked = [text for text in candidates if has_tool_call_marker(text)]
    if tool_marked:
        if not _valid_tool_call_candidate(tool_marked[-1]):
            raise RuntimeError("Gemini returned an incomplete or malformed tool_call")
        return tool_marked[-1]
    return max(candidates, key=len)


def _response_candidate_stats(raw: str):
    candidates = _response_candidates(raw)
    lengths = [len(text) for text in candidates]
    longest = max(candidates, key=len, default="")
    repeated_prefix = False
    if len(longest) >= 24:
        prefix = longest[:24]
        repeated_prefix = longest.count(prefix) > 1
    return len(candidates), sum(lengths), max(lengths, default=0), lengths[-12:], repeated_prefix


class GeminiUpstreamError(RuntimeError):
    """An application-level rejection that must not trigger generation retries."""


def _bard_error_code(raw: str):
    legacy = re.search(r'BardErrorInfo(?:"\s*,\s*\[|\s*\[)(\d+)\]', raw)
    if legacy:
        return int(legacy.group(1))
    if "BardErrorInfo" not in raw:
        return None

    def find(value):
        if isinstance(value, list):
            for index, item in enumerate(value):
                if (isinstance(item, str) and item.endswith(".BardErrorInfo")
                        and index + 1 < len(value)):
                    details = value[index + 1]
                    if isinstance(details, list) and details and isinstance(details[0], int):
                        return details[0]
                code = find(item)
                if code is not None:
                    return code
        elif isinstance(value, dict):
            for item in value.values():
                code = find(item)
                if code is not None:
                    return code
        elif isinstance(value, str) and value[:1] in ("[", "{"):
            try:
                return find(json.loads(value))
            except json.JSONDecodeError:
                pass
        return None

    for line in raw.splitlines():
        try:
            code = find(json.loads(line))
        except json.JSONDecodeError:
            continue
        if code is not None:
            return code
    return None


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    code = _bard_error_code(raw)
    if code is not None:
        raise GeminiUpstreamError(f"Gemini upstream rejected request: BardErrorInfo [{code}]")
    # ponytail: regex probe — backend echoes actual model name (e.g. "3.5 Flash-Lite");
    # may miss if backend renames formats, check raw response then.
    m = re.search(r'"(\d\.\d (?:Flash(?:-Lite)?|Thinking|Pro))', raw)
    if m:
        log(f"actual model from response: {m.group(1)}")
    candidates = _response_candidates(raw)
    selected = _select_response_text(candidates)
    return clean_text(selected)


def _generate_image_raw_with_curl(prompt: str, file_refs: list = None) -> str:
    """Send the dedicated GUI-equivalent image-generation request via Chrome TLS."""
    if not HAS_CURL_CFFI:
        raise RuntimeError("curl_cffi is required for Gemini image generation")

    from .multimodal import _cached_page_tokens
    page_tokens = _cached_page_tokens(max_age=0)
    request_uuid = str(uuid.uuid4()).upper()
    payload_kwargs = {"xsrf_token": page_tokens.get("at")}
    if file_refs:
        payload_kwargs["file_refs"] = file_refs
    body = _build_image_payload(prompt, request_uuid, **payload_kwargs)
    headers = _build_headers(request_uuid=request_uuid)
    headers.update(_image_model_headers(page_tokens, request_uuid))
    request_args = {
        "data": body,
        "headers": headers,
        "timeout": CONFIG["request_timeout_sec"],
        "impersonate": "chrome",
    }
    if CONFIG.get("proxy"):
        request_args["proxy"] = CONFIG["proxy"]

    last_err = None
    for attempt in range(max(1, CONFIG["retry_attempts"])):
        try:
            response = curl_requests.post(_get_url(page_tokens.get("f_sid")), **request_args)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_err = exc
            if attempt < max(1, CONFIG["retry_attempts"]) - 1:
                log(f"Image generation retry {attempt + 1}/{CONFIG['retry_attempts']}: {exc}")
                time.sleep(_retry_delay(attempt))
    raise last_err


def generate_image_structured(prompt: str, file_refs: list = None) -> GenerationResult:
    """Generate an image and return rich metadata, optionally using attachments."""
    raw = (_generate_image_raw_with_curl(prompt, file_refs=file_refs)
           if file_refs else _generate_image_raw_with_curl(prompt))
    return extract_generation_result(raw, clean_text)


def _batch_response_url(raw: str) -> str:
    """Extract the first full-size image URL from batchexecute's framed RPC body."""
    decoder = json.JSONDecoder()
    position = raw.find("\n") + 1 if raw.startswith(")]}'") else 0
    while position < len(raw):
        while position < len(raw) and raw[position].isspace():
            position += 1
        length = re.match(r"\d+\n", raw[position:])
        if not length:
            break
        position += length.end()
        try:
            envelope, position = decoder.raw_decode(raw, position)
        except json.JSONDecodeError:
            break
        pending = [envelope]
        while pending:
            record = pending.pop()
            if not isinstance(record, list):
                continue
            if (len(record) >= 3 and record[0] == "wrb.fr" and record[1] == "c8o8Fe"
                    and isinstance(record[2], str)):
                try:
                    payload = json.loads(record[2])
                    candidate = payload[0] if isinstance(payload, list) and payload else None
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(candidate, str):
                    return candidate
            pending.extend(item for item in record if isinstance(item, list))
    raise ValueError("full-size image RPC returned no URL")


def _build_full_size_rpc_headers(
    page_tokens: dict,
    session_uuid: str,
    elapsed_seconds: int = 0,
    elapsed_nanos: int = 0,
) -> dict:
    """Build the c8o8Fe-specific browser model header from the HAR shape."""
    build = CONFIG.get("gemini_bl", "")
    elapsed = elapsed_seconds if elapsed_seconds > 0 else None
    now_ns = time.time_ns()
    header = [
        1, None, None, None, None, None, None, 0,
        [4, 5, 6, 8, 16, 19, 20, 4, 5, 6, 8, 16, 19, 20],
        None, None, None, None, None, 6, 1, session_uuid,
        [build], None,
        [[elapsed, elapsed_nanos], [now_ns // 1_000_000_000, (now_ns // 1_000_000) % 1000 * 1_000_000]],
    ]
    return {
        "x-goog-ext-525001261-jspb": json.dumps(header),
        "x-goog-ext-73010989-jspb": "[0]",
    }


def _jspb_booleans(value):
    """Serialize JSPB booleans as numeric 0/1 values recursively."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, list):
        return [_jspb_booleans(item) for item in value]
    if isinstance(value, dict):
        return {key: _jspb_booleans(item) for key, item in value.items()}
    return value


def _build_full_size_rpc_payload(image) -> list:
    """Build c8o8Fe's rich generated-image payload when metadata is available."""
    rpc_info = getattr(image, "rpc_payload", None)
    if isinstance(rpc_info, dict):
        signature = rpc_info.get("signature")
        context = rpc_info.get("context")
        if isinstance(signature, str) and signature and isinstance(context, list):
            context = list(context)
            image_id = image.image_id
            if not isinstance(image_id, str) or not image_id:
                raise ValueError("generated image metadata has no image ID")
            request_context = [None] * 23
            if isinstance(context[0] if len(context) > 0 else None, int):
                request_context[0] = context[0]
            if isinstance(context[1] if len(context) > 1 else None, str):
                request_context[1] = context[1]
            source_flag = context[16] if len(context) > 16 else 0
            request_context[16] = 1 if source_flag is True or source_flag == 1 else 0
            if len(context) > 18:
                request_context[18] = context[18]
            if len(context) > 22:
                request_context[22] = _jspb_booleans(context[22])
            placeholder_url = rpc_info.get("placeholder_url")
            if not isinstance(placeholder_url, str) or not placeholder_url:
                placeholder_url = image.image_id
            return [
                [[None, None, None, [None, None, None, None, None, signature]],
                 [placeholder_url, 0], None, request_context, None, None, None, None,
                 rpc_info.get("content_id", ""), image_id],
                [image.rid, image.rcid, image.cid, None, image_id],
                1, 0, 1,
            ]
    return [
        [[None, None, None, [None, None, None, None, None, ""]], [image.image_id, 0],
         None, [19, ""], None, None, None, None, None, ""],
        [image.rid, image.rcid, image.cid, None, ""], 1, 0, 1,
    ]


def get_full_size_image(image) -> str | None:
    """Ask Gemini's c8o8Fe RPC for a full-size generated-image URL."""
    if not HAS_CURL_CFFI or not all(isinstance(value, str) and value for value in
                                    (image.cid, image.rid, image.rcid, image.image_id)):
        return None
    diagnostic = None
    try:
        from .multimodal import _cached_page_tokens
        started = time.monotonic()
        page_tokens = _cached_page_tokens(max_age=0)
        payload = _build_full_size_rpc_payload(image)
        rpc = ["c8o8Fe", json.dumps(payload), None, "generic"]
        session_uuid = str(uuid.uuid4()).upper()
        headers = _build_headers()
        elapsed = max(0.0, time.monotonic() - started)
        elapsed_seconds = int(elapsed)
        elapsed_nanos = int((elapsed - elapsed_seconds) * 1000) * 1_000_000
        headers.update(_build_full_size_rpc_headers(
            page_tokens, session_uuid, elapsed_seconds, elapsed_nanos
        ))
        conversation_id = image.cid[2:] if image.cid.startswith("c_") else image.cid
        params = {
            "rpcids": "c8o8Fe", "hl": "zh-CN", "_reqid": _next_reqid(),
            "rt": "c", "source-path": f"{_account_prefix()}/app/{conversation_id}",
            "bl": CONFIG["gemini_bl"],
        }
        if page_tokens.get("f_sid"):
            params["f.sid"] = page_tokens["f_sid"]
        body = urllib.parse.urlencode({
            "f.req": json.dumps([[rpc]]),
            "at": page_tokens.get("at") or CONFIG.get("xsrf_token") or "",
        })
        args = {"data": body, "headers": headers,
                "timeout": CONFIG["request_timeout_sec"], "impersonate": "chrome"}
        if CONFIG.get("proxy"):
            args["proxy"] = CONFIG["proxy"]
        url = (f"https://gemini.google.com{_account_prefix()}/_/BardChatUi/data/"
               f"batchexecute?{urllib.parse.urlencode(params)}")
        diagnostic = _build_image_rpc_diagnostic(image, payload, params, body, headers)
        _write_image_rpc_diagnostic(diagnostic)
        response = curl_requests.post(url, **args)
        try:
            raw = response.text
            diagnostic.update({
                "event": "response",
                "status": response.status_code,
                "response_len": len(raw),
                "response_sha256": _hash_value(raw)[:16],
            })
            response.raise_for_status()
            source_url = _batch_response_url(raw)
            diagnostic["source_url"] = _image_rpc_url_metadata(source_url)
            _write_image_rpc_diagnostic(diagnostic)
            return source_url
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
    except Exception as exc:
        if diagnostic is not None:
            diagnostic.update({"event": "error", "error": safe_error_message(exc)})
            _write_image_rpc_diagnostic(diagnostic)
        log(f"Full-size image RPC unavailable: {safe_error_message(exc)}")
        return None

def _generate_file_with_curl(prompt, model_id, think_mode, file_refs, extra_fields=None,
                             session_hash=None, request_id=None, *, budget_sec=None,
                             max_attempts=None, cancelled=None, on_first_response=None):
    """Port PR #80's Chrome attachment transport with request budgets and routing."""
    if not HAS_CURL_CFFI:
        raise RuntimeError("curl_cffi is required for Gemini image input")
    started = time.monotonic()
    deadline = started + budget_sec if budget_sec is not None else None
    configured_timeout = max(0.001, float(CONFIG["request_timeout_sec"]))

    def remaining_timeout():
        if cancelled and cancelled():
            raise ConnectionAbortedError("Client disconnected during image generation")
        remaining = configured_timeout if deadline is None else min(
            configured_timeout, deadline - time.monotonic()
        )
        if remaining <= 0:
            raise TimeoutError("Generation budget exhausted")
        return remaining

    cookie, _ = load_cookie()
    if not cookie:
        raise RuntimeError("A valid Gemini cookie is required for image input")
    request_uuid = str(uuid.uuid4()).upper()
    selection = (extra_fields or {}).get("_model_selection")
    headers = _build_headers(
        selection if selection is not None else session_hash, request_uuid=request_uuid
    )
    # Import lazily: multimodal uses the cookie and logging helpers above.
    from .multimodal import _cached_page_tokens
    tokens = _cached_page_tokens(max_age=0, timeout=remaining_timeout())
    if not tokens.get("at") or not tokens.get("f_sid"):
        raise RuntimeError("Gemini image session is unavailable; refresh the authentication export")
    body = _build_payload(
        prompt, model_id, think_mode, file_refs, extra_fields,
        xsrf_token=tokens["at"], request_uuid=request_uuid,
    )
    url = _get_url(tokens["f_sid"])
    trace_id = request_id or uuid.uuid4().hex[:10]
    attempts = max(1, max_attempts if max_attempts is not None else CONFIG["retry_attempts"])
    log(f"upstream_start transport=chrome_file id={trace_id} images={len(file_refs)} "
        f"max_attempts={attempts} {_selection_log_fields(selection)}")
    notified = False
    for attempt in range(attempts):
        response = None
        session = None
        try:
            timeout = remaining_timeout()
            session = curl_requests.Session()
            args = {"data": body, "headers": headers, "timeout": timeout,
                    "impersonate": "chrome", "stream": True, "allow_redirects": False}
            if CONFIG.get("proxy"):
                args["proxy"] = CONFIG["proxy"]
            response = session.post(url, **args)
            if response.status_code in (401, 403, 429):
                raise GeminiUpstreamError(f"Gemini image request rejected: HTTP {response.status_code}")
            response.raise_for_status()
            if response.status_code != 200:
                raise GeminiUpstreamError(f"Unexpected Gemini image response: HTTP {response.status_code}")
            parts = []
            for chunk in response.iter_content(chunk_size=65536):
                remaining_timeout()
                if not chunk:
                    continue
                if not notified and on_first_response is not None:
                    notified = True
                    on_first_response()
                parts.append(chunk)
            text = extract_response_text(b"".join(parts).decode("utf-8", errors="replace"))
            log(f"upstream_finish transport=chrome_file id={trace_id} attempt={attempt + 1} "
                f"duration_ms={int((time.monotonic() - started) * 1000)} extracted_chars={len(text)}")
            return text
        except Exception as error:
            log(f"upstream_error transport=chrome_file id={trace_id} attempt={attempt + 1} "
                f"error_type={type(error).__name__} error={safe_error_message(error)}")
            if (isinstance(error, (GeminiUpstreamError, TimeoutError, ConnectionAbortedError))
                    or getattr(error, "code", None) == 28 or attempt + 1 >= attempts):
                raise
            delay = _retry_delay(attempt)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise TimeoutError("Generation budget exhausted") from error
        finally:
            if response is not None:
                response.close()
            if session is not None:
                session.close()
        time.sleep(delay)


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, session_hash: str = None, request_id: str = None, *, budget_sec: float = None, max_attempts: int = None, cancelled=None, on_first_response=None) -> str:
    """Non-streaming generation with retry."""
    if file_refs:
        return _generate_file_with_curl(
            prompt, model_id, think_mode, file_refs, extra_fields, session_hash,
            request_id, budget_sec=budget_sec, max_attempts=max_attempts,
            cancelled=cancelled, on_first_response=on_first_response,
        )
    load_cookie()
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
    url = _get_url()
    selection = (extra_fields or {}).get("_model_selection")
    headers = _build_headers(selection if selection is not None else session_hash)
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    last_err = None
    trace_id = request_id or uuid.uuid4().hex[:10]
    started_at = time.monotonic()
    first_response_received = False
    deadline = started_at + budget_sec if budget_sec is not None else None
    attempts = max(1, max_attempts if max_attempts is not None else CONFIG["retry_attempts"])
    configured_timeout = max(0.001, float(CONFIG["request_timeout_sec"]))
    budget_label = f"{float(budget_sec):.3f}" if budget_sec is not None else "none"
    log(
        f"upstream_start transport=buffered id={trace_id} "
        f"request_bytes={len(body)} budget_sec={budget_label} "
        f"request_timeout_sec={configured_timeout:.3f} max_attempts={attempts} "
        f"{_selection_log_fields(selection)}"
    )
    for attempt in range(attempts):
        attempt_started = time.monotonic()
        phase = "preflight"
        response_status = "unknown"
        response_bytes = 0
        response_encoding = "identity"
        try:
            if cancelled and cancelled():
                raise ConnectionAbortedError("Client disconnected before generation")
            timeout = configured_timeout
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                timeout = min(timeout, remaining)
                if timeout <= 0:
                    raise TimeoutError("Generation budget exhausted")
            remaining_label = f"{remaining:.3f}" if remaining is not None else "none"
            log(
                f"upstream_attempt transport=buffered id={trace_id} "
                f"attempt={attempt + 1}/{attempts} phase=open timeout_sec={timeout:.3f} "
                f"budget_remaining_sec={remaining_label}"
            )
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            phase = "open"
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
            response_status = getattr(resp, "status", getattr(resp, "code", "unknown"))
            declared_encoding = resp.headers.get("Content-Encoding") if hasattr(resp, "headers") else None
            response_encoding = (
                declared_encoding.strip().lower()
                if isinstance(declared_encoding, str) and declared_encoding.strip()
                else "identity"
            )
            phase = "read"
            try:
                if deadline is None and on_first_response is None:
                    raw = resp.read().decode("utf-8", errors="replace")
                    response_bytes = len(raw.encode("utf-8"))
                else:
                    parts = []
                    while True:
                        if cancelled and cancelled():
                            raise ConnectionAbortedError("Client disconnected during generation")
                        if deadline is not None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("Generation budget exhausted")
                            # urllib has no total read deadline; bound each underlying read.
                            sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                            if sock is not None:
                                sock.settimeout(min(configured_timeout, remaining))
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        if on_first_response is not None and not first_response_received:
                            first_response_received = True
                            log(f"upstream_first_data transport=buffered id={trace_id} "
                                f"duration_ms={int((time.monotonic() - started_at) * 1000)}")
                            on_first_response()
                        parts.append(chunk)
                        response_bytes += len(chunk)
                    raw = b"".join(parts).decode("utf-8", errors="replace")
            finally:
                resp.close()
            if response_encoding not in ("identity", ""):
                raise GeminiUpstreamError(
                    f"Upstream answered with Content-Encoding={response_encoding}; "
                    "the buffered transport only reads identity bodies. "
                    "Check the Accept-Encoding request header."
                )
            phase = "parse"
            candidate_count, candidate_chars, longest_chars, tail_lengths, repeated_prefix = _response_candidate_stats(raw)
            text = extract_response_text(raw)
            log(
                f"upstream_finish transport=buffered id={trace_id} attempt={attempt + 1} "
                f"duration_ms={int((time.monotonic() - attempt_started) * 1000)} "
                f"http_status={response_status} response_bytes={response_bytes} "
                f"content_encoding={response_encoding} "
                f"frames={sum(1 for line in raw.split(chr(10)) if chr(34)+'wrb.fr'+chr(34) in line)} "
                f"candidates={candidate_count} candidate_chars={candidate_chars} "
                f"longest_chars={longest_chars} tail_lengths={tail_lengths} "
                f"repeated_prefix={repeated_prefix} extracted_chars={len(text)}"
            )
            return text
        except Exception as e:
            last_err = e
            remaining = deadline - time.monotonic() if deadline is not None else None
            remaining_label = f"{remaining:.3f}" if remaining is not None else "none"
            budget_exhausted = remaining is not None and remaining <= 0
            error_status = getattr(e, "code", response_status)
            if isinstance(e, TimeoutError):
                timeout_reason = "budget_exhausted" if budget_exhausted else "upstream_timeout"
            elif isinstance(e, ConnectionAbortedError):
                timeout_reason = "client_disconnected"
            else:
                timeout_reason = "none"
            log(
                f"upstream_error transport=buffered id={trace_id} attempt={attempt + 1}/{attempts} "
                f"duration_ms={int((time.monotonic() - attempt_started) * 1000)} phase={phase} "
                f"http_status={error_status} response_bytes={response_bytes} "
                f"budget_remaining_sec={remaining_label} error_type={type(e).__name__} "
                f"timeout_reason={timeout_reason} error={safe_error_message(e)}"
            )
            if isinstance(e, (TimeoutError, ConnectionAbortedError, GeminiUpstreamError)):
                raise
            if attempt < attempts - 1:
                delay = _retry_delay(attempt)
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise TimeoutError("Generation budget exhausted") from e
                log(
                    f"upstream_retry transport=buffered id={trace_id} "
                    f"after_attempt={attempt + 1} next_attempt={attempt + 2} "
                    f"delay_sec={delay:.3f} error_type={type(e).__name__}"
                )
                time.sleep(delay)
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, session_hash: str = None, request_id: str = None, *, on_first_response=None):
    """Stream text; attachment requests return one complete Chrome-backed result."""
    if file_refs or not HAS_HTTPX:
        kwargs = {"on_first_response": on_first_response} if on_first_response is not None else {}
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields, session_hash, request_id=request_id, **kwargs)
        if text:
            yield text
        return

    load_cookie()
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields)
    url = _get_url()
    selection = (extra_fields or {}).get("_model_selection")
    headers = _build_headers(selection if selection is not None else session_hash)
    client = _get_httpx_client()

    last_err = None
    request_tag = request_id or uuid.uuid4().hex[:10]
    log(
        f"upstream_start transport=stream tag={request_tag} "
        f"request_bytes={len(body.encode('utf-8'))} "
        f"request_timeout_sec={float(CONFIG['request_timeout_sec']):.3f} "
        f"max_attempts={max(1, CONFIG['retry_attempts'])} {_selection_log_fields(selection)}"
    )
    emitted_raw_text = ""
    started_at = time.monotonic()
    first_response_received = False

    def notify_first_response():
        nonlocal first_response_received
        if not first_response_received:
            first_response_received = True
            log(f"upstream_first_data transport=stream id={request_tag} "
                f"duration_ms={int((time.monotonic() - started_at) * 1000)}")
            if on_first_response is not None:
                on_first_response()

    for attempt in range(max(1, CONFIG["retry_attempts"])):
        attempt_started = time.monotonic()
        frame_count = 0
        response_bytes = 0
        text_frame_count = 0
        phase = "open"
        response_status = "unknown"
        log(
            f"upstream_attempt transport=stream tag={request_tag} "
            f"attempt={attempt + 1}/{max(1, CONFIG['retry_attempts'])} phase=open "
            f"timeout_sec={float(CONFIG['request_timeout_sec']):.3f}"
        )
        try:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                response_status = getattr(resp, "status_code", "unknown")
                resp.raise_for_status()
                phase = "read"
                for line in _iter_response_lines(resp, on_chunk=notify_first_response):
                    response_bytes += len(line.encode("utf-8"))
                    if '"wrb.fr"' in line:
                        frame_count += 1
                    previous_text = emitted_raw_text
                    emitted_raw_text, deltas = _extract_stream_deltas(line, emitted_raw_text)
                    if emitted_raw_text != previous_text:
                        text_frame_count += 1
                    for delta in deltas:
                        yield delta
            log(
                f"upstream_finish transport=stream tag={request_tag} attempt={attempt + 1} "
                f"duration_ms={int((time.monotonic() - attempt_started) * 1000)} "
                f"http_status={response_status} response_bytes={response_bytes} frames={frame_count} "
                f"text_frames={text_frame_count} emitted_chars={len(emitted_raw_text)}"
            )
            return
        except Exception as e:
            last_err = e
            log(
                f"upstream_error transport=stream tag={request_tag} attempt={attempt + 1}/"
                f"{max(1, CONFIG['retry_attempts'])} "
                f"duration_ms={int((time.monotonic() - attempt_started) * 1000)} phase={phase} "
                f"http_status={getattr(e, 'response', None).status_code if getattr(e, 'response', None) is not None else response_status} "
                f"response_bytes={response_bytes} frames={frame_count} "
                f"text_frames={text_frame_count} emitted_chars={len(emitted_raw_text)} "
                f"error_type={type(e).__name__} error={safe_error_message(e)}"
            )
            # Once bytes have been delivered, restarting would duplicate or
            # replace already visible model output. Let the API layer report a
            # terminal stream error instead.
            if emitted_raw_text or isinstance(e, GeminiUpstreamError):
                raise
            if attempt < max(1, CONFIG["retry_attempts"]) - 1:
                delay = _retry_delay(attempt)
                log(
                    f"upstream_retry transport=stream tag={request_tag} "
                    f"after_attempt={attempt + 1} next_attempt={attempt + 2} "
                    f"delay_sec={delay:.3f} error_type={type(e).__name__}"
                )
                time.sleep(delay)
    raise last_err
