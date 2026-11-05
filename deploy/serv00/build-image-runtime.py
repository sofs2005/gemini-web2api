#!/usr/bin/env python3
"""Build a pinned Chrome transport wheel on FreeBSD amd64 without root access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import urllib.request

CURL_CFFI_URL = "https://files.pythonhosted.org/packages/82/e1/730125c43e3e331d98e17af3cb310ba526b3f1101b7635ca23d976ebfcf5/curl_cffi-0.16.3.tar.gz"
CURL_CFFI_SHA256 = "d15d0c2a35f2d75bec430c28946c2a833f421c85773bdb0795182cc5c515665b"
IMPERSONATE_URL = "https://github.com/lexiforest/curl-impersonate/releases/download/v2.2.2/libcurl-impersonate-v2.2.2.x86_64-freebsd.tar.gz"
IMPERSONATE_SHA256 = "74c45b40118985faecbe447cf2498268591b51509e67ca1364efdd9ac50617c9"


def download(url, target, expected):
    if not target.exists():
        partial = target.with_suffix(target.suffix + ".partial")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise ValueError("Archive checksum mismatch: " + target.name)
    print("Verified " + target.name, flush=True)


def extract(archive, destination):
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(destination, filter="data")


def patch_source(source):
    """Backport only FreeBSD architecture detection and C++ linking from upstream."""
    libs_path = source / "libs.json"
    libs = json.loads(libs_path.read_text())
    if not any(item["system"] == "FreeBSD" and item["machine"] == "amd64" for item in libs):
        libs.append({"system": "FreeBSD", "machine": "amd64", "pointer_size": 64,
                     "sysname": "freebsd", "link_type": "static",
                     "obj_name": "libcurl-impersonate.a", "arch": "x86_64"})
        libs_path.write_text(json.dumps(libs, indent=4) + "\n")
    build_path = source / "scripts/build.py"
    build = build_path.read_text()
    old = "    elif is_android:\n        extra_link_args = ["
    new = '    elif system == "FreeBSD" or is_android:\n        extra_link_args = ['
    if new not in build:
        if build.count(old) != 1:
            raise ValueError("Unexpected curl_cffi build source; refusing to patch")
        build_path.write_text(build.replace(old, new))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path, default=Path("runtime-build"))
    args = parser.parse_args()
    if platform.system() != "FreeBSD" or platform.machine() != "amd64":
        raise SystemExit("Run this builder on FreeBSD amd64 with Python 3.10 or newer")
    if sys.version_info < (3, 11, 4):
        raise SystemExit("Use Python 3.11.4 or newer for safe tar extraction")
    root = args.build_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_archive, library_archive = root / "curl_cffi-0.16.3.tar.gz", root / "libcurl-2.2.2-freebsd.tar.gz"
    download(CURL_CFFI_URL, source_archive, CURL_CFFI_SHA256)
    download(IMPERSONATE_URL, library_archive, IMPERSONATE_SHA256)
    extract(source_archive, root)
    library = root / "libcurl"
    library.mkdir(exist_ok=True)
    extract(library_archive, library)
    source = root / "curl_cffi-0.16.3"
    patch_source(source)
    environment = dict(os.environ, IMPERSONATE_BUILD_DIR=str(library), MAKEFLAGS="-j1")
    python = sys.executable
    subprocess.run([python, "-m", "pip", "install", "wheel", "setuptools>=77", "cffi>=2.0.0", "certifi>=2024.2.2"], check=True)
    wheels = root / "wheelhouse"
    wheels.mkdir(exist_ok=True)
    # curl_cffi needs cffi at runtime; build it here so the offline wheelhouse
    # is complete even when the host platform tag has a patch-level suffix.
    subprocess.run([python, "-m", "pip", "wheel", "--wheel-dir", str(wheels), "cffi>=2.0.0"], check=True)
    subprocess.run([python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                    "--wheel-dir", str(wheels), str(source)], env=environment, check=True)
    built = list(wheels.glob("curl_cffi-0.16.3-*.whl"))
    if len(built) != 1:
        raise RuntimeError("Expected exactly one FreeBSD curl_cffi wheel")
    subprocess.run([python, "-m", "pip", "install", str(built[0])], check=True)
    # Only initialize a curl handle; this probe makes no network requests.
    subprocess.run([python, "-c", "from curl_cffi import Curl; from curl_cffi.requests.impersonate import DEFAULT_CHROME; c=Curl(); assert c.impersonate(DEFAULT_CHROME) == 0; print(c.version().decode()); c.close()"], check=True)
    manifest = {"curl_cffi": "0.16.3", "libcurl_impersonate": "2.2.2",
                "platform": platform.platform(), "python": platform.python_version(),
                "patch": "FreeBSD amd64 architecture and libc++ link flags from upstream",
                "sources": {CURL_CFFI_URL: CURL_CFFI_SHA256, IMPERSONATE_URL: IMPERSONATE_SHA256},
                "wheel": built[0].name,
                "wheel_sha256": hashlib.sha256(built[0].read_bytes()).hexdigest()}
    (wheels / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("FreeBSD Chrome transport ready: " + str(built[0]), flush=True)


if __name__ == "__main__":
    main()
