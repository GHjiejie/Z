"""Bounded orphan scanning with an age grace and transactional reference checks."""

import logging
import time
from collections.abc import Iterator
from typing import Any

from llamaindex_service.storage import ObjectInfo, Storage

logger = logging.getLogger(__name__)


class Maintenance:
    def __init__(
        self,
        repository: Any,
        storage: Storage,
        grace_seconds: float = 86400,
        scan_limit: int = 500,
        delete_limit: int = 100,
    ):
        if grace_seconds < 86400:
            raise ValueError("Orphan grace must be at least 24 hours")
        if min(scan_limit, delete_limit) < 1:
            raise ValueError("Maintenance limits must be positive")
        self.repository = repository
        self.storage = storage
        self.grace_seconds = grace_seconds
        self.scan_limit = scan_limit
        self.delete_limit = delete_limit
        self._objects: Iterator[ObjectInfo] | None = None

    def run_once(self) -> dict[str, int]:
        """Continue a bounded inventory scan after enforcing history retention.

        Repository locks each object key while checking references and deleting.
        Upload finalization uses that same lock and rejects creation times over one
        hour old, preventing delayed uploads from referencing collected objects.
        """
        expired = self.repository.purge_expired_history()
        scanned, deleted = 0, 0
        cutoff = time.time() - self.grace_seconds
        if self._objects is None:
            self._objects = self.storage.iter_objects(page_size=self.scan_limit)
        try:
            while scanned < self.scan_limit and deleted < self.delete_limit:
                item = next(self._objects, None)
                if item is None:
                    self.close()
                    break
                scanned += 1
                if item.modified_at >= cutoff:
                    continue
                if self.repository.delete_orphan_object(
                    item.object_key, self.storage.delete
                ):
                    deleted += 1
        except Exception:
            self.close()
            raise
        result = {
            "expired_messages": expired,
            "scanned_objects": scanned,
            "deleted_objects": deleted,
        }
        logger.info("Worker maintenance completed", extra=result)
        return result

    def close(self) -> None:
        close = getattr(self._objects, "close", None)
        if close:
            close()
        self._objects = None
