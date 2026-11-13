"""Low-frequency, read-only discovery of the signed-in account's Gemini models."""
import errno
import hashlib
import json
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import CONFIG
from .gemini import load_cookie, _account_prefix, _build_headers, _get_ssl_ctx, _next_reqid, log

RPC_ID = "otAQ7b"
MIN_INTERVAL_SEC = 3600
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_check_lock = threading.Lock()
_monitor_thread = None
_stop_event = threading.Event()


class ModelResponseError(ValueError):
    """The RPC did not contain a complete authenticated model list."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must not turn one scheduled check into several requests.
        return None


def _at(data, index, default=None):
    return data[index] if len(data) > index else default


def _text(*values):
    return next((value for value in values if isinstance(value, str) and value), "")


def parse_model_response(raw):
    """Parse length-framed/XSSI-prefixed batchexecute data without saving it."""
    decoder = json.JSONDecoder()
    position = 0
    payloads = []
    while True:
        position = raw.find("[", position)
        if position < 0:
            break
        try:
            batch, end = decoder.raw_decode(raw, position)
        except ValueError:
            position += 1
            continue
        position = end
        pending = [batch]
        while pending:
            frame = pending.pop()
            if not isinstance(frame, list):
                continue
            if len(frame) > 2 and frame[:2] == ["wrb.fr", RPC_ID]:
                try:
                    payloads.append(json.loads(frame[2]))
                except (ValueError, TypeError):
                    raise ModelResponseError("Invalid model RPC payload") from None
            else:
                pending.extend(item for item in reversed(frame) if isinstance(item, list))
    if len(payloads) != 1:
        raise ModelResponseError("Expected one model RPC result")
    payload = payloads[0]
    if not isinstance(payload, list) or len(payload) <= 15:
        raise ModelResponseError("Model list absent")
    status = payload[14]
    if status is not None and (type(status) is not int or status != 1000):
        raise ModelResponseError("Account is not available")
    rows = payload[15]
    if not isinstance(rows, list) or not rows:
        # An empty/partial response must never erase a valid baseline.
        raise ModelResponseError("Model list is empty or invalid")
    tiers = _at(payload, 16, [])
    flags = _at(payload, 17, [])
    tiers = tiers if isinstance(tiers, list) else []
    flags = flags if isinstance(flags, list) else []
    if 21 in tiers or 22 in tiers:
        capacity, capacity_field = (1 if 21 in tiers else 2), 13
    else:
        capacity_field = 12
        capacity = (4 if 115 in flags else 3 if 16 in tiers or 106 in flags
                    else 2 if 8 in tiers or 19 in flags else 1)
    models = []
    ids = set()
    for row in rows:
        if not isinstance(row, list) or not row or not isinstance(row[0], str) or not row[0].strip():
            raise ModelResponseError("Invalid model entry")
        model_id = row[0]
        if model_id in ids:
            raise ModelResponseError("Duplicate model ID")
        ids.add(model_id)
        category = _text(_at(row, 1), _at(row, 10))
        name = _text(_at(row, 11), _at(row, 19), category, model_id)
        number = _at(row, 17)
        if type(number) is not int:
            number = _at(row, 9)
        models.append({
            "id": model_id, "category": category, "display_name": name,
            "description": _text(_at(row, 12), _at(row, 2)),
            "number": number if type(number) is int else 1,
            "capacity": capacity, "capacity_field": capacity_field,
        })
    return {"account_status_code": status, "models": sorted(models, key=lambda item: item["id"])}


def compare_models(previous, current):
    """Use stable IDs to distinguish renames from additions and removals."""
    old = {item["id"]: item for item in previous}
    new = {item["id"]: item for item in current}
    changes = {"added": [new[key] for key in sorted(new.keys() - old.keys())],
               "removed": [old[key] for key in sorted(old.keys() - new.keys())],
               "renamed": [], "updated": []}
    for key in sorted(old.keys() & new.keys()):
        before, after = _without_labels(old[key]), _without_labels(new[key])
        if before == after:
            continue
        change = {"id": key, "before": before, "after": after}
        kind = "renamed" if before["display_name"] != after["display_name"] else "updated"
        changes[kind].append(change)
    return changes


def _without_labels(row):
    return {key: value for key, value in row.items() if key != "known_labels"}


def _row_labels(row):
    labels = {value.strip() for value in (row.get("known_labels") or ())
              if isinstance(value, str) and value.strip()}
    for field in ("display_name", "description", "category"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            labels.add(value.strip())
    return labels


def carry_known_labels(snapshot, previous):
    """Accumulate every label an upstream model id has ever been advertised under.

    Google renames models in place (the same id went from "4 Argon" to
    "4 Carbon") and rolls the rename out gradually, so a client pinned to
    yesterday's name must keep resolving.
    """
    history = {}
    if previous and previous.get("scope") == snapshot.get("scope"):
        for row in previous.get("models") or []:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                history[row["id"]] = _row_labels(row)
    for row in snapshot.get("models") or []:
        row["known_labels"] = sorted(history.get(row.get("id"), set()) | _row_labels(row))
    return snapshot


def _scope(cookie, auth_user):
    """Hash stable session cookies; rotating TS/CC cookies do not reset history."""
    jar = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
    stable = [(key, jar[key]) for key in ("__Secure-1PSID", "__Secure-3PSID", "SAPISID") if jar.get(key)]
    if not stable:
        raise ModelResponseError("Authenticated session cookie required")
    identity = json.dumps([str(auth_user or "0"), stable], separators=(",", ":"))
    return hashlib.sha256(identity.encode()).hexdigest()


def _current_scope():
    cookie, _ = load_cookie()
    return _scope(cookie, CONFIG.get("auth_user"))


def fetch_account_models():
    """Make exactly one RPC request, without generation, redirects or retries."""
    load_cookie()  # Export metadata must be loaded before building the URL/body.
    # The exported XSRF token can predate the current web deployment. Fetch the
    # authenticated page token first; fall back to the export if that fails.
    try:
        from .multimodal import _cached_page_tokens
        page_tokens = _cached_page_tokens(max_age=0, timeout=15)
    except Exception:
        page_tokens = {}
    headers = _build_headers()
    headers["Accept-Encoding"] = "identity"  # urllib does not decode gzip/br/zstd.
    scope = _scope(headers.get("Cookie", ""), CONFIG.get("auth_user"))
    xsrf_token = page_tokens.get("at") or CONFIG.get("xsrf_token")
    if not xsrf_token:
        raise ModelResponseError("XSRF token required")
    prefix = _account_prefix()
    params = {"rpcids": RPC_ID, "hl": "en", "rt": "c",
              "_reqid": _next_reqid(),
              "source-path": prefix + "/app", "bl": CONFIG["gemini_bl"]}
    if page_tokens.get("f_sid"):
        params["f.sid"] = page_tokens["f_sid"]
    url = ("https://gemini.google.com" + prefix + "/_/BardChatUi/data/batchexecute?"
           + urllib.parse.urlencode(params))
    headers["x-goog-ext-525001261-jspb"] = "[1,null,null,null,null,null,null,null,[4,5,6,8],null,null,null,null,null,null,null]"
    headers["x-goog-ext-73010989-jspb"] = "[0]"
    body = urllib.parse.urlencode({
        "at": xsrf_token,
        "f.req": json.dumps([[[RPC_ID, "[]", None, "generic"]]]),
    }).encode()
    proxy = CONFIG.get("proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
        urllib.request.HTTPSHandler(context=_get_ssl_ctx()), NoRedirect(),
    )
    with opener.open(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=25) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelResponseError("Model response exceeds limit")
    result = parse_model_response(raw.decode("utf-8", errors="replace"))
    result["scope"] = scope
    return result


def _read_state(path):
    if not os.path.exists(path):
        return {"schema_version": 1, "last_attempt_at": 0, "snapshot": None, "last_change": None}
    with open(path, encoding="utf-8") as source:
        state = json.load(source)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("Invalid model state")
    attempt = state.get("last_attempt_at")
    if type(attempt) not in (int, float) or not math.isfinite(attempt) or attempt < 0:
        raise ValueError("Invalid model check time")
    snapshot = state.get("snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("scope"), str):
            raise ValueError("Invalid model snapshot")
        models = snapshot.get("models")
        if not isinstance(models, list) or not models:
            raise ValueError("Invalid model snapshot")
        ids = set()
        for model in models:
            if (not isinstance(model, dict) or not isinstance(model.get("id"), str)
                    or not model["id"] or model["id"] in ids
                    or not isinstance(model.get("display_name"), str)):
                raise ValueError("Invalid model snapshot")
            ids.add(model["id"])
    return state


def _write_state(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".gemini-models-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)  # mkstemp keeps the replacement private (0600).
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _state_lock(path):
    """Serialize checks across processes sharing a snapshot file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            yield False
        else:
            yield True
        # Closing the file releases the OS lock, even after a process crash.


