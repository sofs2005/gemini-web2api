#!/bin/sh
# Install a prepared FreeBSD image release using only bundled wheels.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$root"
umask 077
python3 - <<'PY'
import hashlib, json, platform, sys
from pathlib import Path
if platform.system() != "FreeBSD" or platform.machine() != "amd64" or sys.version_info[:2] != (3, 11):
    raise SystemExit("This release requires FreeBSD amd64 and Python 3.11")
files = json.loads(Path("SHA256SUMS.json").read_text())
for name, expected in files.items():
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit("Invalid release manifest path")
    if hashlib.sha256(relative.read_bytes()).hexdigest() != expected:
        raise SystemExit("Release checksum mismatch: " + name)
print("Release files verified")
checks = json.loads(Path("wheelhouse/SHA256SUMS.json").read_text())
for name, expected in checks.items():
    if Path(name).name != name:
        raise SystemExit("Invalid wheel manifest path")
    actual = hashlib.sha256((Path("wheelhouse") / name).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit("Wheel checksum mismatch: " + name)
print("Bundled FreeBSD wheels verified")
PY
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
# Install from the verified wheelhouse without consulting pip platform tags.
# Binary wheels are built for FreeBSD 14.3 and remain ABI-compatible across
# patch levels (for example _p19), which pip otherwise rejects.
.venv/bin/python "$root/install_wheels.py" "$root/wheelhouse"
.venv/bin/python - <<'PY'
import cffi
from curl_cffi import Curl
from curl_cffi.requests.impersonate import DEFAULT_CHROME
curl = Curl()
try:
    if curl.impersonate(DEFAULT_CHROME) != 0:
        raise SystemExit("Chrome impersonation unavailable")
    print("cffi", cffi.__version__)
    print(curl.version().decode())
finally:
    curl.close()
PY
printf 'Installed offline. Configure config.json, then run: sh start.sh\n'
