import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from apps.platform_worker import main
from packages.runtime.orchestrator import RunOrchestrator
from packages.runtime.worker_lease import WorkerLease
from packages.sandbox.manager import SandboxManager


@pytest.fixture
async def worker_tasks():
    def permanent():
        return asyncio.create_task(asyncio.Event().wait())

    def consumer():
        return SimpleNamespace(task=permanent(), reconcile_task=permanent(),
                               worker_lease=SimpleNamespace(task=permanent()))

    services = SimpleNamespace(orchestrator=consumer(), knowledge=consumer(),
        sandbox_manager=SimpleNamespace(_cleanup_task=permanent()), health=SimpleNamespace(task=permanent()))
    tasks = main.critical_tasks(services)
    try:
        yield services
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.parametrize('component', ['runtime.consumer', 'runtime.reconcile', 'runtime.heartbeat',
    'knowledge.consumer', 'knowledge.reconcile', 'knowledge.heartbeat', 'sandbox.cleanup', 'health.refresh'])
@pytest.mark.asyncio
async def test_every_critical_loop_is_supervised(worker_tasks, component):
    stopped = asyncio.Event()
    supervisor = asyncio.create_task(main.supervise(worker_tasks, stopped))
    main.critical_tasks(worker_tasks)[component].cancel()
    with pytest.raises(RuntimeError, match=component):
        await asyncio.wait_for(supervisor, 1)


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.asyncio
async def test_unexpected_normal_or_exceptional_exit_fails_worker(worker_tasks, failure):
    worker_tasks.orchestrator.task.cancel()
    await asyncio.gather(worker_tasks.orchestrator.task, return_exceptions=True)

    async def finish():
        if failure:
            raise ValueError('synthetic-private-credential')

    task = worker_tasks.orchestrator.task = asyncio.create_task(finish())
    try:
        with pytest.raises(RuntimeError, match='runtime.consumer') as error:
            await asyncio.wait_for(main.supervise(worker_tasks, asyncio.Event()), 1)
        assert 'synthetic-private-credential' not in str(error.value)
        assert error.value.__cause__ is None
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_signal_stop_does_not_take_ownership_of_application_tasks(worker_tasks):
    stopped = asyncio.Event()
    stopped.set()
    await main.supervise(worker_tasks, stopped)
    assert all(not task.done() for task in main.critical_tasks(worker_tasks).values())


@pytest.mark.asyncio
async def test_enabled_metrics_listener_is_also_supervised(worker_tasks):
    task = worker_tasks.metrics_task = asyncio.create_task(asyncio.Event().wait())
    try:
        task.cancel()
        with pytest.raises(RuntimeError, match='metrics.listener'):
            await main.supervise(worker_tasks, asyncio.Event())
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_loop_fails_startup(worker_tasks):
    saved = worker_tasks.health.task
    worker_tasks.health.task = None
    try:
        with pytest.raises(RuntimeError, match='every critical task'):
            await main.supervise(worker_tasks, asyncio.Event())
    finally:
        worker_tasks.health.task = saved


@pytest.mark.asyncio
async def test_worker_failure_exits_lifespan_and_removes_signal_handlers(worker_tasks, monkeypatch):
    cleaned = []

    @asynccontextmanager
    async def lifespan(application):
        try:
            worker_tasks.knowledge.task.cancel()
            yield
        finally:
            cleaned.append('lifespan')

    app = SimpleNamespace(state=SimpleNamespace(services=worker_tasks),
                          router=SimpleNamespace(lifespan_context=lifespan))
    monkeypatch.setattr(main, 'create_app', lambda: app)
    monkeypatch.setenv('DEEPAGENT_PROCESS_ROLE', 'api')
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'add_signal_handler', lambda *args: None)
    monkeypatch.setattr(loop, 'remove_signal_handler', lambda sig: cleaned.append(sig))
    with pytest.raises(RuntimeError, match='knowledge.consumer'):
        await asyncio.wait_for(main.run(), 1)
    assert cleaned == ['lifespan', main.signal.SIGINT, main.signal.SIGTERM]


@pytest.mark.asyncio
async def test_failed_tasks_do_not_prevent_lease_and_manager_cleanup():
    async def broken():
        raise RuntimeError('synthetic failure')

    stopped = []

    async def stop_lease():
        stopped.append('lease')

    orchestrator = object.__new__(RunOrchestrator)
    orchestrator.task, orchestrator.reconcile_task = asyncio.create_task(broken()), asyncio.create_task(broken())
    orchestrator.worker_lease = SimpleNamespace(stop=stop_lease)
    await asyncio.sleep(0)
    await orchestrator.stop()
    assert stopped == ['lease']
    assert orchestrator.task is orchestrator.reconcile_task is None

    lease = WorkerLease(SimpleNamespace(current_time=lambda: SimpleNamespace(isoformat=lambda: 'now'),
        execute=lambda *args: stopped.append('offline')), 'worker', 'runtime', {})
    lease.task = asyncio.create_task(broken())
    await asyncio.sleep(0)
    await lease.stop()
    assert stopped[-1] == 'offline'
    assert lease.task is None

    manager = object.__new__(SandboxManager)
    manager._stop_cleanup = asyncio.Event()
    manager._cleanup_task = asyncio.create_task(broken())
    await asyncio.sleep(0)
    await manager.stop()
    assert manager._cleanup_task is None
