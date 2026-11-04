# Serv00 native release 1.3.0

This release packages the modular Python server for FreeBSD 14.3 amd64 with Python 3.11. It includes all 13 dependency wheels and installs without Docker, root access, compilation, or package downloads. `release.json` records the source commit and stable release version; `SHA256SUMS.json` covers every packaged file except itself. The installer verifies those files before installing dependencies.

## OpenAI Images conformance

`POST /v1/images/generations` and `POST /v1/images/edits` now follow the OpenAI 2.3.0 request, response, and error shapes much more closely:

- All OpenAI-compatible `/v1` errors use `ErrorResponse` with `type`, `message`, `param`, and `code`; responses carry `x-request-id`. `/v1beta/*` keeps the Google-style error object.
- `stream:false` and `null` are accepted; `stream:true` is rejected with `unsupported_value`. `partial_images` is accepted and range-checked but has no effect because the backend returns only a final image.
- `n` supports 1–10 by making independent upstream image generations. `n>1` does not guarantee visual consistency; latency and Gemini quota use scale roughly linearly. The response-level `size` is omitted when output sizes disagree.
- `size` is validated, added to the generation prompt, and applied with ImageMagick when available. The response reports the actual size when determinable instead of echoing the request.
- `quality`, `style`, `moderation`, `input_fidelity`, `safety_identifier`, `background=opaque/auto`, `user`, and `output_compression` are accepted and validated where applicable; unsupported controls remain no-ops. `background=transparent` and `markup` are rejected with `unsupported_value`.
- `response_format` supports `b64_json` only. `url` is rejected because the service does not host generated images; clients should decode `b64_json` and store it themselves.
- Edits support multipart form data and JSON bodies with an HTTPS image URL or base64 data URL. Only one source image is supported; multi-image edits return `unsupported_value`. A single source image with one mask remains supported. `file_id` references are rejected because the Files API is not implemented.
- `image_request_budget_sec` (default 900; set `0` to disable) bounds the whole Images request, including per-image RPC retries and generated-image downloads. Exhaustion returns HTTP 504 with `code=request_timeout`.
- `usage` is a deterministic proxy-side estimate: `ceil(UTF-8 bytes / 4)` for text and `ceil(width/32) * ceil(height/32)` per input or output image. It is not an OpenAI or Gemini tokenizer count and must not be used for billing.

## Masked edit pipeline

`POST /v1/images/edits` with a mask edits a cropped region of the photo instead of marking the full frame:

- The mask bounds are expanded with roughly a full selection's worth of real surrounding photo, then aligned to the nearest supported aspect ratio by expanding only.
- Only the cropped photo is sent to Gemini; the mask is never uploaded, because the model copies mask colors and shape into the result.
- The edited crop is tone matched against the original crop using statistics taken outside the editable area, so added content cannot darken the patch.
- The result is composited back through the mask alpha, so every pixel outside the editable area stays identical to the source.
- Up to three attempts are ranked by outside drift and selection change, and any generation whose aspect ratio no longer matches the crop is discarded instead of being resized into a cropped image.
- `image_edit_context_crop`, `image_edit_tone_match`, `image_edit_frame_gate`, `image_edit_attempts`, `image_edit_drift_limit` and `image_edit_min_change` control this behavior; `image_edit_crop` and `image_edit_locator` keep the earlier experimental paths available.

Success still depends on the upstream model actually returning an edit: some requests come back unchanged, in which case the best attempt is returned and `selection_change` in the log stays near the raw noise floor.

## Image input and agent compatibility

- Uses the current Gemini attachment structure, authenticated page session/XSRF tokens, and Chrome transport for image generation requests.
- Bundles `curl_cffi 0.16.3` and `libcurl-impersonate 2.2.2` with BoringSSL, built and loaded on Serv00 itself.
- Validates edit masks before any Gemini request: the mask must be a PNG with an alpha channel and must match the source image dimensions. Transparent mask areas are requested as editable while opaque areas are requested to remain unchanged. Source dimensions are read for PNG, JPEG, and WebP inputs.
- Preserves dynamic model selection, agent request budgets, one-attempt agent calls, and SSE heartbeats. Image SSE delivers the completed answer as one content chunk.
- Stops on authentication/rate-limit responses and structured Gemini rejections instead of retrying them.
- Downloads generated Gemini media on the authenticated server path. Images endpoints return `b64_json`; clients receive image bytes, never a signed mediator URL.
- Sends account cookies only to Gemini RPC and the two tested text mediator hosts; final `lh3.googleusercontent.com` image requests use no account cookie.

Real Serv00 validation passed for ordinary and SSE image requests with `gemini-3.8-flash`, and for a tool-result image with `gemini-4-argon` through the existing public API. A mask edit on an isolated Serv00 port returned HTTP 200 in about 27 seconds: the transparent center area became yellow while the opaque blue border remained blue. The user also confirmed the pi workflow. These probes are not run by the installer or launcher.

## Production deployment

Use `config.example.json` for a new production deployment. Reserve a TCP port in Serv00 even for loopback, configure authentication, and point `cookie_file` at a private authenticated export. The production template enables daily model checks and five-minute cookie rotation, and limits generation attempts to one. The separate `config.image-test.example.json` disables background upstream requests for controlled validation.

For an existing installation, keep the current `config.json`, authentication export, model snapshot, cache, API keys and tunnel configuration. Save the previous source and virtual environment before replacing them, and stop the API process under the same `.keepalive.lock` used by cron before switching code. Install dependencies into a fresh virtual environment before the switch. Verify the local health endpoint reports version 1.3.0 and that the existing public model-list route is available. No generation request is necessary for these checks.

This release targets `python -m gemini_web2api`. The legacy single-file entry point and Cloudflare Worker do not include the new attachment transport.
