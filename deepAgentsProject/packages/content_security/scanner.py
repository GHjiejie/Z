from __future__ import annotations

import os
import socket
import struct
from time import monotonic
import math
from dataclasses import dataclass
from typing import Protocol


class ContentScanError(RuntimeError):
    pass


class ContentRejectedError(ContentScanError):
    pass


class ContentScanner(Protocol):
    name: str

    def scan(self, content: bytes, *, object_name: str) -> None: ...


@dataclass(frozen=True)
class NoopContentScanner:
    name: str = "disabled"

    def scan(self, content: bytes, *, object_name: str) -> None:
        return None


class ClamAVContentScanner:
    """Fail-closed ClamAV INSTREAM client over a Unix socket or private TCP."""

    name = "clamav"

    def __init__(
        self,
        *,
        unix_socket: str | None = None,
        host: str = "127.0.0.1",
        port: int = 3310,
        timeout_seconds: float = 15.0,
        max_bytes: int = 512 * 1024 * 1024,
    ):
        self.unix_socket = unix_socket
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_bytes <= 0:
            raise ValueError('Scanner timeout and byte limit must be positive and finite')

    def scan(self, content: bytes, *, object_name: str) -> None:
        if len(content) > self.max_bytes:
            raise ContentRejectedError(
                f"Content exceeds the malware scanner limit: {object_name}"
            )
        connection: socket.socket | None = None
        deadline = monotonic() + self.timeout_seconds

        def bound_next_io():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError('Scanner total deadline expired')
            connection.settimeout(remaining)

        try:
            if self.unix_socket:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(self.timeout_seconds)
                connection.connect(self.unix_socket)
            else:
                connection = socket.create_connection(
                    (self.host, self.port), timeout=self.timeout_seconds
                )
            bound_next_io()
            connection.sendall(b"zINSTREAM\0")
            for offset in range(0, len(content), 64 * 1024):
                chunk = content[offset : offset + 64 * 1024]
                bound_next_io()
                connection.sendall(struct.pack("!I", len(chunk)))
                bound_next_io()
                connection.sendall(chunk)
            bound_next_io()
            connection.sendall(struct.pack("!I", 0))
            response = bytearray()
            while len(response) <= 4096:
                bound_next_io()
                part = connection.recv(4096)
                if not part:
                    break
                response.extend(part)
                if b"\0" in part or b"\n" in part:
                    break
            bound_next_io()
        except (OSError, TimeoutError) as exc:
            raise ContentScanError("Malware scanner is unavailable") from exc
        finally:
            if connection is not None:
                connection.close()
        result = bytes(response).rstrip(b"\0\r\n").decode("utf-8", errors="replace")
        if result.endswith(" OK"):
            return
        if result.endswith(" FOUND"):
            signature = result.rsplit(": ", 1)[-1].removesuffix(" FOUND")
            raise ContentRejectedError(
                f"Content was rejected by malware policy ({signature}): {object_name}"
            )
        raise ContentScanError("Malware scanner returned an invalid response")


def create_content_scanner(*, production: bool = False) -> ContentScanner:
    provider = os.getenv(
        "DEEPAGENT_CONTENT_SCANNER", "clamav" if production else "disabled"
    ).strip().lower()
    if provider in {"", "disabled", "none"}:
        if production:
            raise ContentScanError("A content scanner is required in production")
        return NoopContentScanner()
    if provider != "clamav":
        raise ContentScanError(f"Unsupported content scanner: {provider}")
    return ClamAVContentScanner(
        unix_socket=os.getenv("CLAMAV_UNIX_SOCKET", "").strip() or None,
        host=os.getenv("CLAMAV_HOST", "127.0.0.1").strip(),
        port=int(os.getenv("CLAMAV_PORT", "3310")),
        timeout_seconds=float(os.getenv("CLAMAV_TIMEOUT_SECONDS", "15")),
        max_bytes=int(os.getenv("CLAMAV_MAX_BYTES", str(512 * 1024 * 1024))),
    )