def _utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_models_once(now=None):
    """Persist the attempt before contacting Google, including failed checks."""
    configured_interval = int(CONFIG.get("model_check_interval_sec") or 0)
    if configured_interval <= 0 or not CONFIG.get("cookie_file"):
        return "disabled"
    if not _check_lock.acquire(blocking=False):
        return "busy"
    try:
        interval = max(MIN_INTERVAL_SEC, configured_interval)
        now = time.time() if now is None else now
        path = CONFIG.get("model_snapshot_file") or "./gemini-models.json"
        with _state_lock(path) as acquired:
            return _perform_check(path, interval, now) if acquired else "busy"
    finally:
        _check_lock.release()


def _perform_check(path, interval, now):
    # Corrupt/unwritable state fails closed: do not send untracked requests.
    state = _read_state(path)
    if state["last_attempt_at"] and now < state["last_attempt_at"] + interval:
        return "not_due"
    state.update(last_attempt_at=now, last_status="checking")
    _write_state(path, state)
    try:
        result = fetch_account_models()
        if result["scope"] != _current_scope():
            raise ModelResponseError("Session changed during check")
    except Exception as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
            status = "http_" + str(error.code)
        else:
            status = "error_" + type(error).__name__
        state["last_status"] = status
        _write_state(path, state)
        log("model_check status=" + status, force=True)
        return status
    previous = state.get("snapshot")
    snapshot = carry_known_labels(dict(result, checked_at=_utc(now)), previous)
    if previous is None or previous["scope"] != snapshot["scope"]:
        status = "baseline"
        state["last_change"] = None
        message = "model_check status=baseline models=" + json.dumps(snapshot["models"], ensure_ascii=False)
    else:
        changes = compare_models(previous["models"], snapshot["models"])
        status = "changed" if any(changes.values()) else "unchanged"
        if status == "changed":
            state["last_change"] = {"detected_at": _utc(now), "changes": changes}
            message = "model_change " + json.dumps(state["last_change"], ensure_ascii=False)
        else:
            message = "model_check status=unchanged count=" + str(len(snapshot["models"]))
    state.update(snapshot=snapshot, last_status=status)
    _write_state(path, state)
    log(message, force=True)
    return status


def start_model_monitor():
    global _monitor_thread
    if int(CONFIG.get("model_check_interval_sec") or 0) <= 0 or not CONFIG.get("cookie_file"):
        return
    if _monitor_thread is not None and _monitor_thread.is_alive():
        return
    _stop_event.clear()

    def run():
        # Establish the initial baseline immediately; saved attempts keep
        # subsequent restarts on the existing schedule.
        while not _stop_event.is_set():
            try:
                check_models_once()
            except Exception as error:
                log("model_check status=state_error type=" + type(error).__name__, force=True)
            if _stop_event.wait(60):
                break

    _monitor_thread = threading.Thread(target=run, name="model-monitor", daemon=True)
    _monitor_thread.start()


def stop_model_monitor():
    _stop_event.set()
    if _monitor_thread is not None:
        _monitor_thread.join(timeout=1)
