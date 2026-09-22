"""Transactional state, worker fencing and authorization before both recall paths.

PostgreSQL is the deployment database. SQLite supports deterministic unit tests;
it is intentionally not a production retrieval or concurrency implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from sqlalchemy import (
    and_,
    create_engine,
    delete,
    event,
    exists,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.pool import StaticPool

from llamaindex_service.contracts import AuthContext, ServiceError

from . import models as m

_OBJECT_LOCK = threading.RLock()


def _id() -> str:
    return uuid.uuid4().hex


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _scope(table, ctx):
    return and_(
        table.c.tenant_id == ctx.tenant_id, table.c.project_id == ctx.project_id
    )


def _base(ctx, entity_id=None):
    return {
        "id": entity_id or _id(),
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "created_at": time.time(),
    }


def _missing():
    raise ServiceError("NOT_FOUND", "资源不存在或无权访问", 404)


class Repository:
    def __init__(self, settings, embedding_dimension: int | None = None):
        if isinstance(settings, str):
            settings = SimpleNamespace(
                database_url=settings,
                embedding_dimension=embedding_dimension or 32,
                embedding_model="local-hash-v1",
                embedding_revision="1",
                environment="development",
                conversation_retention_days=30,
            )
        self.settings = settings
        self.dimension = settings.embedding_dimension
        self.model = settings.embedding_model
        self.revision = settings.embedding_revision
        kwargs = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            kwargs.update(connect_args={"check_same_thread": False})
            if ":memory:" in settings.database_url:
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(settings.database_url, **kwargs)
        self.postgres = self.engine.dialect.name == "postgresql"
        if not self.postgres:

            @event.listens_for(self.engine, "connect")
            def foreign_keys(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def _tx(self, ctx=None):
        with self.engine.begin() as connection:
            if self.postgres and ctx:
                connection.execute(
                    text(
                        "SELECT set_config('rag.tenant_id', :tenant, true), set_config('rag.project_id', :project, true)"
                    ),
                    {"tenant": ctx.tenant_id, "project": ctx.project_id},
                )
            yield connection

    def create_schema(self) -> None:
        """Explicit migration entry; never called by application startup."""
        from llamaindex_service.migrations import upgrade

        upgrade(self.engine)

    def health(self, worker=False) -> bool:
        with self._tx() as connection:
            version = connection.execute(
                select(m.schema_version.c.version)
            ).scalar_one()
            if version != 2:
                return False
            if self.postgres and self.settings.environment == "production":
                unsafe = connection.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls OR EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'rag_documents' AND tableowner = current_user) FROM pg_roles WHERE rolname = current_user"
                    )
                ).scalar_one()
                if unsafe:
                    raise ServiceError(
                        "UNSAFE_DATABASE_ROLE",
                        "生产数据库账号不能是表所有者、超级用户或 BYPASSRLS",
                        503,
                    )
                worker_role = connection.execute(
                    text("SELECT pg_has_role(current_user, 'rag_worker', 'member')")
                ).scalar_one()
                if worker_role != worker:
                    raise ServiceError(
                        "UNSAFE_DATABASE_ROLE",
                        "API 与 Worker 必须使用各自独立的数据库角色",
                        503,
                    )
            return True

    def _audit(self, connection, ctx, action, target_id, details=None):
        connection.execute(
            insert(m.audit).values(
                **_base(ctx),
                user_id=ctx.user_id,
                action=action,
                target_id=target_id,
                details=details or {},
            )
        )

    def ensure_membership(self, ctx: AuthContext) -> None:
        """Administrative bootstrap only. Never derive membership from JWT claims."""
        with self._tx(ctx) as connection:
            row = connection.execute(
                select(m.memberships).where(
                    _scope(m.memberships, ctx), m.memberships.c.user_id == ctx.user_id
                )
            ).first()
            if row:
                connection.execute(
                    update(m.memberships)
                    .where(
                        _scope(m.memberships, ctx),
                        m.memberships.c.user_id == ctx.user_id,
                    )
                    .values(active=True, roles=list(ctx.roles))
                )
            else:
                connection.execute(
                    insert(m.memberships).values(
                        tenant_id=ctx.tenant_id,
                        project_id=ctx.project_id,
                        user_id=ctx.user_id,
                        active=True,
                        roles=list(ctx.roles),
                    )
                )

    def get_membership(self, ctx) -> dict | None:
        with self._tx(ctx) as connection:
            row = (
                connection.execute(
                    select(m.memberships).where(
                        _scope(m.memberships, ctx),
                        m.memberships.c.user_id == ctx.user_id,
                        m.memberships.c.active.is_(True),
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def resolve_membership(self, ctx):
        row = self.get_membership(ctx)
        return tuple(row["roles"]) if row else None

    def has_membership(self, ctx) -> bool:
        return self.get_membership(ctx) is not None

    def _kb_predicate(self, ctx):
        return exists(
            select(1).where(
                _scope(m.kb_permissions, ctx),
                m.kb_permissions.c.kb_id == m.knowledge_bases.c.id,
                m.kb_permissions.c.user_id == ctx.user_id,
            )
        )

    def _kb(
        self, connection, ctx, kb_id, write=False, owner=False, active=False, lock=False
    ):
        statement = (
            select(m.knowledge_bases, m.kb_permissions.c.role)
            .join(
                m.kb_permissions,
                and_(
                    m.kb_permissions.c.kb_id == m.knowledge_bases.c.id,
                    _scope(m.kb_permissions, ctx),
                    m.kb_permissions.c.user_id == ctx.user_id,
                ),
            )
            .where(_scope(m.knowledge_bases, ctx), m.knowledge_bases.c.id == kb_id)
        )
        if lock:
            statement = statement.with_for_update(of=m.knowledge_bases)
        row = connection.execute(statement).mappings().first()
        if not row:
            _missing()
        if (
            owner
            and row["role"] != "owner"
            or write
            and row["role"] not in ("owner", "editor")
        ):
            raise ServiceError("FORBIDDEN", "该操作需要更高的知识库权限", 403)
        if active and row["status"] != "active":
            raise ServiceError("KNOWLEDGE_BASE_DISABLED", "知识库已停用", 409)
        return dict(row)

    def create_kb(self, ctx, name, description="") -> dict:
        kb_id, generation_id = _id(), _id()
        with self._tx(ctx) as connection:
            connection.execute(
                insert(m.knowledge_bases).values(
                    **_base(ctx, kb_id),
                    name=name,
                    description=description,
                    status="active",
                    active_generation_id=generation_id,
                )
            )
            connection.execute(
                insert(m.kb_permissions).values(
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    kb_id=kb_id,
                    user_id=ctx.user_id,
                    role="owner",
                )
            )
            connection.execute(
                insert(m.generations).values(
                    **_base(ctx, generation_id),
                    kb_id=kb_id,
                    embedding_model=self.model,
                    embedding_revision=self.revision,
                    embedding_dimension=self.dimension,
                    status="active",
                )
            )
            self._audit(connection, ctx, "knowledge_base.create", kb_id)
            return self._kb(connection, ctx, kb_id)

    def get_kb(self, ctx, kb_id) -> dict:
        with self._tx(ctx) as connection:
            return self._kb(connection, ctx, kb_id)

    def assert_kb_write(self, ctx, kb_id) -> dict:
        with self._tx(ctx) as connection:
            return self._kb(connection, ctx, kb_id, write=True, active=True)

    def list_kbs(self, ctx, limit=50, offset=0) -> list[dict]:
        with self._tx(ctx) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(m.knowledge_bases)
                    .where(_scope(m.knowledge_bases, ctx), self._kb_predicate(ctx))
                    .order_by(m.knowledge_bases.c.created_at, m.knowledge_bases.c.id)
                    .limit(limit)
                    .offset(offset)
                ).mappings()
            ]

    def update_kb(self, ctx, kb_id, name=None, status=None, description=None) -> dict:
        with self._tx(ctx) as connection:
            self._kb(connection, ctx, kb_id, owner=True)
            values = {
                key: value
                for key, value in {
                    "name": name,
                    "status": status,
                    "description": description,
                }.items()
                if value is not None
            }
            if status is not None and status not in ("active", "disabled"):
                raise ServiceError("INVALID_STATUS", "知识库状态无效")
            if values:
                connection.execute(
                    update(m.knowledge_bases)
                    .where(
                        _scope(m.knowledge_bases, ctx), m.knowledge_bases.c.id == kb_id
                    )
                    .values(**values)
                )
            self._audit(
                connection,
                ctx,
                "knowledge_base.update",
                kb_id,
                {"fields": sorted(values)},
            )
            return self._kb(connection, ctx, kb_id)

    def get_kb_permissions(self, ctx, kb_id) -> list[dict]:
        with self._tx(ctx) as connection:
            self._kb(connection, ctx, kb_id, owner=True)
            return [
                dict(row)
                for row in connection.execute(
                    select(m.kb_permissions.c.user_id, m.kb_permissions.c.role).where(
                        _scope(m.kb_permissions, ctx), m.kb_permissions.c.kb_id == kb_id
                    )
                ).mappings()
            ]

    def set_kb_permissions(self, ctx, kb_id, permissions) -> dict:
        if isinstance(permissions, dict):
            permissions = [
                {"user_id": user_id, "role": role}
                for user_id, role in permissions.items()
            ]
        if not any(item["role"] == "owner" for item in permissions) or any(
            item["role"] not in ("owner", "editor", "reader") for item in permissions
        ):
            raise ServiceError(
                "INVALID_PERMISSIONS",
                "权限必须包含至少一位 owner，角色仅支持 owner/editor/reader",
            )
        if len({item["user_id"] for item in permissions}) != len(permissions):
            raise ServiceError("INVALID_PERMISSIONS", "知识库成员不能重复")
        with self._tx(ctx) as connection:
            self._kb(connection, ctx, kb_id, owner=True, lock=True)
            connection.execute(
                delete(m.kb_permissions).where(
                    _scope(m.kb_permissions, ctx), m.kb_permissions.c.kb_id == kb_id
                )
            )
            connection.execute(
                insert(m.kb_permissions),
                [
                    {
                        "tenant_id": ctx.tenant_id,
                        "project_id": ctx.project_id,
                        "kb_id": kb_id,
                        **item,
                    }
                    for item in permissions
                ],
            )
            self._audit(connection, ctx, "knowledge_base.permissions", kb_id)
            return {"kb_id": kb_id, "members": permissions}

    def _doc_acl(self, ctx):
        user_access = exists(
            select(1).where(
                _scope(m.document_permissions, ctx),
                m.document_permissions.c.document_id == m.documents.c.id,
                m.document_permissions.c.user_id == ctx.user_id,
            )
        )
        # JSON arrays need dialect-specific containment; every role remains a bound parameter.
        role_access = []
        for role in ctx.roles:
            if self.postgres:
                role_access.append(
                    text(
                        "CAST(rag_documents.allowed_roles AS jsonb) ? :acl_role_"
                        + str(len(role_access))
                    ).bindparams(**{"acl_role_" + str(len(role_access)): role})
                )
            else:
                role_access.append(
                    exists(
                        select(1)
                        .select_from(
                            func.json_each(m.documents.c.allowed_roles).table_valued(
                                "value"
                            )
                        )
                        .where(
                            text(
                                "value = :acl_role_" + str(len(role_access))
                            ).bindparams(**{"acl_role_" + str(len(role_access)): role})
                        )
                    )
                )
        return or_(m.documents.c.acl_restricted.is_(False), user_access, *role_access)

    def _doc_statement(self, ctx):
        return (
            select(m.documents)
            .join(m.knowledge_bases, m.knowledge_bases.c.id == m.documents.c.kb_id)
            .where(
                _scope(m.documents, ctx),
                _scope(m.knowledge_bases, ctx),
                self._kb_predicate(ctx),
                self._doc_acl(ctx),
                m.documents.c.deleted_at.is_(None),
            )
        )

    def _doc(self, connection, ctx, document_id, write=False, lock=False):
        statement = self._doc_statement(ctx).where(m.documents.c.id == document_id)
        if lock:
            statement = statement.with_for_update(of=m.documents)
        row = connection.execute(statement).mappings().first()
        if not row:
            _missing()
        self._kb(connection, ctx, row["kb_id"], write=write)
        return dict(row)

    def get_document(self, ctx, document_id) -> dict:
        with self._tx(ctx) as connection:
            return self._doc(connection, ctx, document_id)

    def list_documents(self, ctx, kb_id, limit=50, offset=0) -> list[dict]:
        with self._tx(ctx) as connection:
            self._kb(connection, ctx, kb_id)
            rows = (
                connection.execute(
                    self._doc_statement(ctx)
                    .where(m.documents.c.kb_id == kb_id)
                    .order_by(m.documents.c.created_at, m.documents.c.id)
                    .limit(limit)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
            result = []
            for row in rows:
                item = dict(row)
                latest_job = (
                    connection.execute(
                        select(m.jobs.c.id, m.jobs.c.status, m.jobs.c.phase)
                        .where(_scope(m.jobs, ctx), m.jobs.c.document_id == item["id"])
                        .order_by(m.jobs.c.created_at.desc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                item["latest_job"] = dict(latest_job) if latest_job else None
                result.append(item)
            return result

    def set_document_permissions(
        self, ctx, document_id, user_ids=None, allowed_users=None, allowed_roles=None
    ) -> dict:
        users = list(
            dict.fromkeys(
                allowed_users if allowed_users is not None else user_ids or []
            )
        )
        roles = list(dict.fromkeys(allowed_roles or []))
        with self._tx(ctx) as connection:
            document = self._doc(connection, ctx, document_id, lock=True)
            self._kb(connection, ctx, document["kb_id"], owner=True)
            connection.execute(
                delete(m.document_permissions).where(
                    _scope(m.document_permissions, ctx),
                    m.document_permissions.c.document_id == document_id,
                )
            )
            if users:
                connection.execute(
                    insert(m.document_permissions),
                    [
                        {
                            "tenant_id": ctx.tenant_id,
                            "project_id": ctx.project_id,
                            "document_id": document_id,
                            "user_id": user_id,
                        }
                        for user_id in users
                    ],
                )
            connection.execute(
                update(m.documents)
                .where(_scope(m.documents, ctx), m.documents.c.id == document_id)
                .values(acl_restricted=bool(users or roles), allowed_roles=roles)
            )
            self._audit(connection, ctx, "document.permissions", document_id)
            return {
                "document_id": document_id,
                "allowed_users": users,
                "allowed_roles": roles,
            }

    def _idempotency_lock(self, connection, ctx, operation, key):
        if self.postgres:
            lock_id = int.from_bytes(
                hashlib.sha256(
                    f"{ctx.tenant_id}:{ctx.project_id}:{ctx.user_id}:{operation}:{key}".encode()
                ).digest()[:8],
                signed=True,
            )
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id}
            )

    def _replay(self, connection, ctx, operation, key, fingerprint):
        if not key:
            return None
        self._idempotency_lock(connection, ctx, operation, key)
        row = (
            connection.execute(
                select(m.idempotency).where(
                    _scope(m.idempotency, ctx),
                    m.idempotency.c.user_id == ctx.user_id,
                    m.idempotency.c.operation == operation,
                    m.idempotency.c.key == key,
                )
            )
            .mappings()
            .first()
        )
        if row and row["fingerprint"] != fingerprint:
            raise ServiceError(
                "IDEMPOTENCY_CONFLICT", "相同幂等键不能用于不同请求", 409
            )
        return dict(row["result"]) if row else None

    def _remember(self, connection, ctx, operation, key, fingerprint, result):
        if key:
            connection.execute(
                insert(m.idempotency).values(
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    user_id=ctx.user_id,
                    operation=operation,
                    key=key,
                    fingerprint=fingerprint,
                    result=result,
                )
            )

    def upload_version(
        self,
        ctx,
        kb_id,
        filename,
        object_key,
        sha256,
        size,
        content_type,
        document_id=None,
        idempotency_key=None,
        object_created_at=None,
    ) -> dict:
        fingerprint = _fingerprint(
            [kb_id, filename, sha256, size, content_type, document_id]
        )
        with _OBJECT_LOCK, self._tx(ctx) as connection:
            self._object_lock(connection, object_key)
            if object_created_at is not None and time.time() - object_created_at > 3600:
                raise ServiceError(
                    "UPLOAD_EXPIRED", "上传保存超过一小时，请重新上传", 409
                )
            kb = self._kb(connection, ctx, kb_id, write=True, active=True, lock=True)
            # Uploads may have waited on object/knowledge-base locks after HTTP
            # authentication. Reload membership only once those waits are over.
            ctx = self._fresh_context(connection, ctx)
            if connection.execute(
                select(m.generations.c.id).where(
                    _scope(m.generations, ctx),
                    m.generations.c.kb_id == kb_id,
                    m.generations.c.status == "building",
                )
            ).first():
                raise ServiceError(
                    "INDEX_REBUILD_IN_PROGRESS",
                    "索引重建期间暂停上传和替换，完成或取消后恢复",
                    409,
                )
            if document_id:
                document = self._doc(
                    connection, ctx, document_id, write=True, lock=True
                )
                ctx = self._fresh_context(connection, ctx)
                document = self._doc(connection, ctx, document_id, write=True)
                if document["kb_id"] != kb_id:
                    _missing()
            replay = self._replay(
                connection, ctx, "upload", idempotency_key, fingerprint
            )
            if replay:
                self._doc(connection, ctx, replay["document_id"], write=True)
                return {**replay, "replayed": True}
            if document_id:
                sequence = document["update_sequence"] + 1
                connection.execute(
                    update(m.documents)
                    .where(_scope(m.documents, ctx), m.documents.c.id == document_id)
                    .values(update_sequence=sequence)
                )
            else:
                document_id, sequence = _id(), 1
                connection.execute(
                    insert(m.documents).values(
                        **_base(ctx, document_id),
                        kb_id=kb_id,
                        filename=filename,
                        update_sequence=sequence,
                        acl_restricted=False,
                        allowed_roles=[],
                    )
                )
            generation = (
                connection.execute(
                    select(m.generations).where(
                        _scope(m.generations, ctx),
                        m.generations.c.id == kb["active_generation_id"],
                    )
                )
                .mappings()
                .one()
            )
            self._check_generation(generation)
            version_id, job_id = _id(), _id()
            connection.execute(
                insert(m.versions).values(
                    **_base(ctx, version_id),
                    document_id=document_id,
                    generation_id=generation["id"],
                    sequence=sequence,
                    filename=filename,
                    object_key=object_key,
                    sha256=sha256,
                    size_bytes=size,
                    content_type=content_type,
                    status="QUEUED",
                )
            )
            connection.execute(
                insert(m.jobs).values(
                    **_base(ctx, job_id),
                    kb_id=kb_id,
                    document_id=document_id,
                    version_id=version_id,
                    kind="ingest",
                    status="QUEUED",
                    phase="VALIDATING",
                    completed=0,
                    attempts=0,
                    available_at=time.time(),
                )
            )
            result = {
                "document_id": document_id,
                "version_id": version_id,
                "job_id": job_id,
                "status": "QUEUED",
                "replayed": False,
            }
            self._remember(
                connection, ctx, "upload", idempotency_key, fingerprint, result
            )
            self._audit(
                connection,
                ctx,
                "document.upload",
                document_id,
                {"version_id": version_id, "job_id": job_id},
            )
            return result

    def get_content(self, ctx, document_id, version_id) -> dict:
        with self._tx(ctx) as connection:
            ctx = self._fresh_context(connection, ctx)
            document = self._doc(connection, ctx, document_id)
            self._kb(connection, ctx, document["kb_id"], active=True)
            row = (
                connection.execute(
                    select(m.versions).where(
                        _scope(m.versions, ctx),
                        m.versions.c.document_id == document_id,
                        m.versions.c.id == version_id,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                _missing()
            self._audit(
                connection,
                ctx,
                "document.download",
                document_id,
                {"version_id": version_id},
            )
            return dict(row)

    def delete_document(self, ctx, document_id) -> dict:
        with self._tx(ctx) as connection:
            document = self._doc(connection, ctx, document_id, write=True, lock=True)
            connection.execute(
                update(m.documents)
                .where(_scope(m.documents, ctx), m.documents.c.id == document_id)
                .values(deleted_at=time.time(), active_version_id=None)
            )
            connection.execute(
                update(m.jobs)
                .where(
                    _scope(m.jobs, ctx),
                    m.jobs.c.document_id == document_id,
                    m.jobs.c.status.in_(["QUEUED", "RUNNING", "RETRY_WAIT"]),
                )
                .values(status="CANCELLED", claim_token=None, lease_until=None)
            )
            job_id = _id()
            connection.execute(
                insert(m.jobs).values(
                    **_base(ctx, job_id),
                    kb_id=document["kb_id"],
                    document_id=document_id,
                    kind="cleanup",
                    status="QUEUED",
                    phase="CLEANUP",
                    completed=0,
                    attempts=0,
                    available_at=time.time(),
                )
            )
            self._audit(
                connection, ctx, "document.delete", document_id, {"job_id": job_id}
            )
            return {"document_id": document_id, "job_id": job_id, "status": "QUEUED"}

    def get_job(self, ctx, job_id) -> dict:
        with self._tx(ctx) as connection:
            row = (
                connection.execute(
                    select(m.jobs).where(_scope(m.jobs, ctx), m.jobs.c.id == job_id)
                )
                .mappings()
                .first()
            )
            if not row:
                _missing()
            self._kb(connection, ctx, row["kb_id"])
            # Deleted document cleanup remains visible to writers, but never to revoked document readers.
            document = (
                connection.execute(
                    select(m.documents).where(
                        _scope(m.documents, ctx),
                        m.documents.c.id == row["document_id"],
                        self._doc_acl(ctx),
                    )
                )
                .mappings()
                .first()
            )
            if not document:
                _missing()
            return {
                key: value
                for key, value in row.items()
                if key not in ("claim_token", "worker_id")
            }

    def retry_job(self, ctx, job_id, idempotency_key=None) -> dict:
        with self._tx(ctx) as connection:
            row = (
                connection.execute(
                    select(m.jobs)
                    .where(_scope(m.jobs, ctx), m.jobs.c.id == job_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not row:
                _missing()
            self._kb(connection, ctx, row["kb_id"], write=True)
            if row["kind"] == "ingest":
                self._kb(connection, ctx, row["kb_id"], write=True, lock=True)
                if connection.execute(
                    select(m.generations.c.id).where(
                        _scope(m.generations, ctx),
                        m.generations.c.kb_id == row["kb_id"],
                        m.generations.c.status == "building",
                    )
                ).first():
                    raise ServiceError(
                        "INDEX_REBUILD_IN_PROGRESS", "索引重建期间暂停摄取重试", 409
                    )
                document = self._doc(connection, ctx, row["document_id"], write=True)
                version = (
                    connection.execute(
                        select(m.versions).where(
                            m.versions.c.id == row["version_id"],
                            _scope(m.versions, ctx),
                        )
                    )
                    .mappings()
                    .one()
                )
                if version["sequence"] != document["update_sequence"]:
                    raise ServiceError(
                        "SUPERSEDED_VERSION",
                        "该版本已有后续替换任务，不能重新发布",
                        409,
                    )
            replay = self._replay(
                connection, ctx, "retry", idempotency_key, _fingerprint(job_id)
            )
            if replay:
                return {**replay, "replayed": True}
            if row["status"] != "FAILED":
                raise ServiceError("JOB_NOT_RETRYABLE", "只有失败任务可以手动重试", 409)
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == job_id)
                .values(
                    status="QUEUED",
                    attempts=0,
                    error_code=None,
                    error_message=None,
                    available_at=time.time(),
                    claim_token=None,
                    lease_until=None,
                )
            )
            result = {"job_id": job_id, "status": "QUEUED", "replayed": False}
            self._remember(
                connection, ctx, "retry", idempotency_key, _fingerprint(job_id), result
            )
            self._audit(connection, ctx, "job.retry", job_id)
            return result

    def _claim(self, connection, job_id, claim_token, lock=False):
        statement = select(m.jobs).where(
            m.jobs.c.id == job_id,
            m.jobs.c.status == "RUNNING",
            m.jobs.c.claim_token == claim_token,
            m.jobs.c.lease_until > time.time(),
        )
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        if not row:
            raise ServiceError("STALE_JOB_CLAIM", "任务租约已失效", 409)
        return dict(row)

    def claim_job(self, worker_id, lease_seconds=120) -> dict | None:
        with self._tx() as connection:
            now = time.time()
            statement = (
                select(m.jobs)
                .outerjoin(m.versions, m.versions.c.id == m.jobs.c.version_id)
                .outerjoin(
                    m.generations,
                    m.generations.c.id
                    == func.coalesce(
                        m.jobs.c.generation_id, m.versions.c.generation_id
                    ),
                )
                .where(
                    or_(
                        and_(
                            m.jobs.c.status.in_(["QUEUED", "RETRY_WAIT"]),
                            m.jobs.c.available_at <= now,
                        ),
                        and_(m.jobs.c.status == "RUNNING", m.jobs.c.lease_until <= now),
                    ),
                    or_(
                        m.jobs.c.kind == "cleanup",
                        and_(
                            m.generations.c.embedding_model == self.model,
                            m.generations.c.embedding_revision == self.revision,
                            m.generations.c.embedding_dimension == self.dimension,
                        ),
                    ),
                )
                .order_by(m.jobs.c.available_at, m.jobs.c.created_at)
                .limit(1)
                .with_for_update(skip_locked=True, of=m.jobs)
            )
            row = connection.execute(statement).mappings().first()
            if not row:
                return None
            token = _id()
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == row["id"])
                .values(
                    status="RUNNING",
                    claim_token=token,
                    worker_id=worker_id,
                    lease_until=now + lease_seconds,
                    attempts=row["attempts"] + 1,
                )
            )
            result = {
                **dict(row),
                "job_id": row["id"],
                "status": "RUNNING",
                "claim_token": token,
                "attempts": row["attempts"] + 1,
            }
            if row["version_id"]:
                version = (
                    connection.execute(
                        select(m.versions).where(m.versions.c.id == row["version_id"])
                    )
                    .mappings()
                    .one()
                )
                generation = (
                    connection.execute(
                        select(m.generations).where(
                            m.generations.c.id
                            == (row["generation_id"] or version["generation_id"])
                        )
                    )
                    .mappings()
                    .one()
                )
                result.update(
                    {
                        key: version[key]
                        for key in (
                            "object_key",
                            "filename",
                            "sha256",
                            "size_bytes",
                            "content_type",
                            "generation_id",
                        )
                    }
                )
                result.update(
                    {
                        key: generation[key]
                        for key in (
                            "embedding_model",
                            "embedding_revision",
                            "embedding_dimension",
                        )
                    }
                )
                result["generation_id"] = generation["id"]
                if row["kind"] == "ingest":
                    connection.execute(
                        update(m.versions)
                        .where(m.versions.c.id == version["id"])
                        .values(status="RUNNING")
                    )
            return result

    def heartbeat(self, job_id, claim_token, lease_seconds=120) -> bool:
        with self._tx() as connection:
            result = connection.execute(
                update(m.jobs)
                .where(
                    m.jobs.c.id == job_id,
                    m.jobs.c.claim_token == claim_token,
                    m.jobs.c.status == "RUNNING",
                    m.jobs.c.lease_until > time.time(),
                )
                .values(lease_until=time.time() + lease_seconds)
            )
            return result.rowcount == 1

    def job_phase(self, job_id, claim_token, phase, completed=0) -> bool:
        with self._tx() as connection:
            result = connection.execute(
                update(m.jobs)
                .where(
                    m.jobs.c.id == job_id,
                    m.jobs.c.claim_token == claim_token,
                    m.jobs.c.status == "RUNNING",
                    m.jobs.c.lease_until > time.time(),
                )
                .values(phase=phase, completed=completed)
            )
            return result.rowcount == 1

    def _check_generation(self, generation):
        if (
            generation["embedding_model"],
            generation["embedding_revision"],
            generation["embedding_dimension"],
        ) != (self.model, self.revision, self.dimension):
            raise ServiceError(
                "INDEX_MODEL_MISMATCH",
                "活动索引与配置的 Embedding 不匹配，请构建新索引后再切换",
                409,
            )

    def publish_chunks(
        self,
        job_id,
        claim_token,
        chunks,
        embedding_model=None,
        embedding_revision=None,
        embedding_dimension=None,
    ) -> dict:
        with self._tx() as connection:
            job = self._claim(connection, job_id, claim_token)
            document = (
                connection.execute(
                    select(m.documents)
                    .where(m.documents.c.id == job["document_id"])
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            # Deletion also locks document then jobs; preserve that order to avoid
            # publish/delete deadlocks. Recheck the fenced lease after waiting.
            job = self._claim(connection, job_id, claim_token, lock=True)
            version = (
                connection.execute(
                    select(m.versions).where(m.versions.c.id == job["version_id"])
                )
                .mappings()
                .one()
            )
            generation = (
                connection.execute(
                    select(m.generations).where(
                        m.generations.c.id
                        == (job["generation_id"] or version["generation_id"])
                    )
                )
                .mappings()
                .one()
            )
            self._check_generation(generation)
            if any(
                value is not None and value != generation[key]
                for key, value in {
                    "embedding_model": embedding_model,
                    "embedding_revision": embedding_revision,
                    "embedding_dimension": embedding_dimension,
                }.items()
            ):
                raise ServiceError(
                    "INDEX_MODEL_MISMATCH", "Worker Embedding 配置不匹配", 409
                )
            superseded = (
                document["active_version_id"] != version["id"]
                if job["kind"] == "reindex"
                else document["update_sequence"] != version["sequence"]
            )
            if (
                document["deleted_at"]
                or superseded
                or (job["kind"] == "reindex" and generation["status"] != "building")
            ):
                connection.execute(
                    update(m.jobs)
                    .where(m.jobs.c.id == job_id)
                    .values(
                        status="CANCELLED",
                        error_code="SUPERSEDED_VERSION",
                        claim_token=None,
                        lease_until=None,
                    )
                )
                if job["kind"] == "ingest":
                    connection.execute(
                        update(m.versions)
                        .where(m.versions.c.id == version["id"])
                        .values(status="CANCELLED")
                    )
                return {"job_id": job_id, "status": "CANCELLED"}
            if not chunks:
                raise ServiceError("EMPTY_DOCUMENT", "没有可以发布的文本片段", 422)
            ctx = AuthContext(job["tenant_id"], job["project_id"], "worker")
            rows = []
            for index, chunk in enumerate(chunks):
                vector = chunk["embedding"]
                if (
                    len(vector) != self.dimension
                    or not all(math.isfinite(value) for value in vector)
                    or not any(value != 0 for value in vector)
                ):
                    raise ServiceError(
                        "INVALID_EMBEDDING", "Embedding 维度或数值无效", 422
                    )
                chunk_id = (
                    chunk.get("id")
                    or hashlib.sha256(
                        f"{version['id']}:{generation['id']}:{index}".encode()
                    ).hexdigest()
                )
                rows.append(
                    {
                        **_base(ctx, chunk_id),
                        "kb_id": job["kb_id"],
                        "document_id": document["id"],
                        "version_id": version["id"],
                        "generation_id": generation["id"],
                        "text": chunk["text"],
                        "lexical_text": chunk.get("lexical_text", chunk["text"]),
                        "embedding": vector,
                        "source_metadata": chunk.get("metadata", {}),
                    }
                )
            connection.execute(
                delete(m.chunks).where(
                    m.chunks.c.version_id == version["id"],
                    m.chunks.c.generation_id == generation["id"],
                )
            )
            connection.execute(insert(m.chunks), rows)
            if job["kind"] == "reindex":
                connection.execute(
                    update(m.jobs)
                    .where(m.jobs.c.id == job_id)
                    .values(
                        status="READY",
                        phase="PUBLISHING",
                        completed=len(rows),
                        claim_token=None,
                        lease_until=None,
                    )
                )
                return {"job_id": job_id, "status": "READY", "chunk_count": len(rows)}
            connection.execute(
                update(m.documents)
                .where(
                    m.documents.c.id == document["id"],
                    m.documents.c.update_sequence == version["sequence"],
                    m.documents.c.deleted_at.is_(None),
                )
                .values(active_version_id=version["id"], filename=version["filename"])
            )
            connection.execute(
                update(m.versions)
                .where(m.versions.c.id == version["id"])
                .values(status="READY")
            )
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == job_id)
                .values(
                    status="READY",
                    phase="PUBLISHING",
                    completed=len(rows),
                    claim_token=None,
                    lease_until=None,
                )
            )
            self._audit(
                connection,
                ctx,
                "document.publish",
                document["id"],
                {"version_id": version["id"], "chunks": len(rows)},
            )
            return {"job_id": job_id, "status": "READY", "chunk_count": len(rows)}

    def fail_job(
        self, job_id, claim_token, error_code, message, retryable=False, max_attempts=3
    ) -> dict:
        with self._tx() as connection:
            job = self._claim(connection, job_id, claim_token, lock=True)
            retry = retryable and job["attempts"] < max_attempts
            state = "RETRY_WAIT" if retry else "FAILED"
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == job_id)
                .values(
                    status=state,
                    available_at=time.time() + min(300, 2 ** job["attempts"]),
                    error_code=error_code[:64],
                    error_message=message[:500],
                    claim_token=None,
                    lease_until=None,
                )
            )
            if job["version_id"] and job["kind"] == "ingest":
                connection.execute(
                    update(m.versions)
                    .where(m.versions.c.id == job["version_id"])
                    .values(status=state)
                )
            return {"job_id": job_id, "status": state}

    def cleanup_objects(self, job_id, claim_token) -> list[str]:
        with self._tx() as connection:
            job = self._claim(connection, job_id, claim_token)
            if job["kind"] != "cleanup":
                raise ServiceError("INVALID_JOB", "不是清理任务", 409)
            return list(
                connection.execute(
                    select(m.versions.c.object_key).where(
                        m.versions.c.document_id == job["document_id"]
                    )
                ).scalars()
            )

    def complete_cleanup(self, job_id, claim_token) -> dict:
        with self._tx() as connection:
            job = self._claim(connection, job_id, claim_token, lock=True)
            if job["kind"] != "cleanup":
                raise ServiceError("INVALID_JOB", "不是清理任务", 409)
            connection.execute(
                delete(m.chunks).where(m.chunks.c.document_id == job["document_id"])
            )
            connection.execute(
                update(m.versions)
                .where(m.versions.c.document_id == job["document_id"])
                .values(status="DELETED")
            )
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == job_id)
                .values(status="READY", completed=1, claim_token=None, lease_until=None)
            )
            return {"job_id": job_id, "status": "READY"}

    def _fresh_context(self, connection, ctx):
        row = (
            connection.execute(
                select(m.memberships).where(
                    _scope(m.memberships, ctx),
                    m.memberships.c.user_id == ctx.user_id,
                    m.memberships.c.active.is_(True),
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise ServiceError("MEMBERSHIP_REVOKED", "项目成员身份已失效", 403)
        return AuthContext(
            ctx.tenant_id, ctx.project_id, ctx.user_id, tuple(row["roles"])
        )

    def _candidate_statement(self, ctx):
        return (
            select(
                m.chunks.c.id,
                m.chunks.c.kb_id,
                m.chunks.c.document_id,
                m.chunks.c.version_id,
                m.chunks.c.generation_id,
                m.chunks.c.text,
                m.chunks.c.source_metadata,
                m.documents.c.filename.label("title"),
            )
            .select_from(
                m.chunks.join(m.documents, m.documents.c.id == m.chunks.c.document_id)
                .join(m.knowledge_bases, m.knowledge_bases.c.id == m.chunks.c.kb_id)
                .join(m.versions, m.versions.c.id == m.chunks.c.version_id)
            )
            .where(
                _scope(m.chunks, ctx),
                _scope(m.documents, ctx),
                _scope(m.knowledge_bases, ctx),
                _scope(m.versions, ctx),
                self._kb_predicate(ctx),
                self._doc_acl(ctx),
                m.documents.c.deleted_at.is_(None),
                m.knowledge_bases.c.status == "active",
                m.documents.c.kb_id == m.knowledge_bases.c.id,
                m.versions.c.document_id == m.documents.c.id,
                m.documents.c.active_version_id == m.chunks.c.version_id,
                m.knowledge_bases.c.active_generation_id == m.chunks.c.generation_id,
                m.versions.c.generation_id == m.chunks.c.generation_id,
                m.versions.c.status == "READY",
            )
        )

    @staticmethod
    def _candidate(row):
        result = dict(row)
        result["chunk_id"] = result["id"]
        result["metadata"] = result.pop("source_metadata")
        result.pop("embedding", None)
        result.pop("lexical_text", None)
        return result

    def search_candidates(
        self,
        ctx,
        kb_ids,
        document_ids,
        query_vector,
        lexical_query,
        top_k=30,
        filters=None,
    ) -> dict:
        if (
            not kb_ids
            or len(query_vector) != self.dimension
            or not all(math.isfinite(value) for value in query_vector)
            or not any(query_vector)
        ):
            raise ServiceError("INVALID_QUERY", "检索范围或向量无效")
        if not 1 <= top_k <= 100:
            raise ServiceError("INVALID_TOP_K", "召回数量应在 1 至 100 之间")
        filters = filters or {}
        if set(filters) - {"filename", "content_type", "page", "section"}:
            raise ServiceError("INVALID_FILTER", "不支持该元数据过滤字段")
        with self._tx(ctx) as connection:
            ctx = self._fresh_context(connection, ctx)
            for kb_id in kb_ids:
                kb = self._kb(connection, ctx, kb_id, active=True)
                generation = (
                    connection.execute(
                        select(m.generations).where(
                            _scope(m.generations, ctx),
                            m.generations.c.id == kb["active_generation_id"],
                        )
                    )
                    .mappings()
                    .one()
                )
                self._check_generation(generation)
            for document_id in document_ids or []:
                document = self._doc(connection, ctx, document_id)
                if document["kb_id"] not in kb_ids:
                    _missing()
            statement = self._candidate_statement(ctx).where(
                m.chunks.c.kb_id.in_(kb_ids)
            )
            if document_ids:
                statement = statement.where(m.chunks.c.document_id.in_(document_ids))
            for key, value in filters.items():
                if key == "filename":
                    statement = statement.where(m.documents.c.filename == value)
                elif key == "content_type":
                    statement = statement.where(m.versions.c.content_type == value)
                else:
                    statement = statement.where(
                        m.chunks.c.source_metadata[key].as_string() == str(value)
                    )
            if self.postgres:
                # Exact cosine distance over the authorized set guarantees recall under ACL
                # filtering. An ANN index can be introduced after workload validation.
                distance = m.chunks.c.embedding.cosine_distance(query_vector)
                vector_statement = (
                    statement.add_columns((1 - distance).label("score"))
                    .order_by(distance, m.chunks.c.id)
                    .limit(top_k)
                )
                vector_rows = connection.execute(vector_statement).mappings().all()
                lexemes = list(dict.fromkeys(lexical_query.split()))
                lexical_rows = []
                if lexemes:
                    # OR avoids requiring every Chinese bigram or natural-language token.
                    # Each value is escaped as a tsquery lexeme AND passed as a SQL bind.
                    tsquery = " | ".join(
                        "'" + token.replace("\\", "\\\\").replace("'", "''") + "'"
                        for token in lexemes
                    )
                    query = func.to_tsquery("simple", tsquery)
                    search_vector = func.to_tsvector("simple", m.chunks.c.lexical_text)
                    rank = func.ts_rank_cd(search_vector, query)
                    lexical_statement = (
                        statement.add_columns(rank.label("score"))
                        .where(search_vector.op("@@")(query))
                        .order_by(rank.desc(), m.chunks.c.id)
                        .limit(top_k)
                    )
                    lexical_rows = (
                        connection.execute(lexical_statement).mappings().all()
                    )
            else:
                # Offline tests only: SQL still applies complete authorization first.
                rows = (
                    connection.execute(
                        statement.add_columns(
                            m.chunks.c.embedding, m.chunks.c.lexical_text
                        )
                    )
                    .mappings()
                    .all()
                )
                norm = math.sqrt(sum(value * value for value in query_vector))
                vector_rows, lexical_rows = [], []
                terms = set(lexical_query.split())
                for row in rows:
                    candidate = dict(row)
                    vector = candidate["embedding"]
                    candidate["score"] = sum(
                        a * b for a, b in zip(query_vector, vector, strict=True)
                    ) / (norm * math.sqrt(sum(value * value for value in vector)))
                    vector_rows.append(candidate)
                    hits = len(terms & set(candidate["lexical_text"].split()))
                    if hits:
                        lexical_rows.append({**candidate, "score": float(hits)})
                vector_rows = sorted(
                    vector_rows, key=lambda row: (-row["score"], row["id"])
                )[:top_k]
                lexical_rows = sorted(
                    lexical_rows, key=lambda row: (-row["score"], row["id"])
                )[:top_k]
            self._audit(
                connection,
                ctx,
                "retrieval.search",
                "query",
                {
                    "kb_ids": kb_ids,
                    "vector_count": len(vector_rows),
                    "lexical_count": len(lexical_rows),
                },
            )
            return {
                "vector": [self._candidate(row) for row in vector_rows],
                "lexical": [self._candidate(row) for row in lexical_rows],
            }

    def _revalidate(self, connection, ctx, citations, active=True):
        ctx = self._fresh_context(connection, ctx)
        for citation in citations:
            chunk_id = citation.get("chunk_id") or citation.get("id")
            if not chunk_id:
                return False
            statement = self._candidate_statement(ctx).where(m.chunks.c.id == chunk_id)
            if citation.get("version_id"):
                statement = statement.where(
                    m.chunks.c.version_id == citation["version_id"]
                )
            if citation.get("document_id"):
                statement = statement.where(
                    m.chunks.c.document_id == citation["document_id"]
                )
            if citation.get("generation_id"):
                statement = statement.where(
                    m.chunks.c.generation_id == citation["generation_id"]
                )
            if not connection.execute(statement).first():
                return False
        return True

    def revalidate(self, ctx, citations) -> bool:
        with self._tx(ctx) as connection:
            try:
                return self._revalidate(connection, ctx, citations)
            except ServiceError:
                return False

    def create_conversation(self, ctx, kb_ids, title="") -> dict:
        with self._tx(ctx) as connection:
            for kb_id in kb_ids:
                self._kb(connection, ctx, kb_id, active=True)
            result = {
                **_base(ctx),
                "user_id": ctx.user_id,
                "title": title,
                "kb_ids": list(dict.fromkeys(kb_ids)),
                "deleted_at": None,
            }
            connection.execute(insert(m.conversations).values(**result))
            return result

    def _conversation(self, connection, ctx, conversation_id):
        row = (
            connection.execute(
                select(m.conversations).where(
                    _scope(m.conversations, ctx),
                    m.conversations.c.id == conversation_id,
                    m.conversations.c.user_id == ctx.user_id,
                    m.conversations.c.deleted_at.is_(None),
                )
            )
            .mappings()
            .first()
        )
        if not row:
            _missing()
        for kb_id in row["kb_ids"]:
            self._kb(connection, ctx, kb_id, active=True)
        return dict(row)

    def _history(self, connection, ctx, conversation_id):
        self._conversation(connection, ctx, conversation_id)
        cutoff = (
            time.time()
            - getattr(self.settings, "conversation_retention_days", 30) * 86400
        )
        rows = (
            connection.execute(
                select(m.messages)
                .where(
                    _scope(m.messages, ctx),
                    m.messages.c.conversation_id == conversation_id,
                    m.messages.c.created_at >= cutoff,
                )
                .order_by(m.messages.c.created_at, m.messages.c.id)
            )
            .mappings()
            .all()
        )
        return [
            dict(row)
            for row in rows
            if self._revalidate(connection, ctx, row["citations"])
        ]

    def conversation_history(self, ctx, conversation_id) -> list[dict]:
        with self._tx(ctx) as connection:
            return self._history(connection, ctx, conversation_id)

    def get_conversation(self, ctx, conversation_id) -> dict:
        with self._tx(ctx) as connection:
            return {
                **self._conversation(connection, ctx, conversation_id),
                "messages": self._history(connection, ctx, conversation_id),
            }

    def delete_conversation(self, ctx, conversation_id) -> dict:
        with self._tx(ctx) as connection:
            self._conversation(connection, ctx, conversation_id)
            connection.execute(
                update(m.conversations)
                .where(
                    _scope(m.conversations, ctx),
                    m.conversations.c.id == conversation_id,
                )
                .values(deleted_at=time.time())
            )
            connection.execute(
                delete(m.messages).where(
                    _scope(m.messages, ctx),
                    m.messages.c.conversation_id == conversation_id,
                )
            )
            connection.execute(
                update(m.query_runs)
                .where(
                    _scope(m.query_runs, ctx),
                    m.query_runs.c.conversation_id == conversation_id,
                )
                .values(result=None, payload={}, status="deleted")
            )
            self._audit(connection, ctx, "conversation.delete", conversation_id)
            return {"id": conversation_id, "status": "deleted"}

    def _add_turn(self, connection, ctx, conversation_id, question, answer, citations):
        self._conversation(connection, ctx, conversation_id)
        if not self._revalidate(connection, ctx, citations):
            raise ServiceError("EVIDENCE_REVOKED", "回答依赖的证据已不可访问", 409)
        for role, content in (("user", question), ("assistant", answer)):
            connection.execute(
                insert(m.messages).values(
                    **_base(ctx),
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    citations=citations,
                )
            )

    def add_conversation_turn(
        self, ctx, conversation_id, question, answer, citations
    ) -> None:
        with self._tx(ctx) as connection:
            self._add_turn(
                connection, ctx, conversation_id, question, answer, citations
            )

    def check_rate_limit(self, ctx, limit=120, window_seconds=60) -> None:
        window = int(time.time() // window_seconds)
        with self._tx(ctx) as connection:
            self._idempotency_lock(connection, ctx, "rate", "request")
            row = (
                connection.execute(
                    select(m.rate_limits)
                    .where(
                        _scope(m.rate_limits, ctx),
                        m.rate_limits.c.user_id == ctx.user_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row and row["window_start"] == window and row["count"] >= limit:
                raise ServiceError("RATE_LIMITED", "请求频率超限，请稍后重试", 429)
            if row:
                connection.execute(
                    update(m.rate_limits)
                    .where(
                        _scope(m.rate_limits, ctx),
                        m.rate_limits.c.user_id == ctx.user_id,
                    )
                    .values(
                        window_start=window,
                        count=row["count"] + 1 if row["window_start"] == window else 1,
                    )
                )
            else:
                connection.execute(
                    insert(m.rate_limits).values(
                        tenant_id=ctx.tenant_id,
                        project_id=ctx.project_id,
                        user_id=ctx.user_id,
                        window_start=window,
                        count=1,
                    )
                )

    def _query(self, connection, ctx, request_id):
        row = (
            connection.execute(
                select(m.query_runs).where(
                    _scope(m.query_runs, ctx),
                    m.query_runs.c.id == request_id,
                    m.query_runs.c.user_id == ctx.user_id,
                )
            )
            .mappings()
            .first()
        )
        if not row:
            _missing()
        result = dict(row)
        if result["conversation_id"]:
            self._conversation(connection, ctx, result["conversation_id"])
        for kb_id in result["payload"].get("knowledge_base_ids", []):
            self._kb(connection, ctx, kb_id, active=True)
        citations = (result.get("result") or {}).get("citations", [])
        if not self._revalidate(connection, ctx, citations):
            raise ServiceError("EVIDENCE_REVOKED", "已保存回答的来源不可访问", 409)
        result["request_id"] = result["id"]
        return result

    def begin_query(
        self,
        ctx,
        request_id,
        payload,
        idempotency_key=None,
        conversation_id=None,
        max_concurrent=4,
        lease_seconds=300,
    ) -> dict:
        fingerprint = _fingerprint(payload)
        conversation_id = conversation_id or payload.get("conversation_id")
        with self._tx(ctx) as connection:
            ctx = self._fresh_context(connection, ctx)
            # Serializes admission for this user across API replicas, independent of keys.
            self._idempotency_lock(connection, ctx, "query_admission", "user")
            replay = self._replay(
                connection, ctx, "answer", idempotency_key, fingerprint
            )
            if replay:
                row = self._query(connection, ctx, replay["request_id"])
                if row["status"] == "running" and row["lease_until"] <= time.time():
                    connection.execute(
                        update(m.query_runs)
                        .where(m.query_runs.c.id == row["id"])
                        .values(status="interrupted", error_code="QUERY_LEASE_EXPIRED")
                    )
                    row["status"] = "interrupted"
                    row["error_code"] = "QUERY_LEASE_EXPIRED"
                return {**row, "replayed": True}
            if conversation_id:
                conversation = self._conversation(connection, ctx, conversation_id)
                if set(payload.get("knowledge_base_ids", [])) != set(
                    conversation["kb_ids"]
                ):
                    raise ServiceError(
                        "CONVERSATION_SCOPE_MISMATCH", "会话知识库范围与请求不一致", 409
                    )
            for kb_id in payload.get("knowledge_base_ids", []):
                self._kb(connection, ctx, kb_id, active=True)
            for document_id in payload.get("document_ids") or []:
                document = self._doc(connection, ctx, document_id)
                if document["kb_id"] not in payload.get("knowledge_base_ids", []):
                    _missing()
            connection.execute(
                update(m.query_runs)
                .where(
                    _scope(m.query_runs, ctx),
                    m.query_runs.c.user_id == ctx.user_id,
                    m.query_runs.c.status == "running",
                    m.query_runs.c.lease_until <= time.time(),
                )
                .values(status="interrupted", error_code="QUERY_LEASE_EXPIRED")
            )
            running = connection.execute(
                select(func.count())
                .select_from(m.query_runs)
                .where(
                    _scope(m.query_runs, ctx),
                    m.query_runs.c.user_id == ctx.user_id,
                    m.query_runs.c.status == "running",
                )
            ).scalar_one()
            if running >= max_concurrent:
                raise ServiceError("CONCURRENCY_LIMIT", "并发问答数量超限", 429)
            result = {
                **_base(ctx, request_id),
                "user_id": ctx.user_id,
                "conversation_id": conversation_id,
                "status": "running",
                "payload": payload,
                "result": None,
                "error_code": None,
                "lease_until": time.time() + lease_seconds,
            }
            connection.execute(insert(m.query_runs).values(**result))
            self._remember(
                connection,
                ctx,
                "answer",
                idempotency_key,
                fingerprint,
                {"request_id": request_id},
            )
            self._audit(connection, ctx, "answer.begin", request_id)
            return {**result, "request_id": request_id, "replayed": False}

    def heartbeat_query(self, ctx, request_id, lease_seconds=300) -> bool:
        with self._tx(ctx) as connection:
            result = connection.execute(
                update(m.query_runs)
                .where(
                    _scope(m.query_runs, ctx),
                    m.query_runs.c.id == request_id,
                    m.query_runs.c.user_id == ctx.user_id,
                    m.query_runs.c.status == "running",
                    m.query_runs.c.lease_until > time.time(),
                )
                .values(lease_until=time.time() + lease_seconds)
            )
            return result.rowcount == 1

    def finish_query(self, ctx, request_id, status, result, error_code=None) -> dict:
        if status not in (
            "completed",
            "no_answer",
            "invalidated",
            "failed",
            "interrupted",
        ):
            raise ServiceError("INVALID_QUERY_STATUS", "问答状态无效")
        with self._tx(ctx) as connection:
            row = (
                connection.execute(
                    select(m.query_runs)
                    .where(
                        _scope(m.query_runs, ctx),
                        m.query_runs.c.id == request_id,
                        m.query_runs.c.user_id == ctx.user_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not row:
                _missing()
            if row["status"] != "running":
                if row["status"] == "deleted":
                    # Preserve the deletion tombstone; an in-flight response must
                    # still receive an explicit invalidation verdict.
                    return {
                        **dict(row),
                        "request_id": request_id,
                        "result": None,
                        "error_code": "EVIDENCE_REVOKED",
                    }
                return {**dict(row), "request_id": request_id}
            if status in ("completed", "no_answer"):
                try:
                    # Roll back any partial conversation write while allowing the
                    # outer transaction to commit a durable failure verdict.
                    with connection.begin_nested():
                        ctx = self._fresh_context(connection, ctx)
                        for kb_id in row["payload"].get("knowledge_base_ids", []):
                            self._kb(connection, ctx, kb_id, active=True)
                        for document_id in row["payload"].get("document_ids") or []:
                            self._doc(connection, ctx, document_id)
                        if not self._revalidate(
                            connection, ctx, (result or {}).get("citations", [])
                        ):
                            raise ServiceError(
                                "EVIDENCE_REVOKED", "回答依赖的证据已不可访问", 409
                            )
                        if row["lease_until"] <= time.time():
                            status, result, error_code = (
                                "interrupted",
                                None,
                                "QUERY_LEASE_EXPIRED",
                            )
                        elif row["conversation_id"]:
                            self._add_turn(
                                connection,
                                ctx,
                                row["conversation_id"],
                                row["payload"].get("query", ""),
                                (result or {}).get("answer", ""),
                                (result or {}).get("citations", []),
                            )
                except ServiceError as exc:
                    if exc.code not in {
                        "MEMBERSHIP_REVOKED",
                        "NOT_FOUND",
                        "FORBIDDEN",
                        "KNOWLEDGE_BASE_DISABLED",
                        "EVIDENCE_REVOKED",
                    }:
                        raise
                    status, result, error_code = "failed", None, "EVIDENCE_REVOKED"
            connection.execute(
                update(m.query_runs)
                .where(m.query_runs.c.id == request_id)
                .values(status=status, result=result, error_code=error_code)
            )
            self._audit(
                connection, ctx, "answer.finish", request_id, {"status": status}
            )
            return {
                "request_id": request_id,
                "status": status,
                "result": result,
                "error_code": error_code,
            }

    def get_query(self, ctx, request_id) -> dict:
        with self._tx(ctx) as connection:
            row = self._query(connection, ctx, request_id)
            if row["status"] == "running" and row["lease_until"] <= time.time():
                connection.execute(
                    update(m.query_runs)
                    .where(m.query_runs.c.id == request_id)
                    .values(status="interrupted", error_code="QUERY_LEASE_EXPIRED")
                )
                row.update(status="interrupted", error_code="QUERY_LEASE_EXPIRED")
            return row

    def add_feedback(
        self, ctx, request_id, rating, comment=None, category=None
    ) -> dict:
        with self._tx(ctx) as connection:
            self._query(connection, ctx, request_id)
            result = {
                **_base(ctx),
                "request_id": request_id,
                "user_id": ctx.user_id,
                "rating": str(rating),
                "comment": comment,
                "category": category,
            }
            connection.execute(insert(m.feedback).values(**result))
            return result

    def audit_events(self, ctx, limit=100) -> list[dict]:
        if not {"admin", "auditor"} & set(ctx.roles):
            raise ServiceError("FORBIDDEN", "需要审计权限", 403)
        with self._tx(ctx) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(m.audit)
                    .where(_scope(m.audit, ctx))
                    .order_by(m.audit.c.created_at.desc())
                    .limit(limit)
                ).mappings()
            ]

    def purge_expired_history(self) -> int:
        """Administrative retention pass; worker role only under PostgreSQL RLS."""
        cutoff = (
            time.time()
            - getattr(self.settings, "conversation_retention_days", 30) * 86400
        )
        with self._tx() as connection:
            count = connection.execute(
                delete(m.messages).where(m.messages.c.created_at < cutoff)
            ).rowcount
            connection.execute(
                update(m.query_runs)
                .where(m.query_runs.c.created_at < cutoff)
                .values(payload={}, result=None, status="expired")
            )
            return count

    def _object_lock(self, connection, object_key):
        if self.postgres:
            value = int.from_bytes(
                hashlib.sha256(("object:" + object_key).encode()).digest()[:8],
                signed=True,
            )
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": value}
            )

    def live_object_keys(self) -> set[str]:
        with self._tx() as connection:
            return set(
                connection.execute(
                    select(m.versions.c.object_key).where(
                        m.versions.c.status != "DELETED"
                    )
                ).scalars()
            )

    def delete_orphan_object(self, object_key, delete_callback) -> bool:
        """Only use for objects older than 24h; upload finalization expires at 1h."""
        with _OBJECT_LOCK, self._tx() as connection:
            self._object_lock(connection, object_key)
            referenced = connection.execute(
                select(m.versions.c.id)
                .where(
                    m.versions.c.object_key == object_key,
                    m.versions.c.status != "DELETED",
                )
                .limit(1)
            ).first()
            if referenced:
                return False
            delete_callback(object_key)
            return True

    def stage_generation(
        self, ctx, kb_id, embedding_model, embedding_revision, embedding_dimension
    ) -> dict:
        if (
            not embedding_model
            or not embedding_revision
            or not 8 <= embedding_dimension <= 2000
        ):
            raise ServiceError("INVALID_GENERATION", "Embedding 配置无效")
        with self._tx(ctx) as connection:
            self._kb(connection, ctx, kb_id, owner=True, active=True, lock=True)
            if connection.execute(
                select(m.generations.c.id).where(
                    _scope(m.generations, ctx),
                    m.generations.c.kb_id == kb_id,
                    m.generations.c.status == "building",
                )
            ).first():
                raise ServiceError(
                    "INDEX_REBUILD_IN_PROGRESS", "知识库已有正在构建的索引", 409
                )
            if connection.execute(
                select(m.jobs.c.id).where(
                    _scope(m.jobs, ctx),
                    m.jobs.c.kb_id == kb_id,
                    m.jobs.c.kind == "ingest",
                    m.jobs.c.status.in_(["QUEUED", "RUNNING", "RETRY_WAIT"]),
                )
            ).first():
                raise ServiceError(
                    "INGESTION_IN_PROGRESS", "请等待当前上传任务结束后重建索引", 409
                )
            generation_id = _id()
            connection.execute(
                insert(m.generations).values(
                    **_base(ctx, generation_id),
                    kb_id=kb_id,
                    embedding_model=embedding_model,
                    embedding_revision=embedding_revision,
                    embedding_dimension=embedding_dimension,
                    status="building",
                )
            )
            documents = (
                connection.execute(
                    select(m.documents).where(
                        _scope(m.documents, ctx),
                        m.documents.c.kb_id == kb_id,
                        m.documents.c.deleted_at.is_(None),
                        m.documents.c.active_version_id.is_not(None),
                    )
                )
                .mappings()
                .all()
            )
            job_ids = []
            for document in documents:
                job_id = _id()
                job_ids.append(job_id)
                connection.execute(
                    insert(m.jobs).values(
                        **_base(ctx, job_id),
                        kb_id=kb_id,
                        document_id=document["id"],
                        version_id=document["active_version_id"],
                        generation_id=generation_id,
                        kind="reindex",
                        status="QUEUED",
                        phase="VALIDATING",
                        completed=0,
                        attempts=0,
                        available_at=time.time(),
                    )
                )
            self._audit(
                connection,
                ctx,
                "generation.stage",
                generation_id,
                {"kb_id": kb_id, "jobs": len(job_ids)},
            )
            return {
                "generation_id": generation_id,
                "status": "building",
                "job_ids": job_ids,
            }

    def get_generation(self, ctx, kb_id, generation_id) -> dict:
        with self._tx(ctx) as connection:
            self._kb(connection, ctx, kb_id, owner=True)
            row = (
                connection.execute(
                    select(m.generations).where(
                        _scope(m.generations, ctx),
                        m.generations.c.id == generation_id,
                        m.generations.c.kb_id == kb_id,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                _missing()
            jobs = (
                connection.execute(
                    select(
                        m.jobs.c.id,
                        m.jobs.c.document_id,
                        m.jobs.c.version_id,
                        m.jobs.c.status,
                        m.jobs.c.error_code,
                    ).where(
                        _scope(m.jobs, ctx), m.jobs.c.generation_id == generation_id
                    )
                )
                .mappings()
                .all()
            )
            return {
                **dict(row),
                "generation_id": generation_id,
                "jobs": [dict(job) for job in jobs],
            }

    def activate_generation(self, ctx, kb_id, generation_id) -> dict:
        with self._tx(ctx) as connection:
            kb = self._kb(connection, ctx, kb_id, owner=True, lock=True)
            generation = (
                connection.execute(
                    select(m.generations).where(
                        _scope(m.generations, ctx),
                        m.generations.c.id == generation_id,
                        m.generations.c.kb_id == kb_id,
                    )
                )
                .mappings()
                .first()
            )
            if not generation:
                _missing()
            if generation["status"] != "building":
                raise ServiceError(
                    "INVALID_GENERATION_STATE", "仅构建中的索引可以激活", 409
                )
            documents = (
                connection.execute(
                    select(m.documents)
                    .where(
                        _scope(m.documents, ctx),
                        m.documents.c.kb_id == kb_id,
                        m.documents.c.deleted_at.is_(None),
                        m.documents.c.active_version_id.is_not(None),
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            for document in documents:
                job = (
                    connection.execute(
                        select(m.jobs).where(
                            _scope(m.jobs, ctx),
                            m.jobs.c.generation_id == generation_id,
                            m.jobs.c.document_id == document["id"],
                            m.jobs.c.version_id == document["active_version_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                chunk_count = connection.execute(
                    select(func.count())
                    .select_from(m.chunks)
                    .where(
                        _scope(m.chunks, ctx),
                        m.chunks.c.generation_id == generation_id,
                        m.chunks.c.document_id == document["id"],
                        m.chunks.c.version_id == document["active_version_id"],
                    )
                ).scalar_one()
                if (
                    not job
                    or job["status"] != "READY"
                    or chunk_count != job["completed"]
                    or chunk_count == 0
                ):
                    raise ServiceError(
                        "GENERATION_NOT_READY",
                        "所有活动文档均完成并校验后才能切换",
                        409,
                    )
            connection.execute(
                update(m.generations)
                .where(
                    m.generations.c.id == kb["active_generation_id"],
                    _scope(m.generations, ctx),
                )
                .values(status="retired")
            )
            connection.execute(
                update(m.generations)
                .where(m.generations.c.id == generation_id, _scope(m.generations, ctx))
                .values(status="active")
            )
            connection.execute(
                update(m.knowledge_bases)
                .where(m.knowledge_bases.c.id == kb_id, _scope(m.knowledge_bases, ctx))
                .values(active_generation_id=generation_id)
            )
            version_ids = [document["active_version_id"] for document in documents]
            if version_ids:
                connection.execute(
                    update(m.versions)
                    .where(_scope(m.versions, ctx), m.versions.c.id.in_(version_ids))
                    .values(generation_id=generation_id)
                )
            self._audit(
                connection, ctx, "generation.activate", generation_id, {"kb_id": kb_id}
            )
            return {
                "generation_id": generation_id,
                "status": "active",
                "embedding_model": generation["embedding_model"],
                "embedding_revision": generation["embedding_revision"],
                "embedding_dimension": generation["embedding_dimension"],
            }

    def abort_generation(self, ctx, kb_id, generation_id) -> dict:
        with self._tx(ctx) as connection:
            self._kb(connection, ctx, kb_id, owner=True, lock=True)
            generation = (
                connection.execute(
                    select(m.generations).where(
                        _scope(m.generations, ctx),
                        m.generations.c.id == generation_id,
                        m.generations.c.kb_id == kb_id,
                    )
                )
                .mappings()
                .first()
            )
            if not generation:
                _missing()
            if generation["status"] != "building":
                raise ServiceError(
                    "INVALID_GENERATION_STATE", "不能取消活动或已退出的索引", 409
                )
            connection.execute(
                update(m.jobs)
                .where(_scope(m.jobs, ctx), m.jobs.c.generation_id == generation_id)
                .values(status="CANCELLED", claim_token=None, lease_until=None)
            )
            connection.execute(
                delete(m.chunks).where(
                    _scope(m.chunks, ctx), m.chunks.c.generation_id == generation_id
                )
            )
            connection.execute(
                update(m.generations)
                .where(_scope(m.generations, ctx), m.generations.c.id == generation_id)
                .values(status="discarded")
            )
            self._audit(
                connection, ctx, "generation.abort", generation_id, {"kb_id": kb_id}
            )
            return {"generation_id": generation_id, "status": "discarded"}
