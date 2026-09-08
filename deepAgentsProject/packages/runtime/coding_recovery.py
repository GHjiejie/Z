from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import zlib
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver

from packages.application.services import new_id
from packages.coding.errors import CodingConflictError
from packages.content_security import ContentRejectedError
from packages.domain.models import utc_now
from packages.persistence.fencing import RunWriteFence, current_write_fence
from packages.sandbox.recovery_archive import MAX_ARCHIVE_BYTES, normalize_recovery_archive


MAX_GRAPH_BYTES = 32 * 1024 * 1024
MAX_CHECKPOINTS = 2000


class CodingRecovery:
    """Publish a graph/files pair only at a quiescent, synchronous boundary.

    A checkpoint saver write alone is NOT a recovery point. Only the immutable
    pair published here is eligible. Recovery forks all namespaces into a new
    attempt-specific graph session, so later pending writes from an interrupted
    attempt cannot leak into the imported state (including nested SubAgents).
    """

    def __init__(self, db, events, manager, checkpointer, run, plan):
        self.db, self.events, self.manager = db, events, manager
        self.base, self.run, self.plan = checkpointer, run, plan
        self.session = None
        self.source = None

    def _workspace(self):
        result = self.db.fetch_one(
            "SELECT * FROM coding_workspaces WHERE id=? AND tenant_id=? AND project_id=?",
            (self.run["coding_workspace_id"], self.run["tenant_id"], self.run["project_id"]),
        )
        if not result:
            raise CodingConflictError("Recovery workspace is unavailable")
        return result

    @staticmethod
    def _manifest(point, snapshot):
        fields = (
            "id", "tenant_id", "project_id", "run_id", "session_id", "workspace_id",
            "sequence", "plan_hash", "workspace_generation", "base_commit_sha",
            "workspace_snapshot_id", "checkpoint_id", "phase", "graph_sha256", "created_at",
        )
        value = {key: point[key] for key in fields}
        value["archive_sha256"] = snapshot["archive_sha256"]
        value["size_bytes"] = snapshot["size_bytes"]
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def load(self):
        workspace = self._workspace()
        point = self.db.fetch_one(
            "SELECT * FROM coding_recovery_points WHERE workspace_id=? ORDER BY sequence DESC LIMIT 1",
            (workspace["id"],),
        )
        if point is None:
            # A legacy ChangeSet cannot certify a matching LangGraph state.
            legacy = self.db.fetch_one("SELECT id FROM change_sets WHERE workspace_id=? LIMIT 1", (workspace["id"],))
            if int(workspace["workspace_generation"]) or legacy:
                raise CodingConflictError("Workspace has no consistent recovery point; explicit migration is required")
            return None
        snapshot = self.db.fetch_one("SELECT * FROM workspace_snapshots WHERE id=?", (point["workspace_snapshot_id"],))
        source = self.db.fetch_one("SELECT * FROM repository_snapshots WHERE id=?", (workspace["repository_snapshot_id"],))
        if (not snapshot or not source or point["tenant_id"] != self.run["tenant_id"]
                or point["project_id"] != self.run["project_id"] or point["plan_hash"] != self.plan["plan_hash"]
                or point["base_commit_sha"] != source["resolved_commit_sha"]):
            raise CodingConflictError("Recovery point scope or immutable plan mismatch")
        for field in ("tenant_id", "project_id", "workspace_id", "run_id", "plan_hash", "base_commit_sha", "workspace_generation"):
            if snapshot[field] != point[field]:
                raise CodingConflictError("Recovery snapshot binding mismatch")
        if self._manifest(point, snapshot) != point["manifest_hash"]:
            raise CodingConflictError("Recovery point integrity check failed")
        graph = self._decode_graph(point)
        if point['phase'] == 'CANCELLED':
            cancellation = self.db.fetch_one("""SELECT c.status,r.status AS run_status,c.recovery_point_id
                FROM run_cancellations c JOIN runs r ON r.id=c.run_id WHERE c.run_id=?""", (point['run_id'],))
            if (not cancellation or cancellation['status'] != 'COMPLETED' or cancellation['run_status'] != 'CANCELLED'
                    or cancellation['recovery_point_id'] != point['id'] or point['run_id'] == self.run['id']):
                raise CodingConflictError('Cancelled state is only a completed file baseline for a new Run')
        if snapshot["archive_path"].startswith("snapshot-object://"):
            if self.manager.archive_store is None:
                raise CodingConflictError("Recovery object storage is unavailable")
            content = self.manager.archive_store.read(snapshot, kind="workspace")
        else:
            if self.manager.archive_store is not None:
                raise CodingConflictError("Production recovery requires a shared versioned snapshot")
            path = Path(snapshot["archive_path"]).resolve()
            root = self.manager.snapshot_root
            if root not in path.parents or not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
                raise CodingConflictError("Recovery archive path is invalid")
            content = path.read_bytes()
        if len(content) != snapshot["size_bytes"] or hashlib.sha256(content).hexdigest() != snapshot["archive_sha256"]:
            raise CodingConflictError("Recovery archive integrity check failed")
        normalize_recovery_archive(content)
        try:
            self.manager.content_scanner.scan(content, object_name=f"recovery/{point['id']}")
        except ContentRejectedError as exc:
            raise CodingConflictError("Recovery archive was rejected by the content scanner") from exc
        self.source = {"point": point, "snapshot": snapshot, "content": content, "graph": graph}
        return self.source

    def _decode_graph(self, point):
        try:
            envelope = json.loads(point["graph_state"])
            compressed = base64.b64decode(envelope["data"], validate=True)
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, MAX_GRAPH_BYTES + 1)
            if len(raw) > MAX_GRAPH_BYTES or not decoder.eof or decoder.unused_data:
                raise ValueError("Graph state size exceeds the limit")
            digest = hashlib.sha256(envelope["type"].encode() + b"\0" + raw).hexdigest()
            if digest != point["graph_sha256"] or envelope["type"] not in {"msgpack", "json"}:
                raise ValueError("Graph digest or serialization type is invalid")
            graph = self.base.serde.loads_typed((envelope["type"], raw))
            if point['phase'] == 'CANCELLED':
                if graph != {'version': 2, 'terminal': 'CANCELLED', 'records': []}:
                    raise ValueError('Cancelled state must not carry executable graph history')
                return graph
            if graph["version"] != 1 or not 1 <= len(graph["records"]) <= MAX_CHECKPOINTS:
                raise ValueError("Graph recovery format is invalid")
            heads = [record for record in graph["records"] if record["namespace"] == ""
                     and record["checkpoint"]["id"] == point["checkpoint_id"]]
            if len(heads) != 1:
                raise ValueError("Graph recovery head is missing")
            return graph
        except (KeyError, TypeError, ValueError, zlib.error) as exc:
            raise CodingConflictError("Recovery graph integrity check failed") from exc

    @classmethod
    def publish_cancelled(cls, db, events, checkpointer, run, plan, snapshot, fence):
        """Seal files with an explicit empty terminal graph, not a resumable tool call.

        No expired checkpoint table is read or written. Subsequent Runs retain
        files but start a fresh graph, so a cancelled command cannot be replayed.
        The caller commits this marker, artifacts and finalization atomically.
        """
        session = db.fetch_one('SELECT * FROM coding_graph_sessions WHERE attempt_id=?', (fence.attempt_id,))
        if session is None:
            session = {'id': new_id('gsession')}
            db.execute("""INSERT INTO coding_graph_sessions
                (id,tenant_id,project_id,thread_id,run_id,attempt_id,workspace_id,graph_thread_id,plan_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (session['id'],run['tenant_id'],run['project_id'],run['thread_id'],
                run['id'],fence.attempt_id,snapshot['workspace_id'],'cancelled:' + session['id'],plan['plan_hash'],utc_now()))
        kind, raw = checkpointer.serde.dumps_typed({'version': 2, 'terminal': 'CANCELLED', 'records': []})
        sequence = db.fetch_one('SELECT COALESCE(MAX(sequence),0)+1 AS next FROM coding_recovery_points WHERE workspace_id=?',
            (snapshot['workspace_id'],))['next']
        point = {'id': new_id('recovery'), 'tenant_id': run['tenant_id'], 'project_id': run['project_id'],
                 'run_id': run['id'], 'session_id': session['id'], 'workspace_id': snapshot['workspace_id'],
                 'sequence': sequence, 'plan_hash': plan['plan_hash'], 'workspace_generation': snapshot['workspace_generation'],
                 'base_commit_sha': snapshot['base_commit_sha'], 'workspace_snapshot_id': snapshot['id'],
                 'checkpoint_id': 'cancelled:' + run['id'], 'phase': 'CANCELLED',
                 'graph_state': json.dumps({'type': kind, 'data': base64.b64encode(zlib.compress(raw)).decode('ascii')}),
                 'graph_sha256': hashlib.sha256(kind.encode()+b'\0'+raw).hexdigest(), 'created_at': utc_now()}
        point['manifest_hash'] = cls._manifest(point, snapshot)
        columns = list(point)
        db.execute(f"INSERT INTO coding_recovery_points ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(point[column] for column in columns))
        events.append(run['id'],'graph.cancellation.sealed', {'recovery_point_id': point['id'],
            'workspace_snapshot_id': snapshot['id'], 'manifest_hash': point['manifest_hash'], 'executable': False})
        return point

    def begin(self):
        fence = current_write_fence()
        if not isinstance(fence, RunWriteFence) or fence.run_id != self.run["id"]:
            raise CodingConflictError("Recovery requires the current Run execution lease")
        session_id = new_id("gsession")
        graph_thread_id = f"{self.run['tenant_id']}:{self.run['project_id']}:{self.run['thread_id']}:{session_id}"
        self.db.execute(
            """INSERT INTO coding_graph_sessions
               (id, tenant_id, project_id, thread_id, run_id, attempt_id, workspace_id,
                graph_thread_id, plan_hash, source_point_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, self.run["tenant_id"], self.run["project_id"], self.run["thread_id"],
             self.run["id"], fence.attempt_id, self.run["coding_workspace_id"], graph_thread_id,
             self.plan["plan_hash"], self.source["point"]["id"] if self.source else None, utc_now()),
        )
        self.session = self.db.fetch_one("SELECT * FROM coding_graph_sessions WHERE id=?", (session_id,))
        if self.source:
            # Import into an empty namespace. No mutable source checkpoint table
            # is read here: pending writes are exactly those sealed in the pair.
            for record in sorted(self.source["graph"]["records"], key=lambda item: item["checkpoint"]["id"]):
                config = {"configurable": {"thread_id": graph_thread_id, "checkpoint_ns": record["namespace"]}}
                if record["parent_id"]:
                    config["configurable"]["checkpoint_id"] = record["parent_id"]
                checkpoint = record["checkpoint"]
                stored = self.base.put(config, checkpoint, record["metadata"], checkpoint["channel_versions"])
                grouped = {}
                for task_id, channel, value in record["writes"]:
                    grouped.setdefault(task_id, []).append((channel, value))
                for task_id, writes in grouped.items():
                    self.base.put_writes(stored, writes, task_id)
            self.events.append(self.run["id"], "graph.recovery.imported", {
                "recovery_point_id": self.source["point"]["id"], "session_id": session_id,
                "checkpoint_id": self.source["point"]["checkpoint_id"],
                "source_phase": self.source["point"]["phase"],
            })
        return ConsistentCheckpointSaver(self)

    async def capture(self, phase, checkpoint_id=None):
        workspace = self._workspace()
        snapshot = await self.manager.snapshot_workspace(
            workspace, run=self.run, plan=self.plan, reason="graph_recovery_" + phase.lower(), recovery=True,
        )
        return await asyncio.to_thread(self._publish, snapshot, phase, checkpoint_id)

    def _publish(self, snapshot, phase, checkpoint_id):
        config = {"configurable": {"thread_id": self.session["graph_thread_id"]}}
        head = self.base.get_tuple({"configurable": {**config["configurable"], "checkpoint_ns": ""}})
        if head is None or (checkpoint_id is not None and head.checkpoint["id"] != checkpoint_id):
            raise CodingConflictError("Graph head moved outside the synchronous recovery boundary")
        records = []
        for item in self.base.list(config, limit=MAX_CHECKPOINTS + 1):
            records.append({
                "namespace": item.config["configurable"].get("checkpoint_ns", ""),
                "checkpoint": item.checkpoint, "metadata": item.metadata,
                "parent_id": (item.parent_config or {}).get("configurable", {}).get("checkpoint_id"),
                "writes": item.pending_writes or [],
            })
        if not records or len(records) > MAX_CHECKPOINTS:
            raise CodingConflictError("Graph recovery history exceeds the configured safety limit")
        kind, raw = self.base.serde.dumps_typed({"version": 1, "records": records})
        if len(raw) > MAX_GRAPH_BYTES:
            raise CodingConflictError("Graph recovery state exceeds the configured safety limit")
        graph_state = json.dumps({"type": kind, "data": base64.b64encode(zlib.compress(raw)).decode("ascii")})
        with self.db.transaction():
            current = self._workspace()
            if current["workspace_generation"] != snapshot["workspace_generation"]:
                raise CodingConflictError("Workspace changed outside the synchronous recovery boundary")
            sequence = self.db.fetch_one(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next FROM coding_recovery_points WHERE workspace_id=?",
                (snapshot["workspace_id"],),
            )["next"]
            point = {
                "id": new_id("recovery"), "tenant_id": self.run["tenant_id"], "project_id": self.run["project_id"],
                "run_id": self.run["id"], "session_id": self.session["id"], "workspace_id": snapshot["workspace_id"],
                "sequence": sequence, "plan_hash": self.plan["plan_hash"],
                "workspace_generation": snapshot["workspace_generation"], "base_commit_sha": snapshot["base_commit_sha"],
                "workspace_snapshot_id": snapshot["id"], "checkpoint_id": head.checkpoint["id"], "phase": phase,
                "graph_state": graph_state, "graph_sha256": hashlib.sha256(kind.encode() + b"\0" + raw).hexdigest(),
                "created_at": utc_now(),
            }
            point["manifest_hash"] = self._manifest(point, snapshot)
            columns = list(point)
            self.db.execute(
                f"INSERT INTO coding_recovery_points ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(point[column] for column in columns),
            )
            self.events.append(self.run["id"], "graph.recovery.committed", {
                "recovery_point_id": point["id"], "checkpoint_id": point["checkpoint_id"],
                "workspace_snapshot_id": snapshot["id"], "workspace_generation": snapshot["workspace_generation"],
                "manifest_hash": point["manifest_hash"], "phase": phase,
            })
        return point


class ConsistentCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, recovery):
        super().__init__(serde=recovery.base.serde)
        self.recovery, self.base = recovery, recovery.base

    def get_tuple(self, config):
        return self.base.get_tuple(config)

    async def aget_tuple(self, config):
        return await self.base.aget_tuple(config)

    def list(self, config, **kwargs):
        yield from self.base.list(config, **kwargs)

    async def alist(self, config, **kwargs):
        async for item in self.base.alist(config, **kwargs):
            yield item

    def put(self, config, checkpoint, metadata, new_versions):
        raise RuntimeError("Coding graphs require asynchronous synchronous-durability execution")

    async def aput(self, config, checkpoint, metadata, new_versions):
        result = await self.base.aput(config, checkpoint, metadata, new_versions)
        if not config.get("configurable", {}).get("checkpoint_ns") and metadata.get("step", -1) >= 0:
            await self.recovery.capture("CHECKPOINT", checkpoint["id"])
        return result

    def put_writes(self, config, writes, task_id, task_path=""):
        return self.base.put_writes(config, writes, task_id, task_path)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self.base.aput_writes(config, writes, task_id, task_path)

    def get_next_version(self, current, channel):
        return self.base.get_next_version(current, channel)

    def get_delta_channel_history(self, *, config, channels):
        return self.base.get_delta_channel_history(config=config, channels=channels)

    async def aget_delta_channel_history(self, *, config, channels):
        return await self.base.aget_delta_channel_history(config=config, channels=channels)
