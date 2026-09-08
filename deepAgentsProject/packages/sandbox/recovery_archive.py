"""Versioned recovery archive: repository, artifacts and temporary tool files.

Git metadata is re-created from the immutable source by provisioning, never
accepted from a mutable workspace. Links, devices and path traversal are denied.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import PurePosixPath

from packages.coding.errors import SandboxUnavailableError

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
ROOTS = ("workspace/repo", "artifacts", "tmp")


def normalize_recovery_archive(content: bytes) -> bytes:
    if len(content) > MAX_ARCHIVE_BYTES:
        raise SandboxUnavailableError("Recovery archive exceeds the transfer limit")
    # Recovery archives are plain tar, not attacker-controlled compressed input.
    target = io.BytesIO()
    seen = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as source:
            with tarfile.open(fileobj=target, mode="w") as destination:
                for member in source:
                    path = PurePosixPath(member.name)
                    name = str(path)
                    if (path.is_absolute() or ".." in path.parts or ".git" in path.parts
                            or "\\" in name or name in seen or len(seen) >= 100_000
                            or not any(name == root or name.startswith(root + "/") for root in ROOTS)
                            or (not member.isfile() and not member.isdir())):
                        raise SandboxUnavailableError("Recovery archive contains an unsafe entry")
                    if name in ROOTS and not member.isdir():
                        raise SandboxUnavailableError("Recovery archive root must be a directory")
                    seen.add(name)
                    total += member.size
                    if member.size < 0 or total > MAX_ARCHIVE_BYTES:
                        raise SandboxUnavailableError("Recovery archive exceeds the unpacked limit")
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
                    info.mode = member.mode & 0o777
                    info.uid = info.gid = 10001
                    info.size = 0 if member.isdir() else member.size
                    destination.addfile(info, source.extractfile(member) if member.isfile() else None)
                    if target.tell() > MAX_ARCHIVE_BYTES:
                        raise SandboxUnavailableError("Recovery archive exceeds the normalized limit")
    except (tarfile.TarError, ValueError, OSError) as exc:
        raise SandboxUnavailableError("Invalid recovery archive") from exc
    if not set(ROOTS).issubset(seen):
        raise SandboxUnavailableError("Recovery archive is missing a required root")
    if len(target.getvalue()) > MAX_ARCHIVE_BYTES:
        raise SandboxUnavailableError("Recovery archive exceeds the normalized limit")
    return target.getvalue()


def combine_docker_archives(parts: list[tuple[str, bytes]]) -> bytes:
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as destination:
        for root, content in parts:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as source:
                for member in source:
                    path = PurePosixPath(member.name)
                    if not path.parts or path.parts[0] != PurePosixPath(root).name:
                        raise SandboxUnavailableError("Docker recovery archive has an invalid root")
                    if root == "workspace/repo" and len(path.parts) > 1 and path.parts[1] == ".git":
                        continue
                    member.name = str(PurePosixPath(root, *path.parts[1:]))
                    destination.addfile(member, source.extractfile(member) if member.isfile() else None)
                    if target.tell() > MAX_ARCHIVE_BYTES:
                        raise SandboxUnavailableError("Recovery archive exceeds the transfer limit")
    return normalize_recovery_archive(target.getvalue())
