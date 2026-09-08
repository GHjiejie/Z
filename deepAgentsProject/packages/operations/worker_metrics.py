"""TLS management listener supervised with the Worker; no business API routes."""
import asyncio
import os
from contextlib import asynccontextmanager, contextmanager

import uvicorn

from packages.operations.http import WorkerMetricsApp


@asynccontextmanager
async def worker_metrics(services):
    production = os.getenv('DEEPAGENT_ENVIRONMENT', 'development').lower() in {'production', 'prod'}
    port = int(os.getenv('DEEPAGENT_WORKER_METRICS_PORT', '0'))
    if not 0 <= port <= 65535:
        raise ValueError('Invalid Worker metrics port')
    if port == 0:
        if production:
            raise ValueError('Production Worker requires a TLS metrics listener')
        yield
        return
    observer = services.db.telemetry
    cert = os.getenv('DEEPAGENT_METRICS_TLS_CERT_FILE')
    key = os.getenv('DEEPAGENT_METRICS_TLS_KEY_FILE')
    if not observer.settings.metrics_token or bool(cert) != bool(key) or production and not (cert and key):
        raise ValueError('Worker metrics requires a collector token and production TLS certificate/key')

    class ManagedServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            yield  # Worker owns SIGTERM/SIGINT; management HTTP must not replace them.

    server = ManagedServer(uvicorn.Config(WorkerMetricsApp(observer),
        host=os.getenv('DEEPAGENT_WORKER_METRICS_HOST', '127.0.0.1'), port=port,
        ssl_certfile=cert, ssl_keyfile=key, lifespan='off', access_log=False,
        proxy_headers=False, server_header=False, log_config=None,
        limit_concurrency=8, timeout_keep_alive=2, timeout_graceful_shutdown=3))

    async def serve():
        try:
            await server.serve()
        except SystemExit as error:
            raise RuntimeError('Worker metrics listener could not start') from None

    task = services.metrics_task = asyncio.create_task(serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError('Worker metrics listener ended during startup')
                await asyncio.sleep(.01)
        yield
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), 5)
        except BaseException:
            server.force_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        services.metrics_task = None
