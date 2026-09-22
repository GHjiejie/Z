"""Worker orchestration; every mutation is fenced by the database claim token."""

import logging
import threading
import time
import uuid
from typing import Any

from llamaindex_service.contracts import ServiceError
from llamaindex_service.ingestion import IngestionProcessor
from llamaindex_service.retrieval.embeddings import EmbeddingError
from llamaindex_service.storage import Storage, build_storage

from .maintenance import Maintenance

logger = logging.getLogger(__name__)


def classify_failure(exc: Exception) -> tuple[str, str, bool]:
    """Persist safe, useful errors without exposing SDK messages or file contents."""
    if isinstance(exc, ServiceError):
        return exc.code, exc.message, exc.status_code in {429, 502, 503, 504}
    if isinstance(exc, EmbeddingError):
        retryable = (
            isinstance(exc.__cause__, Exception) and classify_failure(exc.__cause__)[2]
        )
        return (
            "EMBEDDING_FAILED",
            "The embedding provider could not produce valid vectors.",
            retryable,
        )
    import httpx
    from botocore.exceptions import (
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ReadTimeoutError,
        ResponseStreamingError,
    )

    if isinstance(
        exc, (httpx.TimeoutException, httpx.NetworkError, TimeoutError, ConnectionError)
    ):
        return (
            "UPSTREAM_UNAVAILABLE",
            "An external dependency is temporarily unavailable.",
            True,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        retryable = (
            exc.response.status_code in {408, 429} or exc.response.status_code >= 500
        )
        return (
            "UPSTREAM_ERROR",
            "The external provider rejected the request.",
            retryable,
        )
    if isinstance(
        exc,
        (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
            ResponseStreamingError,
        ),
    ):
        return (
            "STORAGE_UNAVAILABLE",
            "The object store is temporarily unavailable.",
            True,
        )
    response = getattr(exc, "response", {})
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if status:
            return (
                "STORAGE_ERROR",
                "The object store could not complete the request.",
                status in {408, 429} or status >= 500,
            )
    return (
        "INGESTION_FAILED",
        "Document processing failed; inspect worker diagnostics using the job ID.",
        False,
    )


class Worker:
    def __init__(
        self,
        settings: Any,
        repository: Any,
        storage: Storage | None = None,
        processor: IngestionProcessor | None = None,
        worker_id: str | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.storage = storage if storage is not None else build_storage(settings)
        self.processor = (
            processor
            if processor is not None
            else IngestionProcessor(settings, self.storage)
        )
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.maintenance = Maintenance(
            repository,
            self.storage,
            grace_seconds=getattr(settings, "orphan_grace_seconds", 86400),
            scan_limit=getattr(settings, "maintenance_scan_limit", 500),
            delete_limit=getattr(settings, "maintenance_delete_limit", 100),
        )

    def run_once(self) -> bool:
        """Process at most one durable job. False means the queue is empty."""
        job = self.repository.claim_job(
            self.worker_id, self.settings.worker_lease_seconds
        )
        if job is None:
            return False
        job_id, token = job.get("job_id", job.get("id")), job["claim_token"]
        stopped = threading.Event()
        cancelled = threading.Event()

        def heartbeat() -> None:
            interval = max(0.25, self.settings.worker_lease_seconds / 3)
            while not stopped.wait(interval):
                try:
                    alive = self.repository.heartbeat(
                        job_id, token, self.settings.worker_lease_seconds
                    )
                except Exception:  # noqa: BLE001 -- uncertainty must invalidate the claim.
                    # Stop work on uncertainty; a later worker can reclaim the lease.
                    alive = False
                    logger.warning("Worker heartbeat failed", extra={"job_id": job_id})
                if not alive:
                    cancelled.set()
                    return

        def phase(name: str, completed: int = 0) -> None:
            if cancelled.is_set() or not self.repository.job_phase(
                job_id, token, name, completed
            ):
                cancelled.set()
                raise ServiceError(
                    "JOB_CANCELLED", "The job was cancelled or its lease expired.", 409
                )

        lease = threading.Thread(target=heartbeat, name=f"lease-{job_id}", daemon=True)
        lease.start()
        try:
            if job["kind"] == "cleanup":
                phase("CLEANUP")
                keys = self.repository.cleanup_objects(job_id, token)
                for index, key in enumerate(keys):
                    phase("CLEANUP", index)
                    self.storage.delete(key)
                phase("CLEANUP", len(keys))
                self.repository.complete_cleanup(job_id, token)
            else:
                chunks = self.processor.process(job, phase, cancelled)
                phase("PUBLISHING", len(chunks))
                self.repository.publish_chunks(
                    job_id,
                    token,
                    chunks,
                    embedding_model=self.settings.embedding_model,
                    embedding_revision=self.settings.embedding_revision,
                    embedding_dimension=self.settings.embedding_dimension,
                )
            logger.info(
                "Worker job completed", extra={"job_id": job_id, "kind": job["kind"]}
            )
        except Exception as exc:  # noqa: BLE001 -- persist classified job errors.
            code, message, retryable = classify_failure(exc)
            if code in {"JOB_CANCELLED", "STALE_JOB_CLAIM"} or cancelled.is_set():
                logger.info("Worker job lease ended", extra={"job_id": job_id})
            else:
                try:
                    self.repository.fail_job(
                        job_id,
                        token,
                        code,
                        message,
                        retryable=retryable,
                        max_attempts=self.settings.worker_max_attempts,
                    )
                except ServiceError as failure:
                    if failure.code != "STALE_JOB_CLAIM":
                        raise
                # Do not log exception strings: upstream errors may contain document text.
                logger.warning(
                    "Worker job failed",
                    extra={
                        "job_id": job_id,
                        "error_code": code,
                        "retryable": retryable,
                    },
                )
        finally:
            stopped.set()
            lease.join(timeout=min(1.0, self.settings.worker_lease_seconds / 3))
        return True

    def run(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event if stop_event is not None else threading.Event()
        next_maintenance = 0.0
        try:
            while not stop_event.is_set():
                try:
                    processed = self.run_once()
                except Exception:  # noqa: BLE001 -- daemon survives dependency outages.
                    logger.error(
                        "Worker queue unavailable; retrying after polling interval"
                    )
                    processed = False
                if time.monotonic() >= next_maintenance:
                    try:
                        self.maintenance.run_once()
                    except Exception:  # noqa: BLE001 -- retry maintenance on the next interval.
                        logger.warning(
                            "Worker maintenance failed; retrying on the next interval"
                        )
                    next_maintenance = time.monotonic() + getattr(
                        self.settings, "maintenance_interval_seconds", 3600
                    )
                if not processed:
                    stop_event.wait(self.settings.worker_poll_seconds)
        finally:
            self.maintenance.close()
