"""Record and verify exact release wheels produced from hash-checked inputs.

This is an integrity manifest, not a signature or a substitute for trusted CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


MANIFEST = "wheelhouse.json"


def inventory(directory: Path) -> list[dict]:
    entries = []
    for path in sorted(directory.iterdir()):
        if path.name == MANIFEST:
            continue
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", path.name):
            raise ValueError("Unexpected wheelhouse entry")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append({"filename": path.name, "sha256": digest.hexdigest(), "size_bytes": path.stat().st_size})
    if not entries:
        raise ValueError("Wheelhouse must not be empty")
    return entries


def record(directory: Path) -> None:
    path = directory / MANIFEST
    if path.exists() or path.is_symlink():
        raise ValueError("Refusing to replace an existing wheel manifest")
    wheels = inventory(directory)
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"version": 1, "wheels": wheels}, stream, sort_keys=True, indent=2)
        stream.write("\n")


def verify(directory: Path) -> None:
    path = directory / MANIFEST
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Wheel manifest is unavailable or invalid")
    saved = json.loads(path.read_text())
    if saved != {"version": 1, "wheels": inventory(directory)}:
        raise ValueError("Wheelhouse differs from its release manifest")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["record", "verify"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    {"record": record, "verify": verify}[args.operation](args.directory)


if __name__ == "__main__":
    main()
