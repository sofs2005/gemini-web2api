"""Configuration management."""
import json
import os

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 5,
    "retry_delay_max_sec": 30,
    "request_timeout_sec": 300,
    "stream_heartbeat_sec": 10,
    "agent_request_budget_sec": 300,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "google_user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    "image_rpc_debug_file": None,
    "google_browser_headers": {
      "x-browser-channel": "stable",
      "x-browser-copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
      "x-browser-validation": "YXjt/if5gRhK4M5qXvb22nUeBck=",
      "x-browser-year": "2026",
      "x-client-data": "CKmdygEIlaHLAQiGoM0B"
    },
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "log_file": None,
    "log_max_bytes": 2 * 1024 * 1024,
    "log_backup_count": 2,
    "cookie_file": None,
    "client_allowlist_file": None,
    "cookie_rotation_interval_sec": 0,
    "model_check_interval_sec": 86400,
    "model_snapshot_file": "./gemini-models.json",
    "proxy": None,
    "cache_db_path": "./gemini-web2api-cache.sqlite3",
    "cache_default_ttl_sec": 3600,
    "cache_max_ttl_sec": 604800,
    "auto_cache": True,
    "api_keys": [],
    "temporary_chats": False,
    "generated_image_max_bytes": 10 * 1024 * 1024,
    "generated_image_max_redirects": 3,
    "image_edit_crop": False,
    "image_edit_locator": False,
    "image_edit_attempts": 3,
    "image_edit_drift_limit": 40.0,
    "image_edit_min_change": 8.0,
    "image_edit_frame_gate": True,
    "image_edit_frame_tolerance": 0.02,
    "image_edit_context_crop": True,
    "image_edit_tone_match": True,
    "session_hashes": {},
}

CONFIG = dict(DEFAULT_CONFIG)

_ENV_KEYS = {
    "PORT": "port", "HOST": "host", "COOKIE_FILE": "cookie_file",
    "CLIENT_ALLOWLIST_FILE": "client_allowlist_file",
    "PROXY": "proxy", "GEMINI_BL": "gemini_bl", "XSRF_TOKEN": "xsrf_token",
    "AUTH_USER": "auth_user", "DEFAULT_MODEL": "default_model",
    "API_KEYS": "api_keys", "LOG_REQUESTS": "log_requests",
    "TEMPORARY_CHATS": "temporary_chats", "RETRY_ATTEMPTS": "retry_attempts",
    "RETRY_DELAY_SEC": "retry_delay_sec", "RETRY_DELAY_MAX_SEC": "retry_delay_max_sec",
    "REQUEST_TIMEOUT_SEC": "request_timeout_sec",
    "AGENT_REQUEST_BUDGET_SEC": "agent_request_budget_sec",
    "STREAM_HEARTBEAT_SEC": "stream_heartbeat_sec",
    "COOKIE_ROTATION_INTERVAL_SEC": "cookie_rotation_interval_sec",
    "MODEL_CHECK_INTERVAL_SEC": "model_check_interval_sec",
    "MODEL_SNAPSHOT_FILE": "model_snapshot_file",
}
_ENV_INT = {"port", "retry_attempts", "retry_delay_sec", "retry_delay_max_sec", "request_timeout_sec", "agent_request_budget_sec", "stream_heartbeat_sec", "cookie_rotation_interval_sec", "model_check_interval_sec"}
_ENV_BOOL = {"log_requests", "temporary_chats"}
_ENV_NULLABLE = {"cookie_file", "proxy", "xsrf_token", "auth_user"}


def load_env(path: str):
    """Load KEY=VALUE overrides from a .env-style file onto CONFIG."""
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            field = _ENV_KEYS.get(key.strip().upper())
            if not field:
                continue
            val = val.strip().strip('"').strip("'")
            if field in _ENV_INT:
                CONFIG[field] = int(val)
            elif field in _ENV_BOOL:
                CONFIG[field] = val.lower() in ("1", "true", "yes", "on")
            elif field == "api_keys":
                CONFIG[field] = [k.strip() for k in val.split(",") if k.strip()]
            elif field in _ENV_NULLABLE:
                CONFIG[field] = val or None
            elif val:
                CONFIG[field] = val


def load_config(path: str = None):
    """Load config from JSON file."""
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
