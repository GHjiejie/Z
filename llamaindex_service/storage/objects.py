"""Bounded streaming uploads; object identifiers never contain user filenames."""

import hashlib
import os
import re
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from llamaindex_service.contracts import ServiceError

READ_SIZE = 64 * 1024
OBJECT_KEY = re.compile(r"objects/[a-f0-9]{32}\.(?:txt|md|pdf|docx)\Z")
CONTENT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str
    filename: str
    created_at: float


@dataclass(frozen=True)
class ObjectInfo:
    """Private object inventory for bounded maintenance scans."""

    object_key: str
    modified_at: float


class Storage(Protocol):
    def write(
        self, stream: BinaryIO, filename: str, content_type: str | None = None
    ) -> StoredObject: ...

    def open(self, object_key: str) -> BinaryIO: ...

    def delete(self, object_key: str) -> None: ...

    def materialize(self, object_key: str) -> Any: ...

    def iter_objects(self, page_size: int = 500) -> Iterator[ObjectInfo]: ...


def validate_filename(filename: str, content_type: str | None = None) -> str:
    """Allow supported data formats and reject path/control characters early."""
    if (
        not filename
        or len(filename.encode("utf-8")) > 255
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise ServiceError(
            "INVALID_FILENAME", "Use a filename without path components."
        )
    extension = Path(filename).suffix.lower()
    if extension not in CONTENT_TYPES:
        raise ServiceError(
            "UNSUPPORTED_FILE_TYPE", "Supported formats: TXT, MD, PDF, DOCX.", 415
        )
    mime = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    accepted = {CONTENT_TYPES[extension], "application/octet-stream"}
    if extension == ".md":
        accepted.update({"text/plain", "text/x-markdown"})
    if extension == ".docx":
        accepted.add("application/zip")
    if mime not in accepted:
        raise ServiceError(
            "FILE_TYPE_MISMATCH", "The MIME type does not match the filename.", 415
        )
    return extension


def validate_signature(stream: BinaryIO, extension: str) -> None:
    stream.seek(0)
    prefix = stream.read(4096)
    stream.seek(0)
    if extension == ".pdf" and not prefix.startswith(b"%PDF-"):
        raise ServiceError(
            "FILE_TYPE_MISMATCH", "The file does not have a PDF signature.", 415
        )
    if extension == ".docx" and not prefix.startswith(b"PK\x03\x04"):
        raise ServiceError(
            "FILE_TYPE_MISMATCH", "The file does not have a DOCX signature.", 415
        )
    if extension in {".txt", ".md"} and (
        b"\x00" in prefix or prefix.startswith((b"%PDF-", b"PK\x03\x04", b"MZ"))
    ):
        raise ServiceError(
            "FILE_TYPE_MISMATCH", "The file must contain UTF-8 text.", 415
        )
    # Full UTF-8 decoding is performed by the resource-limited parser.


@contextmanager
def prepared_upload(
    stream: BinaryIO, filename: str, content_type: str | None, max_bytes: int
) -> Iterator[tuple[BinaryIO, StoredObject]]:
    extension = validate_filename(filename, content_type)
    digest = hashlib.sha256()
    size = 0
    with tempfile.TemporaryFile(mode="w+b") as spool:
        while block := stream.read(min(READ_SIZE, max_bytes - size + 1)):
            if not isinstance(block, bytes):
                raise ServiceError("INVALID_UPLOAD", "Upload must be a binary stream.")
            size += len(block)
            if size > max_bytes:
                raise ServiceError(
                    "FILE_TOO_LARGE", "The upload exceeds the configured limit.", 413
                )
            digest.update(block)
            spool.write(block)
        if size == 0:
            raise ServiceError("EMPTY_FILE", "The uploaded file is empty.")
        validate_signature(spool, extension)
        yield (
            spool,
            StoredObject(
                object_key=f"objects/{uuid.uuid4().hex}{extension}",
                sha256=digest.hexdigest(),
                size_bytes=size,
                content_type=CONTENT_TYPES[extension],
                filename=filename,
                created_at=time.time(),
            ),
        )


def validate_key(object_key: str) -> None:
    if not OBJECT_KEY.fullmatch(object_key):
        raise ServiceError("INVALID_OBJECT_KEY", "Invalid stored object identifier.")


class LocalStorage:
    """Development storage. A random immutable key is atomically installed."""

    def __init__(self, root: str | Path, max_upload_bytes: int = 20 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = self.root / "objects"
        if self.directory.is_symlink():
            raise ServiceError(
                "UNSAFE_STORAGE_PATH", "Storage directory must not be a symlink.", 500
            )
        self.directory.mkdir(mode=0o700, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        validate_key(object_key)
        path = self.root / object_key
        if self.directory.is_symlink() or path.is_symlink():
            raise ServiceError(
                "UNSAFE_STORAGE_PATH", "Stored objects must not be symlinks.", 500
            )
        return path

    def write(
        self, stream: BinaryIO, filename: str, content_type: str | None = None
    ) -> StoredObject:
        with prepared_upload(stream, filename, content_type, self.max_upload_bytes) as (
            spool,
            info,
        ):
            target = self._path(info.object_key)
            # O_EXCL prevents overwriting even if a key collision is forced.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                with os.fdopen(os.open(target, flags, 0o600), "wb") as output:
                    while block := spool.read(READ_SIZE):
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError as exc:
                raise ServiceError(
                    "OBJECT_COLLISION", "Could not allocate a unique object key.", 503
                ) from exc
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            return info

    def open(self, object_key: str) -> BinaryIO:
        path = self._path(object_key)
        try:
            return os.fdopen(
                os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb"
            )
        except FileNotFoundError as exc:
            raise ServiceError(
                "OBJECT_NOT_FOUND", "The original file is unavailable.", 404
            ) from exc

    @contextmanager
    def materialize(self, object_key: str) -> Iterator[Path]:
        path = self._path(object_key)
        if not path.is_file():
            raise ServiceError(
                "OBJECT_NOT_FOUND", "The original file is unavailable.", 404
            )
        yield path

    def delete(self, object_key: str) -> None:
        self._path(object_key).unlink(missing_ok=True)

    def iter_objects(self, page_size: int = 500) -> Iterator[ObjectInfo]:
        # Keep the iterator between passes instead of materializing all filenames.
        if self.directory.is_symlink():
            raise ServiceError(
                "UNSAFE_STORAGE_PATH", "Storage directory must not be a symlink.", 500
            )
        with os.scandir(self.directory) as entries:
            for entry in entries:
                key = f"objects/{entry.name}"
                if OBJECT_KEY.fullmatch(key) and entry.is_file(follow_symlinks=False):
                    try:
                        modified = entry.stat(follow_symlinks=False).st_mtime
                    except FileNotFoundError:
                        continue
                    yield ObjectInfo(key, modified)


class S3Storage:
    """Private S3-compatible objects. Credentials come from the standard SDK chain."""

    def __init__(
        self,
        bucket: str,
        endpoint: str | None = None,
        region: str = "us-east-1",
        max_upload_bytes: int = 20 * 1024 * 1024,
        client: Any = None,
    ):
        if not bucket:
            raise ValueError("S3 bucket is required")
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=region,
                config=Config(
                    connect_timeout=10, read_timeout=60, retries={"max_attempts": 3}
                ),
            )
        self.client = client
        self.bucket = bucket
        self.max_upload_bytes = max_upload_bytes

    def write(
        self, stream: BinaryIO, filename: str, content_type: str | None = None
    ) -> StoredObject:
        with prepared_upload(stream, filename, content_type, self.max_upload_bytes) as (
            spool,
            info,
        ):
            self.client.put_object(
                Bucket=self.bucket,
                Key=info.object_key,
                Body=spool,
                ContentLength=info.size_bytes,
                ContentType=info.content_type,
                Metadata={"sha256": info.sha256, "created_at": str(info.created_at)},
                IfNoneMatch="*",
            )
            return info

    def open(self, object_key: str) -> BinaryIO:
        validate_key(object_key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise ServiceError(
                    "OBJECT_NOT_FOUND", "The original file is unavailable.", 404
                ) from exc
            raise
        if response.get("ContentLength", 0) > self.max_upload_bytes:
            response["Body"].close()
            raise ServiceError(
                "FILE_TOO_LARGE", "The stored file exceeds the configured limit.", 413
            )
        return response["Body"]

    @contextmanager
    def materialize(self, object_key: str) -> Iterator[Path]:
        validate_key(object_key)
        with tempfile.TemporaryDirectory(prefix="rag-object-") as folder:
            path = Path(folder) / Path(object_key).name
            size = 0
            with self.open(object_key) as source, path.open("xb") as output:
                while block := source.read(READ_SIZE):
                    size += len(block)
                    if size > self.max_upload_bytes:
                        raise ServiceError(
                            "FILE_TOO_LARGE",
                            "The stored file exceeds the configured limit.",
                            413,
                        )
                    output.write(block)
            yield path

    def delete(self, object_key: str) -> None:
        validate_key(object_key)
        self.client.delete_object(Bucket=self.bucket, Key=object_key)

    def iter_objects(self, page_size: int = 500) -> Iterator[ObjectInfo]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket,
            Prefix="objects/",
            PaginationConfig={"PageSize": max(1, min(page_size, 1000))},
        ):
            for entry in page.get("Contents", []):
                if OBJECT_KEY.fullmatch(entry["Key"]):
                    yield ObjectInfo(entry["Key"], entry["LastModified"].timestamp())


def build_storage(settings: Any) -> LocalStorage | S3Storage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.storage_path, settings.max_upload_bytes)
    if settings.storage_backend == "s3":
        return S3Storage(
            settings.s3_bucket,
            settings.s3_endpoint,
            settings.s3_region,
            settings.max_upload_bytes,
        )
    raise ValueError(f"Unsupported storage backend: {settings.storage_backend}")
