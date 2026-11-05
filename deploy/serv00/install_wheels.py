#!/usr/bin/env python3
"""Install verified release wheels without pip's host platform-tag matching."""
import json
import sys
import sysconfig
import zipfile
from pathlib import Path, PurePosixPath


def install(wheelhouse, site):
    """Extract only manifest-listed wheels into site and return their paths."""
    wheelhouse = Path(wheelhouse)
    site = Path(site)
    manifest = json.loads((wheelhouse / "SHA256SUMS.json").read_text())
    if not isinstance(manifest, dict) or not manifest:
        raise SystemExit("Wheel manifest is empty")
    wheels = []
    for name in sorted(manifest):
        relative = Path(name)
        if relative.name != name or not name.endswith(".whl"):
            raise SystemExit("Invalid wheel manifest entry: " + name)
        wheels.append(wheelhouse / name)
    if not wheels:
        raise SystemExit("No wheels found in " + str(wheelhouse))
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                name = PurePosixPath(member.filename)
                if name.is_absolute() or ".." in name.parts:
                    raise SystemExit("Unsafe wheel path: " + member.filename)
            archive.extractall(site)
    print("Installed %d verified wheels into %s" % (len(wheels), site))
    return wheels


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (1, 2):
        raise SystemExit("Usage: install_wheels.py WHEELHOUSE [SITE]")
    site = args[1] if len(args) == 2 else sysconfig.get_paths()["purelib"]
    install(args[0], site)


if __name__ == "__main__":
    main()
