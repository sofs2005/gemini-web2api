"""Model definitions and mapping from Gemini frontend JS source."""

import re
import uuid

_SESSION_ID = "gemini-web2api-" + uuid.uuid4().hex
_SELECTION_KEY = "_model_selection"

# MODE_CATEGORY enum from 028-6eb337387583.js:
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.8-flash": {
        "mode": 1, "think": 4,
        "desc": "Latest all-around model (Gemini 3.8 Flash)",
    },
    "gemini-3.7-flash": {
        "mode": 1, "think": 4,
        "desc": "All-around model (Gemini 3.7 Flash)",
    },
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "All-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Alias for gemini-3.6-flash (backend upgraded)",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-3.1-pro-enhanced": {
        "mode": 3, "think": 4, "extra": {31: 2, 80: 3},
        "desc": "Pro with enhanced output (experimental)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}


def _slug(value):
    value = re.sub(r"^gemini[\s-]+", "", value.strip().lower())
    return re.sub(r"[^a-z0-9.]+", "-", value).strip("-")


# Upstream has swapped the meaning of two fields once already: at one point
# `display_name` held the model name ("3.8 Flash") and `description` held the
# marketing line ("All-around help"); later the marketing line moved into
# `display_name` ("Everyday help") and the model name into `description`
# ("4 Hydrogen"). Pick whichever candidate actually looks like a model name
# instead of trusting a fixed field.
_MODEL_LABEL = re.compile(r"^\s*\d+(?:\.\d+)?\s+[A-Za-z]")


def _model_label(row):
    for field in ("description", "display_name", "category"):
        value = row.get(field)
        if isinstance(value, str) and _MODEL_LABEL.match(value):
            return value.strip()
    return (row.get("display_name") or row.get("category") or row.get("id") or "").strip()


def _aliases(row, label):
    """Keep marketing copy, category and every historical label as aliases.

    Clients that already pinned `gemini-everyday-help` or `gemini-4-argon` keep
    working after upstream renames the same model id.
    """
    candidates = [row.get("display_name"), row.get("category"), label]
    candidates.extend(row.get("known_labels") or ())
    ordered = []
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        slug = _slug(value)
        if slug and slug not in ordered:
            ordered.append(slug)
    return ordered


def _dynamic_models():
    """Read the last valid snapshot for this session without contacting Google."""
    from .config import CONFIG
    if not CONFIG.get("cookie_file"):
        return None
    from .model_monitor import _read_state, _current_scope
    try:
        state = _read_state(CONFIG.get("model_snapshot_file") or "./gemini-models.json")
        snapshot = state.get("snapshot")
        if not snapshot or snapshot["scope"] != _current_scope():
            return None
        rows = snapshot["models"]
        names = ["gemini-" + (_slug(_model_label(row)) or _slug(row["id"])) for row in rows]
        models = {}
        for row, name in zip(rows, names):
            if (type(row.get("number")) is not int or row["number"] < 1
                    or type(row.get("capacity")) is not int or row["capacity"] < 1
                    or type(row.get("capacity_field")) is not int or row["capacity_field"] not in (12, 13)
                    or not isinstance(row.get("category"), str)
                    or not isinstance(row.get("description"), str)):
                return None
            if names.count(name) > 1:
                name += "-" + _slug(row["id"])
            header = [1, None, None, None, row["id"], None, None, 0, [4, 5, 6, 8], None, None]
            if row["capacity_field"] == 13:
                header.append(None)
            header.extend([row["capacity"], None, None, row["number"], 1, _SESSION_ID])
            label = _model_label(row)
            blurb = row["display_name"] if row["display_name"] and row["display_name"] != label else ""
            models[name] = {"mode": row["number"], "think": 4,
                            "desc": "%s (%s)" % (label, blurb) if blurb else (label or row["description"]),
                            "category": _slug(row["category"]), "upstream_id": row["id"],
                            "aliases": _aliases(row, label),
                            "extra": {_SELECTION_KEY: {"header": header, "scope": snapshot["scope"]}}}
        return models
    except (OSError, ValueError, KeyError, TypeError):
        return None


def get_models():
    """Advertise discovered models when available, otherwise the legacy list."""
    return _dynamic_models() or MODELS


def _find_dynamic_model(name, models):
    if name in models:
        return name
    matches = [key for key, cfg in models.items()
               if name == cfg["upstream_id"] or name == "gemini-" + cfg["category"]
               or name in ("gemini-" + alias for alias in cfg.get("aliases") or ())]
    if len(matches) == 1:
        return matches[0]
    # A major-version alias is safe only when it identifies one listed model.
    if re.fullmatch(r"gemini-?\d+", name):
        major = re.search(r"\d+$", name).group(0)
        matches = [key for key in models if re.match(r"gemini-" + major + r"(?:[.-]|$)", key)]
        if len(matches) == 1:
            return matches[0]
    legacy = MODELS.get(name)
    if legacy:
        categories = {1: ("flash",), 2: ("thinking", "flash"), 3: ("pro",),
                      4: ("auto", "flash"), 5: ("fast-dynamic-thinking", "flash"), 6: ("flash-lite",)}
        for category in categories[legacy["mode"]]:
            matches = [key for key, cfg in models.items() if cfg["category"] == category]
            if len(matches) == 1:
                return matches[0]
        # Upstream renames its families ("Flash" -> "4 Hydrogen"), so fall back
        # to the mode number, which still tracks MODE_CATEGORY one-to-one.
        matches = [key for key, cfg in models.items() if cfg["mode"] == legacy["mode"]]
        if len(matches) == 1:
            return matches[0]
    return None


def resolve_model(model_name: str, default: str = "gemini-3.6-flash"):
    """Resolve to (name, payload_model_number, think_mode, error, extra_fields).

    Current model names/IDs use complete selection headers from the saved account
    snapshot. Legacy aliases map to the available category. Unavailable Gemini
    names fail explicitly rather than silently routing to a different model.
    Other client identifiers retain the legacy fallback to the default.
    """
    if not isinstance(model_name, str):
        return None, None, None, "Model name must be a string", None
    think_override = None
    if "@think=" in model_name:
        model_name, think_str = model_name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError:
            return None, None, None, f"Invalid think level: {think_str}", None
    requested = model_name
    dynamic = _dynamic_models()
    if dynamic:
        target = _find_dynamic_model(model_name, dynamic)
        if target is None and not model_name.startswith("gemini") and not re.fullmatch(r"[a-f0-9]{16}", model_name):
            target = _find_dynamic_model(default, dynamic)
        if target is None:
            return None, None, None, f"Model is unavailable for the current session: {model_name}", None
        cfg = dynamic[target]
        legacy = MODELS.get(requested, {})
        think = think_override if think_override is not None else legacy.get("think", cfg["think"])
        extra = dict(legacy.get("extra") or {})
        extra.update(cfg["extra"])
        return target, cfg["mode"], think, None, extra
    cfg = MODELS.get(model_name)
    if not cfg:
        if model_name.startswith("gemini") or re.fullmatch(r"[a-f0-9]{16}", model_name):
            return None, None, None, f"Model is unavailable for the current session: {model_name}", None
        from .gemini import log
        log(f"Unknown model '{model_name}', falling back to '{default}'")
        model_name = default
        cfg = MODELS[default]
    think_mode = think_override if think_override is not None else cfg["think"]
    return model_name, cfg["mode"], think_mode, None, cfg.get("extra")
