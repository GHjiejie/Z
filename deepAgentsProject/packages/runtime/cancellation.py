"""Durable cancellation finalization, independent of revoked agent execution."""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta

from packages.coding.errors import SandboxUnavailableError
from packages.persistence.fencing import CancellationWriteFence, LeaseLostError, execution_scope
from packages.runtime.coding_recovery import CodingRecovery
from packages.sandbox.cancellation_capture import validate_capture


class CancellationFinalizer:
    lease_seconds = 30
    timeout_seconds = 120

    def __init__(self, executor):
        self.executor = executor
        self.db, self.events = executor.db, executor.events

    def claim(self, run_id):
        with self.db.transaction():
            lock = ' FOR UPDATE' if self.db.dialect == 'postgresql' else ''
            run = self.db.fetch_one('SELECT * FROM runs WHERE id=?' + lock, (run_id,))
            if not run or run['status'] != 'CANCELLING':
                return None
            now = self.db.current_time()
            # Also covers cancellation initiated by current authorization loss.
            self.db.execute('UPDATE run_attempts SET lease_token=NULL, expires_at=NULL WHERE id=?',
                (run['current_attempt_id'],))
            workspace = self.db.fetch_one('SELECT * FROM coding_workspaces WHERE id=?', (run['coding_workspace_id'],))
            if not workspace:
                raise SandboxUnavailableError('Cancellation workspace metadata is missing')
            self.db.execute("""INSERT INTO run_cancellations
                (run_id,attempt_id,workspace_id,sandbox_instance_id,workspace_generation,status,
                 available_at,created_at,updated_at) VALUES(?,?,?,?,?,'PENDING',?,?,?)
                 ON CONFLICT(run_id) DO NOTHING""",
                (run_id,run['current_attempt_id'],workspace['id'],workspace.get('sandbox_instance_id'),
                 workspace['workspace_generation'],now.isoformat(),now.isoformat(),now.isoformat()))
            job = self.db.fetch_one('SELECT * FROM run_cancellations WHERE run_id=?', (run_id,))
            if (job['status'] == 'COMPLETED' or datetime.fromisoformat(job['available_at']) > now
                    or job['status'] == 'RUNNING' and job['expires_at'] and datetime.fromisoformat(job['expires_at']) > now):
                return None
            if (job['attempt_id'] != run['current_attempt_id'] or job['workspace_id'] != workspace['id']
                    or job['sandbox_instance_id'] != workspace.get('sandbox_instance_id')
                    or job['workspace_generation'] != workspace['workspace_generation']):
                raise LeaseLostError('Cancellation workspace binding changed')
            token = secrets.token_hex(32)
            self.db.execute("""UPDATE run_cancellations SET status='RUNNING', worker_id=?, lease_token=?,
                expires_at=?,attempts=attempts+1,updated_at=? WHERE run_id=?""",
                (self.executor.worker_id,token,(now+timedelta(seconds=self.lease_seconds)).isoformat(),now.isoformat(),run_id))
            self.events.append(run_id, 'run.cancellation.finalization.started', {'attempt': job['attempts'] + 1})
            return CancellationWriteFence(run_id, run['current_attempt_id'], self.executor.worker_id, token)

    async def run(self, run_id):
        fence = self.claim(run_id)
        if fence is None:
            return
        operation = asyncio.create_task(self._process(fence))
        heartbeat = asyncio.create_task(self._heartbeat(fence))
        error = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                done, _ = await asyncio.wait((operation, heartbeat), return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done:
                    await heartbeat
                    raise LeaseLostError('Cancellation heartbeat ended unexpectedly')
                await operation
        except BaseException as exc:
            error = exc
        finally:
            operation.cancel()
            heartbeat.cancel()
            await asyncio.gather(operation, heartbeat, return_exceptions=True)
        if error is not None:
            self._retry(fence, error)
            if isinstance(error, asyncio.CancelledError):
                raise error

    async def _heartbeat(self, fence):
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            with execution_scope(fence), self.db.transaction():
                self.db.execute('UPDATE run_cancellations SET expires_at=? WHERE run_id=?',
                    ((self.db.current_time()+timedelta(seconds=self.lease_seconds)).isoformat(),fence.run_id))

    def _retry(self, fence, error):
        with self.db.transaction():
            lock = ' FOR UPDATE' if self.db.dialect == 'postgresql' else ''
            run = self.db.fetch_one('SELECT status FROM runs WHERE id=?' + lock, (fence.run_id,))
            job = self.db.fetch_one('SELECT * FROM run_cancellations WHERE run_id=?', (fence.run_id,))
            if not run or run['status'] != 'CANCELLING' or not job or job['lease_token'] != fence.lease_token:
                return
            now = self.db.current_time()
            delay = min(60, 2 ** min(job['attempts'], 6))
            # Class names are safe diagnostic codes; transport exception text
            # can contain credentials and is never persisted or returned here.
            code = type(error).__name__
            self.db.execute("""UPDATE run_cancellations SET status='PENDING',lease_token=NULL,expires_at=NULL,
                last_error=?,available_at=?,updated_at=? WHERE run_id=?""",
                (code,(now+timedelta(seconds=delay)).isoformat(),now.isoformat(),fence.run_id))
            self.events.append(fence.run_id,'run.cancellation.finalization.retrying',
                {'code': code, 'retry_after_seconds': delay})

    async def _process(self, fence):
        from packages.operations.telemetry import task_operation
        with task_operation(self.db, 'run', fence.run_id, 'runtime.cancellation', attempt_id=fence.attempt_id), execution_scope(fence):
            self.db.assert_execution_fence()
            run = self.db.fetch_one('SELECT * FROM runs WHERE id=?', (fence.run_id,))
            workspace = self.executor._workspace(run['coding_workspace_id'])
            instance = self.db.fetch_one('SELECT * FROM sandbox_instances WHERE id=?',
                (workspace.get('sandbox_instance_id'),))
            if not instance or not instance.get('external_id'):
                # No executable workspace was published. Do not fabricate a diff.
                with self.db.transaction():
                    self._complete(fence, None, None)
                return
            manager = self.executor.sandbox_manager
            provider = manager.providers[instance['provider']]
            capture = await provider.capture_cancellation(instance['external_id'], instance['profile'])
            await asyncio.to_thread(validate_capture, capture)
            self.db.assert_execution_fence()
            plan = self.db.fetch_one('SELECT plan_json FROM resolved_execution_plans WHERE id=?',
                (run['resolved_plan_id'],))['plan']
            snapshot = await manager.prepare_snapshot(workspace, run=run, plan=plan,
                snapshot=capture.snapshot, reason='run_cancelled', recovery=True)
            with self.db.transaction():
                manager.record_snapshot(snapshot)
                source = self.db.fetch_one('SELECT * FROM repository_snapshots WHERE id=?',
                    (workspace['repository_snapshot_id'],))
                # Cancellation never runs project verification scripts or reuses
                # a previous generation's PASSED report as current evidence.
                report = self.executor.verification.partial(run, workspace, reason='run_cancelled')
                changes = self.executor.changesets.build_captured(run, workspace, source, capture.changes,
                    report, plan.get('coding_profile') or {}, plan_hash=plan['plan_hash'], review_reason='run_cancelled')
                self.executor._create_standard_artifacts(run, plan,
                    'Execution was cancelled. Preserved changes have not been verified.', report, changes)
                point = CodingRecovery.publish_cancelled(self.db, self.events, self.executor.checkpointer,
                    run, plan, snapshot, fence)
                self._complete(fence, snapshot['id'], point['id'])

    def _complete(self, fence, snapshot_id, point_id):
        self.events.append(fence.run_id,'run.cancellation.finalization.completed',
            {'workspace_snapshot_id': snapshot_id, 'recovery_point_id': point_id,
             'artifacts': 'preserved' if snapshot_id else 'no_workspace'})
        # Last write in this fenced transaction. The original Run/Attempt lease
        # remains NULL; no agent execution permission has been reintroduced.
        self.db.execute("""UPDATE run_cancellations SET status='COMPLETED',lease_token=NULL,expires_at=NULL,
            last_error=NULL,workspace_snapshot_id=?,recovery_point_id=?,updated_at=? WHERE run_id=?""",
            (snapshot_id,point_id,self.db.current_time().isoformat(),fence.run_id))
