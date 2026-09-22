"""Parse untrusted originals in a separate, time- and resource-limited process."""

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from llamaindex_service.contracts import ServiceError
from llamaindex_service.storage.objects import (
    READ_SIZE,
    validate_filename,
    validate_signature,
)

PARSER_VERSION = "local-structured-v1"


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ParseResult:
    blocks: list[ParsedBlock]
    page_count: int | None
    warnings: list[str]


def verify_original(path: Path, expected_sha256: str, max_bytes: int) -> None:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(READ_SIZE):
            size += len(block)
            if size > max_bytes:
                raise ServiceError(
                    "FILE_TOO_LARGE",
                    "The stored file exceeds the configured limit.",
                    413,
                )
            digest.update(block)
    if not expected_sha256 or digest.hexdigest() != expected_sha256:
        raise ServiceError(
            "CONTENT_HASH_MISMATCH", "The original file failed its integrity check."
        )


def parse_document(
    path: str | Path,
    filename: str,
    settings: Any,
    expected_sha256: str | None = None,
    cancelled: Event | None = None,
) -> ParseResult:
    path = Path(path).resolve()
    if cancelled is not None and cancelled.is_set():
        raise ServiceError(
            "JOB_CANCELLED", "The job was cancelled or its lease expired.", 409
        )
    extension = validate_filename(filename)
    if path.stat().st_size > settings.max_upload_bytes:
        raise ServiceError(
            "FILE_TOO_LARGE", "The stored file exceeds the configured limit.", 413
        )
    with path.open("rb") as stream:
        validate_signature(stream, extension)
    if expected_sha256 is not None:
        verify_original(path, expected_sha256, settings.max_upload_bytes)
    limits = {
        "extension": extension,
        "max_pages": settings.max_pages,
        "max_characters": settings.max_extracted_characters,
        "max_entries": settings.max_docx_entries,
        "max_uncompressed_bytes": settings.max_docx_uncompressed_bytes,
        "max_compression_ratio": settings.max_docx_compression_ratio,
        "memory_mb": settings.parser_memory_mb,
        "cpu_seconds": max(1, int(settings.parse_timeout_seconds)),
    }
    # -I and a fresh environment keep application credentials and import hooks
    # out of the parsing process. This is defense in depth, not a container VM.
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONHASHSEED": "0"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_parser_process.py")),
            str(path),
            json.dumps(limits),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
    )
    deadline = time.monotonic() + settings.parse_timeout_seconds
    try:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise ServiceError(
                    "JOB_CANCELLED", "The job was cancelled or its lease expired.", 409
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceError(
                    "PARSE_TIMEOUT", "Document parsing exceeded the time limit."
                )
            if sys.platform == "darwin" and process.poll() is None:
                try:
                    memory = subprocess.run(
                        ["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                        capture_output=True,
                        timeout=min(1, remaining),
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ServiceError(
                        "PARSE_TIMEOUT", "Document parsing exceeded the time limit."
                    ) from exc
                resident = memory.stdout.strip()
                if (
                    resident.isdigit()
                    and int(resident) > settings.parser_memory_mb * 1024
                ):
                    raise ServiceError(
                        "PARSE_RESOURCE_LIMIT",
                        "Document parsing exceeded the memory limit.",
                    )
            try:
                output, _ = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if process.returncode != 0:
        raise ServiceError(
            "PARSE_RESOURCE_LIMIT",
            "The document parser exited unexpectedly or exceeded a resource limit.",
        )
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError) as exc:
        raise ServiceError(
            "PARSE_FAILED", "The document parser returned an invalid result."
        ) from exc
    if "error" in result:
        raise ServiceError(result["error"]["code"], result["error"]["message"])
    return ParseResult(
        blocks=[ParsedBlock(**block) for block in result["blocks"]],
        page_count=result["page_count"],
        warnings=result["warnings"],
    )
