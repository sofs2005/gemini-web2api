# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[中文文档](README_CN.md)

Convert Google Gemini's web interface into an OpenAI-compatible API. Zero cost, cross-platform, single file.

## Features

- **Optional API Keys**: no auth when `api_keys` is empty, OpenAI-style Bearer auth when configured
- **OpenAI Compatible**: Drop-in replacement for `/v1/chat/completions` and `/v1/models`
- **Tool Calling**: Full function calling support (OpenAI format)
- **Multiple Models**: Flash (3.6), Extended Thinking (20k+ char output), Pro, Auto, Lite
- **Thinking Depth**: Adjustable via `@think=N` suffix (0=deepest, 4=shallowest)
- **Web Search**: Built-in internet access (Gemini's native search)
- **Cross-Platform**: Pure Python, single optional dependency (`httpx` for streaming)
- **Streaming**: SSE streaming support via `httpx`
- **Codex CLI**: Responses API (`/v1/responses`) for OpenAI Codex integration
- **Gemini CLI**: Google native API (`/v1beta/models`) for Gemini CLI compatibility

## Quick Start

```bash
pip install httpx
python gemini_web2api.py
```

Server starts at `http://localhost:8081/v1`.

## Dual-Port Setup (Anonymous + Cookie)

You can run two instances side by side, each on its own port: one anonymous, one with a cookie.

Create two config files (see `.env.example`),

`.env.anon` (anonymous):

```
PORT=8081
```

`.env.cookie` (with cookie):

```
PORT=8082
COOKIE_FILE=cookie.txt
```

Start both ports with one command:

```bash
python start_all.py
```

| Port | Config | Routing behavior |
|------|--------|------------------|
| 8081 | `.env.anon` | Anonymous, all models route to Flash-Lite |
| 8082 | `.env.cookie` | Cookie auth, model categories take effect |

Point clients at 8081 for anonymous, 8082 for authenticated — the two don't interfere. The startup banner prints `Env file:` and `Cookie: yes/none (anonymous)` so you can tell instances apart.

Or start them separately:

```bash
python -m gemini_web2api --env-file .env.anon
python -m gemini_web2api --env-file .env.cookie
```

> Note: if the file `COOKIE_FILE` points to does not exist, the instance silently runs in anonymous mode without erroring. See Cookie configuration below.

## Client Configuration

### Cherry Studio / ChatBox / any OpenAI client

| Field | Value |
|-------|-------|
| Base URL | `http://localhost:8081/v1` |
| API Key | any `api_keys` value from `config.json`; anything if not configured |
| Model | `gemini-3.5-flash-thinking` |

### curl

#### bash / macOS / Linux

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"Hello!"}]}'
```

#### PowerShell (Windows)

```powershell
curl.exe --% http://127.0.0.1:8081/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer sk-your-key" -d "{\"model\":\"gemini-3.5-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}"
```

> Note: On Windows PowerShell, use `curl.exe` and `--%` so PowerShell does not reinterpret JSON quoting or curl options.

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "Explain quantum computing"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

Supports Google native API endpoints:
- `GET /v1beta/models` — list models
- `POST /v1beta/models/{model}:generateContent` — non-streaming
- `POST /v1beta/models/{model}:streamGenerateContent` — streaming (SSE)

## Available Models

| Model | Description | Output |
|-------|-------------|--------|
| `gemini-3.8-flash` | All-around model (latest) | ~12k chars |
| `gemini-3.6-flash` | All-around model | ~12k chars |
| `gemini-3.5-flash` | Alias for gemini-3.6-flash | ~12k chars |
| `gemini-3.5-flash-thinking` | Extended thinking, longest output | **~20k chars** |
| `gemini-3.5-flash-thinking-lite` | Adaptive thinking depth | ~15k chars |
| `gemini-3.1-pro` | Advanced math & code (needs cookie) | ~12k chars |
| `gemini-auto` | Auto model selection | varies |
| `gemini-flash-lite` | Fastest answers, lightweight | ~10k chars |

### Thinking Depth

Append `@think=N` to any model name:

```
gemini-3.5-flash-thinking@think=0   # deepest (default)
gemini-3.5-flash-thinking@think=2   # medium
gemini-3.5-flash-thinking@think=4   # shallowest
```

## Cookie Configuration (Detailed)

### Why a cookie is needed

Model selection sends a "model category" to the Gemini backend via the `[79]` field of the request payload, but **for anonymous requests the backend ignores the category and routes every model to Flash-Lite** (verified by testing). With a cookie, the category is honored:

- `gemini-3.8-flash` / `gemini-3.6-flash` (FAST category) → routes to whatever the account's FAST tier currently serves
- `gemini-3.1-pro` (PRO category) → free Google accounts silently fall back to Flash; **Gemini Advanced (paid subscription)** is required for real Pro routing
- There is no official unauthenticated API: the free Gemini API tier requires a Google account + AI Studio key; anonymous access is only the web chat's basic tier

### Method 1: bundled browser extension (recommended)

The extension exports cookie + SAPISID + XSRF token + gemini_bl in one go:

1. Open `chrome://extensions` in Chrome → enable **Developer mode** → **Load unpacked** → select the `gemini-cookie-sync-extension` directory from this repo
2. Open [gemini.google.com/app](https://gemini.google.com/app), sign in to your Google account, refresh the page
3. Click the extension icon → **Inspect session**, and confirm it shows:

   ```
   XSRF / SNlM0e: present
   gemini_bl / cfb2h: present
   ```

4. Click **Export gemini-auth.json** — you get a complete auth file with cookie, `sapisid`, `xsrf_token`, `gemini_bl`, and `auth_user`
5. Set `"cookie_file": "gemini-auth.json"` in `config.json`

> The `bl` version of gemini.google.com changes with deployments; the exported `gemini_bl` is the current value and more reliable than hand-filling.

### Method 2: manual extraction from DevTools

1. Open [gemini.google.com](https://gemini.google.com), sign in, press **F12** to open DevTools
2. **Application** tab → **Cookies** in the sidebar → `https://gemini.google.com`
3. Copy these cookies:

   | Cookie | Purpose |
   |--------|---------|
   | `SID` / `HSID` / `SSID` | Google sign-in state |
   | `APISID` / `SAPISID` | API auth (SAPISID is also used for sapisidhash) |
   | `__Secure-1PSID` | Session credential |

4. Create `cookie.txt` in the project root, JSON format:

```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx", "sapisid": "your SAPISID value"}
```

Or the plain single-line format:

```
SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx
```

Start with:

```bash
python gemini_web2api.py --cookie-file cookie.txt
```

### Authenticated account path and XSRF token

If the signed-in Gemini page URL contains an account index, such as:

```
https://gemini.google.com/u/1/app/...
```

set `auth_user` to that index. Authenticated web requests may also require the page XSRF token. In the rendered Gemini page source, this token is exposed as `SNlM0e`; pass it as `xsrf_token` in `config.json`. The server sends it as the `at` form field.

Example:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

If authenticated requests return HTTP 400 with an `xsrf` error, refresh Gemini Web, update `xsrf_token`, and make sure `auth_user` matches the `/u/<index>/` part of the browser URL.

Pro routing requires **Gemini Advanced** (paid subscription). A free Google account cookie will authenticate but silently fall back to Flash.

### Verify the routing takes effect

After starting the cookie instance, send a request and check the server log:

```bash
curl http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}]}'
```

The server log prints the model name the backend actually routed to:

```
[xx:xx:xx] actual model from response: 3.X Flash
```

That is the model really served by the FAST tier. If it still shows `3.5 Flash-Lite`, check the cookie file path and whether the session is still valid.

## Configuration

Create `config.json` in the same directory:

```json
{
  "port": 8081,
  "host": "0.0.0.0",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "image_request_budget_sec": 900,
  "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
  "auth_user": null,
  "xsrf_token": null,
  "api_keys": ["sk-your-key"],
  "cookie_file": null,
  "proxy": null,
  "log_requests": true,
  "temporary_chats": false
}
```

Set `temporary_chats` to `true` to use Gemini Web temporary chats instead of
persisting conversations to the account history.

When `api_keys` is `[]`, authentication is disabled. When one or more keys are set, `/v1/*` endpoints require `Authorization: Bearer <key>` or `x-api-key: <key>`.

### Model change monitoring

The modular server (`python -m gemini_web2api`) can periodically read the signed-in
account's model list. Add these settings to the cookie instance's configuration:

```json
{
  "model_check_interval_sec": 86400,
  "model_snapshot_file": "./gemini-models.json",
  "log_file": "logs/service.log"
}
```

With an authentication export configured, the default interval is 86400 seconds
(one day); set it to `0` to disable monitoring. On the first start, with no saved
attempt, it immediately reads the model list and uses that attempt as the start
of the daily cycle. Later starts respect the saved schedule and do not force an
extra request. Each due check makes one `otAQ7b` model-list request, sends no
generation requests, follows no redirects, and does not retry within a check.
The minimum interval is 3600 seconds. Failed checks also respect the interval,
and the last attempt survives service restarts. The configured proxy is used;
without one, this check connects directly.

A current authentication export with a session cookie and XSRF token is required.
The first successful read establishes a baseline. Subsequent reads compare stable
model IDs and record additions, removals, renames, and metadata changes. Failed,
empty, partial, or unauthenticated responses retain the last valid snapshot. A
new account/session establishes a new baseline; routine TS/CC cookie rotation
preserves it. The modular `/v1/models` and `/v1beta/models` endpoints publish the
current snapshot's model names, such as `gemini-4-argon`. Generation uses the
snapshot's model ID, capacity and model number to build the complete selection
header. Reading the list or resolving a model only reads local state; it makes no
additional model-list requests. Snapshot changes take effect without a restart.
Legacy aliases map to the available model in the same category. Unavailable
Gemini names return an error instead of silently selecting another model.
After an update, refresh the channel's model list in downstream services such as
New API. If no valid snapshot for the current session exists, the legacy list is
used and undiscovered Gemini names are rejected.

The snapshot contains `snapshot.models`, `snapshot.checked_at`, `last_attempt_at`,
`last_status`, and the latest change event in `last_change`. It stores a hashed
session scope, never cookies, tokens, or raw RPC responses. On Unix, snapshot and
lock files are private (`0600`); writes are atomic. Model events are logged even
when `log_requests` is false, using `model_check` and `model_change` entries.
A corrupt or unwritable state file stops checks until repaired. Multiple processes
sharing the same snapshot file serialize checks with an OS lock; use a different
snapshot path for each account instance. Keep custom snapshot paths out of Git.

Equivalent `.env` keys are `MODEL_CHECK_INTERVAL_SEC` and `MODEL_SNAPSHOT_FILE`.
The standalone Python script and Cloudflare Worker do not start this monitor.

### Context Cache

The proxy provides local SQLite-backed context caching. Create a cache with
`POST /v1/caches`, then pass its `cache_id` in Chat Completions or Responses
requests. With `auto_cache` enabled, stable system context and prior turns in
multi-turn requests are cached automatically. Cache usage is reported as the
estimated `usage.prompt_tokens_details.cached_tokens` field; this is proxy-side
accounting and is not an upstream Gemini billing value.

## Docker

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json gemini-web2api
```

Or use Docker Compose:

```bash
cp config.example.json config.json
docker compose up -d
```

To mount a cookie file:

```bash
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json -v ./cookie.txt:/app/cookie.txt gemini-web2api
```

Set `"cookie_file": "/app/cookie.txt"` in `config.json`.

> **Note**: If you get empty responses (`content: null`) with Docker's default bridge network, switch to host networking: `docker run --network host ...` or add `network_mode: host` in your compose file. This is caused by Gemini's upstream rejecting requests from certain Docker NAT IP ranges.

## Proxy

If you cannot access `gemini.google.com` directly (connection timeout), configure a proxy:

**Method 1: CLI argument**
```bash
python gemini_web2api.py --proxy http://127.0.0.1:7890
```

**Method 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**Method 3: Environment variable** (auto-detected)
```bash
export HTTPS_PROXY=http://127.0.0.1:7890
python gemini_web2api.py
```

Works with Clash, V2Ray, Shadowsocks, or any HTTP proxy.

## Tool Calling

```python
resp = client.chat.completions.create(
    model="gemini-3.5-flash",
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
        }
    }]
)
```

## Image Input

The modular server (`python -m gemini_web2api`, including Docker images) supports
image input through Chat Completions, Responses, and Google `inlineData`. It requires
a valid authenticated Gemini cookie and `curl_cffi` (installed by `requirements.txt`
on supported platforms). Supply public HTTPS image URLs or base64 data URLs, with a
10 MiB limit per image. The legacy single-file entry point and Cloudflare Worker
have not received this transport fix.

The attachment format, fresh page session tokens, and Chrome transport are adapted
from [upstream PR #80](https://github.com/Sophomoresty/gemini-web2api/pull/80).
Image requests with `stream=true` return one complete content chunk; SSE heartbeats
remain active while waiting. Remote image downloads use direct connections with
validated DNS addresses pinned, bypassing proxies. Gemini page, upload, and generation
requests still honor the configured proxy.

See the [isolated image test deployment](deploy/image-test/README.md) for bounded validation.

```python
resp = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        ]
    }]
)
```

## Image Output

The modular server supports `POST /v1/images/generations` with `prompt`, `model`
(required), `n` (1-10), and `response_format` set to `b64_json`. The image route uses Gemini Web's dedicated
browser request shape. `size` is validated and translated into an explicit aspect-ratio
instruction; when ImageMagick is available the returned image is also resized/cropped to
the requested dimensions. The response reports the actual `size` when it can be
determined, including when `size` is omitted or `auto`, and the actual `output_format`.
If ImageMagick is unavailable, the response reports the real returned dimensions rather
than echoing the requested size. For `n > 1`, the response-level `size` is emitted only
when every output reports the same actual size; mixed sizes are omitted. `n` is executed
as n independent upstream generations, so latency and Gemini quota consumption scale
roughly linearly; there is no seed or batch mode, so visual consistency across outputs is
not guaranteed. `image_request_budget_sec` (default 900, set `0` to disable) bounds the
whole Images request, including per-image retries and generated-image downloads. Exhaustion
returns HTTP 504 with `type=server_error, code=request_timeout`. Responses include a
proxy-side `usage` estimate: text tokens
are `ceil(UTF-8 bytes / 4)`, and image tokens are
`ceil(width/32) * ceil(height/32)` per input attachment or output image. These are
deterministic estimates, not OpenAI or Gemini tokenizer counts; do not use them for
billing.
`model` is required for generations and multipart edits. OpenAI image model names
(`gpt-image-*`, `chatgpt-image-latest`, and legacy `dall-e-*`) are accepted and mapped to
the same Gemini image pipeline; Gemini model names and 16-hex upstream IDs are also
accepted for compatibility but do not change the image backend. Any other name returns
`model_not_found`. JSON edits default an omitted model to `gpt-image-2.5-sunburst`.
Other OpenAI Images options such as `quality`, `style`, `moderation`,
`input_fidelity`, and `safety_identifier` are accepted and validated for client
compatibility, but the Gemini backend does not apply them. `background=opaque` and
`background=auto` are accepted as no-ops; `background=transparent` is rejected with
`unsupported_value` because the service cannot guarantee an alpha channel. `output_compression` is applied when the output is
encoded as JPEG or WebP through ImageMagick. `stream` is not supported and returns a standard OpenAI error with `param: "stream"`.
`partial_images` is accepted and range-checked (0-3) for compatibility, but has no
effect because the backend only returns a final image. All OpenAI-compatible `/v1`
endpoints return the `ErrorResponse` shape (`type`, `message`, `param`, `code`), and every
response includes `x-request-id`. Error types follow the HTTP status: 400/422
`invalid_request_error`, 401 `authentication_error`, 403 `permission_error`, 404
`invalid_request_error`, 429 `rate_limit_error`, and 5xx `server_error`. Proxy-specific causes
such as `tool_call_parse_error` and `upstream_temporary_refusal` are reported in `code`;
`retryable` remains an extra field. `/v1beta/*` keeps the Google-style error object for
Gemini CLI compatibility.

The modular server also supports `POST /v1/images/edits` with OpenAI's multipart form or
JSON body. In multipart mode `image`, `prompt`, and `model` are required, and `mask` is optional.
Exactly one `image` is supported; multi-image edits return a standard
`unsupported_value` error. JSON edits accept a single-element
`images: [{"image_url": ...}]` with an HTTPS or base64 data URL. `file_id` references
are rejected because the service does not implement the OpenAI Files API. `mask` must be
a PNG with an alpha channel and the same dimensions as the source image. With a mask, source dimensions are read from PNG, JPEG, or WebP inputs. Transparent mask areas are described as the editable region, while Gemini is asked
to preserve opaque areas. Gemini Web does not expose a separate mask parameter, so the
service sends the mask as an attachment and describes this alpha contract in the prompt.
Multipart images are limited to 50 MiB each and masks to 4 MiB; JSON HTTPS downloads
remain capped at 10 MiB by the remote fetch guard. Prompts are limited to 32000 characters. `response_format` supports `b64_json` only. `url` is rejected with a standard
`unsupported_value` error because this service does not host generated images. Decode
`b64_json` and store or serve the bytes in your own application if a public URL is needed.
`n` (1-10), `size`, and `output_format` are honored subject to the same ImageMagick
fallback as generations. `quality`, `moderation`, and `input_fidelity` are accepted for compatibility but
ignored; `background=transparent` is rejected with `unsupported_value`, while
`background=opaque/auto` is accepted as a no-op. `output_compression` is applied to
JPEG/WebP output. `markup` is not supported and returns a standard `unsupported_value` error.
`stream` is not supported; `partial_images` is accepted but has no effect.

```bash
curl http://localhost:8081/v1/images/edits \\
  -H "Authorization: Bearer sk-your-key" \\
  -F "model=gpt-image-1" \\
  -F "image=@source.png" \\
  -F "mask=@mask.png" \\
  -F "prompt=Replace the sky with a sunset" \\
  -F "response_format=b64_json"
```

The Responses API also accepts `{"type": "image_generation"}` in `tools` and returns an
`image_generation_call` containing base64 output. Chat Completions recognizes explicit
requests such as `generate an image of ...` and returns a Markdown image URL.

Generated image downloads are restricted to validated Google image hosts, with bounded
redirects, size limits, and image signature checks. The server performs the authenticated
media download and returns the bytes in `b64_json`; signed Gemini delivery URLs are never
exposed to clients. The service does not host generated images, so `response_format=url` is
not supported.

## Limitations

- **Image input requires authentication and Chrome transport**: Use the modular server with a valid cookie and `curl_cffi`. Serv00/FreeBSD can use the [native image-input release](deploy/serv00/README.md) with bundled runtime wheels, without Docker.
- **Not real Pro/Ultra**: Without a paid subscription cookie, `gemini-3.1-pro` routes to the same Flash model. The "Pro" label is a UI preference, not a backend model switch.
- **Single-turn only**: Each request is an independent conversation. Multi-turn context is simulated by including previous messages in the prompt.
- **Rate limits**: Google may throttle high-frequency requests. The server retries automatically but sustained heavy use may be blocked.

## Requirements

- Python 3.8+
- `httpx` (`pip install httpx`) — used for streaming requests
- Network access to `gemini.google.com` (proxy/VPN may be needed in some regions)

## How It Works

This tool reverse-engineers Google Gemini's web StreamGenerate protocol. It sends requests to the same endpoint that the Gemini web app uses, converting between OpenAI's API format and Gemini's internal protobuf-like format.

The model selection is controlled by field `[79]` in the request payload, mapped from Gemini's frontend JavaScript source (`MODE_CATEGORY` enum).

## Acknowledgments

- Inspired by the open-source API proxy ecosystem

## License

MIT

---

## 致谢

本项目的开发 agent 能力由 [GenericAgent](https://github.com/lsdefine/GenericAgent) 提供。

### 🚩 友情链接

[![GenericAgent](https://img.shields.io/badge/Agent_Framework-GenericAgent-orange?style=for-the-badge&logo=github)](https://github.com/lsdefine/GenericAgent)
[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)
