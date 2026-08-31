from __future__ import annotations

import asyncio
import os
import signal
import logging

from apps.platform_api.main import create_app
from packages.operations.worker_metrics import worker_metrics


def critical_tasks(services) -> dict[str, asyncio.Task]:
    """Every permanent Worker loop must stay alive until shutdown begins."""
    tasks = {}
    for name, service in (("runtime", services.orchestrator), ("knowledge", services.knowledge)):
        tasks[name + ".consumer"] = service.task
        tasks[name + ".reconcile"] = service.reconcile_task
        tasks[name + ".heartbeat"] = service.worker_lease.task
    tasks["sandbox.cleanup"] = services.sandbox_manager._cleanup_task
    tasks["health.refresh"] = services.health.task
    if getattr(services, 'metrics_task', None) is not None:
        tasks['metrics.listener'] = services.metrics_task
    if any(not isinstance(task, asyncio.Task) for task in tasks.values()):
        raise RuntimeError("Worker startup did not create every critical task")
    return tasks


async def supervise(services, stopped: asyncio.Event) -> None:
    tasks = critical_tasks(services)
    stop_task = asyncio.create_task(stopped.wait())
    try:
        done, _ = await asyncio.wait([stop_task, *tasks.values()], return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            return
        ended = []
        for name, task in tasks.items():
            if task in done:
                ended.append(name)
                if not task.cancelled():
                    task.exception()  # Retrieve without logging possibly sensitive exception text.
        raise RuntimeError("Worker critical tasks stopped: " + ", ".join(ended))
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        # The application's AsyncExitStack owns consumers, leases and resources.


async def run() -> None:
    os.environ["DEEPAGENT_PROCESS_ROLE"] = "worker"
    application = create_app()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    try:
        for signal_name in signals:
            loop.add_signal_handler(signal_name, stopped.set)
        async with application.router.lifespan_context(application):
            async with worker_metrics(application.state.services):
                await supervise(application.state.services, stopped)
    finally:
        for signal_name in signals:
            loop.remove_signal_handler(signal_name)


if __name__ == "__main__":
    from packages.operations.logging import configure_logging
    configure_logging()
    try:
        asyncio.run(run())
    except Exception:
        logging.getLogger(__name__).exception('Worker failed', extra={'telemetry_event': 'worker.failed'})
        raise SystemExit(1) from None
