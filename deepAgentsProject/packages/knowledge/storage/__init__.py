from __future__ import annotations

import os
from pathlib import Path

from packages.knowledge.ports import ObjectStorage

from .local import LocalObjectStorage
from .oss import AliyunOSSObjectStorage


def create_object_storage(root: Path) -> ObjectStorage:
    provider = os.getenv("KNOWLEDGE_OBJECT_STORE", "local").strip().lower()
    if provider == "local":
        return LocalObjectStorage(root)
    if provider == "oss":
        return AliyunOSSObjectStorage.from_environment()
    raise ValueError(f"Unsupported KNOWLEDGE_OBJECT_STORE: {provider}")


__all__ = ["AliyunOSSObjectStorage", "LocalObjectStorage", "create_object_storage"]
