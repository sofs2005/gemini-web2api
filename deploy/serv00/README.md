# serv00 (FreeBSD) deployment package

There are two native FreeBSD release archives. Neither requires Docker or root access, and neither includes cookies, credentials, or private configuration.

- `gemini-web2api-serv00.tar.gz`: application source for text serving; install dependencies from PyPI.
- `gemini-web2api-serv00-images.tar.gz`: application source plus a complete offline wheelhouse for **FreeBSD 14.3 amd64 / Python 3.11**, including Chrome image transport. Use this release for image input. Stable releases can be named `gemini-web2api-serv00-v1.3.0-freebsd14.3-amd64-py311.tar.gz`.

The formal `v1.3.0` release is built by GitHub Actions. A tag push produces the multi-architecture GHCR image (`ghcr.io/meatroll/gemini-web2api:v1.3.0`) and attaches the FreeBSD native archive (`gemini-web2api-serv00-v1.3.0-freebsd14.3-amd64-py311.tar.gz`) plus its SHA256 file to the GitHub Release. Future releases use the same `vX.Y.Z` tag convention.

For a new production installation:

```sh
mkdir -p ~/gemini-web2api
tar -xzf gemini-web2api-serv00-v1.3.0-freebsd14.3-amd64-py311.tar.gz -C ~/gemini-web2api --strip-components 1
cd ~/gemini-web2api
sh install.sh
cp config.example.json config.json
# Reserve a TCP port with `devil port add tcp random`, then configure that port.
# Configure api_keys and cookie_file before starting, and connect your tunnel to this port.
sh start.sh
```

For upgrades, retain the existing `config.json`, credentials, model snapshot and cache. Install dependencies in a fresh venv, save the previous code and venv, and stop/switch the API under `.keepalive.lock` so cron cannot race the upgrade. Verify the health endpoint reports 1.3.0 and the current public model-list endpoint works. Neither check sends a Gemini generation request. Do not overwrite the active configuration with either example template.

The image release bundles `curl_cffi 0.16.3` statically linked with upstream `libcurl-impersonate 2.2.2` (BoringSSL). Its build applies only FreeBSD architecture detection and libc++ link flags from upstream. `build-image-runtime.py` pins and verifies both source archives, and records the runtime source and wheel hashes. The installer checks the complete release manifest and all wheel hashes before installing. No library compilation is required when deploying the prepared release.

To deploy the image release into an isolated test directory:

```sh
mkdir -p ~/gemini-web2api-image-test
tar -xzf gemini-web2api-serv00-images.tar.gz -C ~/gemini-web2api-image-test --strip-components 1
cd ~/gemini-web2api-image-test
sh install.sh
cp config.image-test.example.json config.json
# Set cookie_file and model_snapshot_file to your existing account export and snapshot.
# Reserve a TCP port with `devil port add tcp random`, then use the port shown by `devil port list`.
# Set config.json's port to that reserved port before starting (loopback also requires a reservation).
# Leave retry_attempts=1, model_check_interval_sec=0, cookie_rotation_interval_sec=0.
sh start.sh
# Replace 39781 below with your reserved test port.
.venv/bin/python smoke.py --base-url http://127.0.0.1:39781/v1
```

The test template binds to loopback and uses 41748 as an example; **Serv00 requires reserving the chosen TCP port even for loopback**. It disables background Google requests and generation retries. The smoke script makes one image inference without answer hints, prevents concurrent probes, and enforces a persistent 60-second cooldown, including failed attempts. Stop after any authentication, rate-limit, protocol, or recognition failure. A second SSE probe is optional and should be run only after a successful first probe and at least 60 seconds of cooldown. The wheelhouse contains no login state; the account export can be reused by path without modifying it.

To reproduce the runtime on FreeBSD amd64 with Python 3.11.4 or newer, run the builder in a clean virtual environment, collect the application dependencies with `pip wheel`, and pass that wheelhouse to `build-images.sh`. Building in a `--system-site-packages` environment can pick up incompatible system build tools; use an isolated venv. The prepared runtime was built on FreeBSD 14.3 and is not a general cross-platform wheel.

For the text-only source archive, upload it to your serv00 home directory, then run:

```sh
tar -xzf gemini-web2api-serv00.tar.gz
cd gemini-web2api
cp config.example.json config.json
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
sh start.sh
```

`start.sh` runs the service in the background, while `sh run.sh` runs it in the foreground. `keepalive.sh` uses `flock` to avoid concurrent restarts and can be called from cron every five minutes. Its local health check restarts the service if either the API or the cookie-rotation worker has stopped.
Set a free port in `config.json` before starting; if exposing a port through serv00, reserve/configure it in the control panel. The template binds to `127.0.0.1` only. Configure authentication and any proxy/tunnel separately before exposing the API.

Generated images are returned as `b64_json`. The service does not host image files, so
`response_format=url` returns a standard `unsupported_value` error. Decode the base64
response and store it in your own application if a public link is required.

Set `cookie_file` to a path on the server only after uploading credentials securely, and keep those files private. For unattended sessions, set `cookie_rotation_interval_sec` to `300`; the service calls Google's RotateCookies endpoint and atomically updates the JSON cookie file with mode 0600. Rotation requires a fresh authenticated export to start and cannot revive a rejected session. Stop the foreground process with Ctrl-C.

The modular service also supports model change monitoring. The default `model_check_interval_sec` is `86400` (one day); set it to `0` to disable monitoring. Set `model_snapshot_file` to an absolute writable path such as `/home/igsshan/gemini-web2api/gemini-models.json`. With an authentication export configured and no saved attempt, the first start immediately reads the model list and establishes the daily cycle. Subsequent starts respect the saved schedule. The minimum enabled interval is one hour. Each due check makes only one model-list RPC request, never a generation request. Both successful and failed attempt times persist across restarts. A private atomic snapshot records the current models and latest additions, removals, renames, and metadata changes; authentication failures leave the valid baseline intact. Events are logged even when request logging is disabled. The valid snapshot drives `/v1/models`, `/v1beta/models`, and complete model selection headers for generation, without extra discovery requests. Refresh the channel model list in New API after updating the service. See the main README for the full configuration and state fields.

The account has a 512 MB RAM quota and existing services use about 300 MB. Use one small image request at a time and monitor available memory before increasing concurrency. The image test instance uses its own directory, process, cache, and logs; no cron entry is installed automatically.
