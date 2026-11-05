#!/bin/sh
# Bundle application source and a prepared FreeBSD amd64/Python 3.11 wheelhouse.
set -eu
export COPYFILE_DISABLE=1
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
if [ "$#" -lt 1 ]; then
    printf 'Usage: %s WHEELHOUSE [OUTPUT.tar.gz]\n' "$0" >&2
    exit 1
fi
wheels=$(CDPATH= cd -- "$1" && pwd)
out=${2:-"$root/../gemini-web2api-serv00-images.tar.gz"}
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT HUP INT TERM
sh "$root/deploy/serv00/build.sh" "$stage/source.tar.gz"
tar -xzf "$stage/source.tar.gz" -C "$stage"
release="$stage/gemini-web2api"
mkdir -p "$release/wheelhouse"
cp "$wheels"/*.whl "$release/wheelhouse/"
cp "$wheels/runtime-manifest.json" "$release/wheelhouse/"
cp "$root/deploy/serv00/install-image.sh" "$release/install.sh"
cp "$root/deploy/serv00/install_wheels.py" "$release/install_wheels.py"
cp "$root/deploy/image-test/smoke.py" "$release/smoke.py"
cp "$root/deploy/serv00/build-image-runtime.py" "$release/build-image-runtime.py"
cp "$root/deploy/serv00/config.production.example.json" "$release/config.example.json"
cp "$root/deploy/serv00/RELEASE_NOTES.md" "$release/RELEASE_NOTES.md"
cp "$root/LICENSE" "$release/LICENSE"
python_bin=${PYTHON:-python3}
"$python_bin" - "$root" "$release" <<'PY'
import hashlib, json, runpy, subprocess, sys
from pathlib import Path
root, release = map(Path, sys.argv[1:])
wheelhouse = release / "wheelhouse"
checks = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(wheelhouse.glob("*.whl"))}
if not any(name.startswith("curl_cffi-0.16.3-") and "freebsd" in name for name in checks):
    raise SystemExit("Missing FreeBSD curl_cffi wheel")
if not any(name.startswith("cffi-") and "freebsd" in name for name in checks):
    raise SystemExit("Missing FreeBSD cffi wheel")
(wheelhouse / "SHA256SUMS.json").write_text(json.dumps(checks, indent=2) + "\n")
config = json.loads((root / "deploy/image-test/config.example.json").read_text())
config.update(host="127.0.0.1", port=41748, cookie_file="cookie.json", model_snapshot_file="gemini-models.json",
              cache_db_path="./image-test-cache.sqlite3", log_file="logs/service.log")
(release / "config.image-test.example.json").write_text(json.dumps(config, indent=2) + "\n")
revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
version = runpy.run_path(str(root / "gemini_web2api/__init__.py"))["__version__"]
(release / "release.json").write_text(json.dumps({"version": version, "channel": "stable",
    "revision": revision, "platform": "FreeBSD 14.3 amd64", "python": "3.11",
    "image_input": True, "offline_install": True, "default_image_test_port": 41748}, indent=2) + "\n")
files = {p.relative_to(release).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in sorted(release.rglob("*")) if p.is_file()}
(release / "SHA256SUMS.json").write_text(json.dumps(files, indent=2) + "\n")
PY
tar -czf "$out" -C "$stage" gemini-web2api
printf 'Created FreeBSD image-input release: %s\n' "$out"
