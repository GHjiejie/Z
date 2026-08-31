from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from apps.platform_api.security import EnterpriseSecurityMiddleware, SecuritySettings
from apps.platform_api.web_console import mount_web_console
from apps.platform_api.native_api.knowledge_routes import router as knowledge_router
from apps.platform_api.native_api.auth_routes import router as auth_router
from apps.platform_api.native_api.coding_routes import router as coding_router
from apps.platform_api.native_api.repository_routes import router as repository_router
from apps.platform_api.native_api.routing_routes import router as routing_router
from apps.platform_api.native_api.routes import router as native_router
from apps.platform_api.native_api.evaluation_routes import router as evaluation_router
from apps.platform_api.native_api.billing_routes import router as billing_router
from apps.platform_api.native_api.model_routes import router as model_router
from apps.platform_api.native_api.release_routes import router as release_router
from packages.runtime.model_registry import ModelRegistry
from packages.billing.service import BillingService
from packages.billing.errors import BudgetExceeded, BillingConfigurationError
from packages.application.approval_service import ApprovalService
from packages.auth import (
    AuthAuthorizationError,
    AuthenticationError,
    AuthConflictError,
    AuthNotFoundError,
    AuthRateLimitError,
    AuthService,
    AuthValidationError,
)
from packages.application.services import (
    AgentService,
    ConflictError,
    NotFoundError,
    seed_reference_data,
)
from packages.compiler import AgentPlanCompiler
from packages.persistence.pagination import InvalidCursor, PageAccessChanged
from packages.config import load_environment, read_environment
from packages.content_security import create_content_scanner
from packages.coding.errors import (
    CodingConflictError,
    CodingError,
    CodingNotFoundError,
    SandboxUnavailableError,
)
from packages.coding.service import CodingService
from packages.coding.models import SandboxProfileSpec
from packages.knowledge.embedding import create_embedding_provider
from packages.knowledge.errors import (
    KnowledgeConflictError,
    KnowledgeError,
    KnowledgeNotFoundError,
)
from packages.knowledge.service import KnowledgeService
from packages.knowledge.storage import create_object_storage
from packages.persistence import create_database
from packages.plugins import PluginLoader, SkillRegistry
from packages.repositories import RepositoryService
from packages.repositories.network import RepositoryNetworkPolicy
from packages.routing import IntentRoutingService
from packages.runtime import RunOrchestrator, RunService
from packages.runtime.coding_model import close_coding_chat_model, create_coding_chat_model
from packages.runtime.deepagents_executor import DeepAgentsRuntimeExecutor
from packages.runtime.event_emitter import EventEmitter
from packages.runtime.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    OpenAICompatibleModelGateway,
)
from packages.runtime.task_queue import create_task_queue
from packages.sandbox import DockerSandboxProvider, RemoteSandboxProvider, SandboxManager
from packages.sandbox.ports import SandboxProvider
from packages.secrets import read_secret

from langchain_core.language_models.chat_models import BaseChatModel
from packages.runtime.checkpoint_saver import FencedCheckpointSaver
from packages.persistence.archive_store import SharedArchiveStore
from packages.evaluations.service import EvaluationService
from packages.operations.health import HealthMonitor
from packages.operations.telemetry import Telemetry, TelemetrySettings
from packages.operations.http import TelemetryMiddleware, MetricsResponse
from packages.runtime.admission import CapacityExceeded


logger = logging.getLogger(__name__)


def create_app(
    database_path: str | None = None,
    seed: bool = True,
    model_gateway: ModelGateway | None = None,
    load_env: bool = True,
    trust_identity_headers: bool | None = None,
    allow_demo_identity: bool | None = None,
    coding_model: BaseChatModel | None = None,
    sandbox_providers: Iterable[SandboxProvider] | None = None,
) -> FastAPI:
    root = Path(__file__).resolve().parents[2]
    file_environment, loaded_environment_files = (
        read_environment(root) if load_env else ({}, [])
    )

    def configured(name: str, default: str) -> str:
        return os.environ.get(name, file_environment.get(name, default))

    security = SecuritySettings.from_environment(file_environment)
    resolved_trust_identity_headers = (
        trust_identity_headers
        if trust_identity_headers is not None
        else (
            not load_env
            or configured("DEEPAGENT_TRUST_IDENTITY_HEADERS", "false").lower()
            in {"1", "true", "yes"}
        )
    )
    resolved_allow_demo_identity = (
        allow_demo_identity
        if allow_demo_identity is not None
        else (
            not load_env
            or configured("DEEPAGENT_ALLOW_DEMO_IDENTITY", "false").lower()
            in {"1", "true", "yes"}
        )
    )
    raw_identity_header_secret = configured("DEEPAGENT_IDENTITY_HEADER_SECRET", "").strip()
    raw_bootstrap_password = configured(
        "DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD", "Console1@"
    )
    resolved_process_role = configured("DEEPAGENT_PROCESS_ROLE", "all").strip().lower()

    @asynccontextmanager
    async def application_lifespan(application: FastAPI):
        resources: AsyncExitStack = application.state.resource_cleanup
        if load_env:
            load_environment(root)
        bootstrap_file = configured(
            "DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD_FILE", ""
        ).strip()
        identity_secret_file = configured(
            "DEEPAGENT_IDENTITY_HEADER_SECRET_FILE", ""
        ).strip()
        # Validate the complete production baseline before opening databases or
        # starting workers. File-backed values are represented only for the
        # presence checks and are read after validation.
        bootstrap_candidate = (
            "file-backed-secret" if bootstrap_file else raw_bootstrap_password
        )
        identity_candidate = (
            "file-backed-secret" if identity_secret_file else raw_identity_header_secret
        )
        security.validate_startup(
            bootstrap_password=bootstrap_candidate,
            cookie_secure=configured(
                "DEEPAGENT_SESSION_COOKIE_SECURE", "false"
            ).lower()
            in {"1", "true", "yes"},
            allow_demo_identity=resolved_allow_demo_identity,
            trust_identity_headers=resolved_trust_identity_headers,
            identity_header_secret=identity_candidate or None,
        )
        bootstrap_password = read_secret(
            "DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD",
            values={
                **file_environment,
                "DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD": raw_bootstrap_password,
            },
            required=True,
            production=security.production,
        )
        identity_header_secret = read_secret(
            "DEEPAGENT_IDENTITY_HEADER_SECRET",
            values={
                **file_environment,
                "DEEPAGENT_IDENTITY_HEADER_SECRET": raw_identity_header_secret,
            },
            required=resolved_trust_identity_headers and security.production,
            production=security.production,
        ) or None
        application.state.trust_identity_headers = resolved_trust_identity_headers
        application.state.allow_demo_identity = resolved_allow_demo_identity
        application.state.identity_header_secret = identity_header_secret
        application.state.security = security
        database_location = database_path or read_secret(
            "DATABASE_URL", values=file_environment, production=security.production
        ) or configured(
            "DEEPAGENT_DB_PATH", str(root / "data" / "deepagent.db")
        )
        postgres_enabled = database_location.startswith(("postgresql://", "postgres://"))
        process_role = resolved_process_role
        if process_role not in {"all", "api", "worker"}:
            raise RuntimeError("DEEPAGENT_PROCESS_ROLE must be all, api, or worker")
        if security.production and (not postgres_enabled or process_role == "all"):
            raise RuntimeError(
                "Production requires PostgreSQL and separate api/worker process roles"
            )
        data_root = (
            Path(configured("DEEPAGENT_DATA_DIR", str(root / "data")))
            if postgres_enabled
            else Path(database_location).parent
        )
        db = create_database(database_location)
        resources.callback(db.close)
        from packages.operations.disaster_recovery import assert_not_recovery_database
        assert_not_recovery_database(db)
        auth = AuthService(db)
        auto_migrate = configured(
            "DEEPAGENT_AUTO_MIGRATE", "false" if security.production else "true"
        ).lower() in {"1", "true", "yes"}
        if security.production and auto_migrate:
            raise RuntimeError("Production schema migrations must run as a separate release job")
        db.initialize(auto_migrate=auto_migrate)
        telemetry = Telemetry(TelemetrySettings.from_environment(process_role, file_environment, production=security.production))
        db.telemetry = application.state.telemetry = telemetry
        resources.push_async_callback(asyncio.to_thread, telemetry.close)
        auth.bootstrap_super_admin(bootstrap_password)
        auth.purge_expired_sessions()
        plugin_roots = [root / "builtin_plugins"]
        configured_roots = os.getenv("DEEPAGENT_PLUGIN_PATHS", "")
        for configured_root in configured_roots.split(os.pathsep):
            if configured_root.strip():
                path = Path(configured_root.strip())
                plugin_roots.append(path if path.is_absolute() else root / path)
        plugin_report = PluginLoader(db, plugin_roots).load()
        logger.info(
            "Loaded %s plugin(s) and %s skill(s): %s",
            plugin_report.plugin_count,
            plugin_report.skill_count,
            ", ".join(plugin_report.plugin_ids) or "none",
        )
        skill_registry = SkillRegistry(db)
        providers = list(sandbox_providers or [])
        if not providers:
            sandbox_provider = configured(
                "DEEPAGENT_SANDBOX_PROVIDER",
                "remote" if security.production else "docker",
            ).strip().lower()
            if sandbox_provider == "remote":
                providers.append(
                    RemoteSandboxProvider(
                        base_url=configured("DEEPAGENT_SANDBOX_SERVICE_URL", ""),
                        service_token=read_secret(
                            "DEEPAGENT_SANDBOX_SERVICE_TOKEN",
                            values=file_environment,
                            required=True,
                            production=security.production,
                        ),
                        ca_file=configured("DEEPAGENT_SANDBOX_CA_FILE", "").strip()
                        or None,
                        client_cert_file=configured(
                            "DEEPAGENT_SANDBOX_CLIENT_CERT_FILE", ""
                        ).strip()
                        or None,
                        client_key_file=configured(
                            "DEEPAGENT_SANDBOX_CLIENT_KEY_FILE", ""
                        ).strip()
                        or None,
                        timeout_seconds=float(
                            configured("DEEPAGENT_SANDBOX_TIMEOUT_SECONDS", "30")
                        ),
                        require_https=security.production,
                    )
                )
            elif sandbox_provider == "docker":
                providers.append(DockerSandboxProvider(
                    image=os.getenv(
                        "DEEPAGENT_CODING_IMAGE", "deepagent/coding-runtime:0.1.0"
                    ),
                    dockerfile_root=str(root / "docker" / "coding-runtime"),
                    auto_build=os.getenv("DEEPAGENT_CODING_AUTO_BUILD", "true").lower()
                    in {"1", "true", "yes"},
                ))
            else:
                raise RuntimeError(
                    "DEEPAGENT_SANDBOX_PROVIDER must be remote or docker"
                )
        if security.production:
            if any(provider.name != "remote" for provider in providers):
                raise RuntimeError(
                    "Production workers must use the remote sandbox provider"
                )
            required_mtls = (
                configured("DEEPAGENT_SANDBOX_CA_FILE", "").strip(),
                configured("DEEPAGENT_SANDBOX_CLIENT_CERT_FILE", "").strip(),
                configured("DEEPAGENT_SANDBOX_CLIENT_KEY_FILE", "").strip(),
            )
            if not all(required_mtls):
                raise RuntimeError(
                    "Production remote sandboxes require CA, client certificate, and client key files"
                )

        def resolve_sandbox_image(provider_name: str, image: str) -> str:
            provider = next(
                (candidate for candidate in providers if candidate.name == provider_name),
                None,
            )
            resolver = getattr(provider, "resolve_image_digest", None)
            if resolver is None:
                raise ValueError(
                    f"Sandbox provider cannot resolve immutable images: {provider_name}"
                )
            return str(resolver(image))

        compiler = AgentPlanCompiler(
            skill_registry,
            allow_test_sandbox=not load_env,
            sandbox_image_resolver=resolve_sandbox_image,
        )
        if seed:
            seed_provider = providers[0]
            if seed_provider.name == "docker":
                coding_sandbox = SandboxProfileSpec(
                    provider="docker",
                    image=str(getattr(seed_provider, "image", "deepagent/coding-runtime:0.1.0")),
                )
            elif seed_provider.name == "fake" and not load_env:
                coding_sandbox = SandboxProfileSpec(
                    provider="fake",
                    image="deepagent/coding-runtime:test",
                    image_digest="sha256:" + ("0" * 64),
                    cpu_limit=1,
                    memory_mb=512,
                    disk_mb=1024,
                    pids_limit=64,
                )
            elif seed_provider.name == "remote":
                coding_sandbox = SandboxProfileSpec(
                    provider="remote",
                    image=configured(
                        "DEEPAGENT_CODING_IMAGE", "deepagent/coding-runtime:0.1.0"
                    ),
                )
            else:
                raise RuntimeError(f"Unsupported seed sandbox provider: {seed_provider.name}")
            seed_reference_data(db, compiler, coding_sandbox=coding_sandbox)
        content_scanner = create_content_scanner(production=security.production)
        object_storage = create_object_storage(data_root / "knowledge_objects")
        archive_store = SharedArchiveStore(object_storage) if object_storage.provider != "local" else None
        embedding_provider = create_embedding_provider()
        if security.production:
            if object_storage.provider == "local":
                raise RuntimeError("Production requires shared object storage")
            if embedding_provider.model_revision.startswith("deepagent-hash-embedding-"):
                raise RuntimeError("Production requires a semantic embedding provider")
        knowledge = KnowledgeService(
            db,
            object_storage,
            embedding_provider,
            queue=create_task_queue(db, "knowledge-ingestion"),
            content_scanner=content_scanner,
        )
        events = EventEmitter(db)
        active_model_gateway = model_gateway or OpenAICompatibleModelGateway.from_environment()
        models = ModelRegistry(db,active_model_gateway,allow_test_override=not load_env and model_gateway is not None)
        resources.push_async_callback(models.close)
        repository_roots = os.getenv("DEEPAGENT_REPOSITORY_ROOTS", "").strip()
        if not repository_roots and not security.production:
            repository_roots = str(root.parent)
        repositories = RepositoryService(
            db,
            data_root / "repository_snapshots",
            [
                Path(value.strip())
                for value in repository_roots.split(os.pathsep)
                if value.strip()
            ],
            content_scanner=content_scanner,
            archive_store=archive_store,
            network_policy=RepositoryNetworkPolicy.from_environment(production=security.production),
        )
        sandbox_manager = SandboxManager(
            db,
            events,
            repositories,
            providers,
            data_root / "workspace_snapshots",
            content_scanner=content_scanner,
            archive_store=archive_store,
        )
        coding = CodingService(db, repositories, sandbox_manager)
        checkpointer = FencedCheckpointSaver(
            db,
            sqlite_path=None if postgres_enabled else str(Path(database_location).with_suffix(".checkpoints.db")),
        )
        resources.callback(checkpointer.close)
        await asyncio.to_thread(checkpointer.initialize, auto_migrate=auto_migrate)
        active_coding_model = None
        try:
            active_coding_model = create_coding_chat_model(
                active_model_gateway, coding_model
            )
            if coding_model is None:
                resources.push_async_callback(close_coding_chat_model, active_coding_model)
        except ModelGatewayError:
            logger.info(
                "Native Coding Agent model is not configured; non-coding runs remain available"
            )
        orchestrator = RunOrchestrator(
            db,
            events,
            knowledge,
            active_model_gateway,
            queue=create_task_queue(db, "runtime-runs"),
        )
        if active_coding_model is not None or models.profiles:
            orchestrator.executors.coding = DeepAgentsRuntimeExecutor(
                db,
                events,
                orchestrator.worker_id,
                sandbox_manager,
                checkpointer,
                active_coding_model,
                active_model_gateway.identity(),
                knowledge,
            )
        run_service = RunService(db, events, orchestrator, coding)
        orchestrator.executors.models = models
        run_service.model_registry = models
        routing = IntentRoutingService(
            db, active_model_gateway, run_service, events
        )
        approvals = ApprovalService(db, events, orchestrator)
        application.state.services = SimpleNamespace(
            db=db,
            auth=auth,
            compiler=compiler,
            plugins=skill_registry,
            skills=skill_registry,
            plugin_load_report=plugin_report,
            knowledge=knowledge,
            events=events,
            model_gateway=active_model_gateway,
            coding_model=active_coding_model,
            repositories=repositories,
            sandbox_manager=sandbox_manager,
            coding=coding,
            checkpointer=checkpointer,
            loaded_environment_files=loaded_environment_files,
            orchestrator=orchestrator,
            agents=AgentService(db, compiler),
            evaluations=EvaluationService(db),
            billing=BillingService(db),
            models=models,
            runs=run_service,
            routing=routing,
            approvals=approvals,
        )
        workers_started = process_role in {"all", "worker"}
        if workers_started:
            resources.push_async_callback(knowledge.stop)
            await knowledge.start()
            resources.push_async_callback(sandbox_manager.stop)
            await sandbox_manager.start()
            resources.push_async_callback(orchestrator.stop)
            await orchestrator.start()
        application.state.services.health = HealthMonitor(application.state.services)
        telemetry.health.monitor = application.state.services.health
        resources.push_async_callback(application.state.services.health.stop)
        await application.state.services.health.start()
        yield

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with AsyncExitStack() as resources:
            application.state.resource_cleanup = resources
            async with application_lifespan(application):
                yield

    application = FastAPI(
        title="DeepAgent Platform API",
        version="0.1.0",
        description="Native Phase 1 control plane and execution runtime",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(security.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=['X-Request-ID', 'X-Trace-ID'],
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(security.allowed_hosts),
    )
    application.add_middleware(
        EnterpriseSecurityMiddleware,
        settings=security,
    )
    application.add_middleware(TelemetryMiddleware, application=application)

    @application.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": str(exc)}})

    @application.exception_handler(ConflictError)
    @application.exception_handler(PageAccessChanged)
    async def conflict_handler(_: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"error": {"code": "CONFLICT", "message": str(exc)}})

    @application.exception_handler(InvalidCursor)
    async def invalid_cursor_handler(_: Request, exc: InvalidCursor):
        return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CURSOR", "message": str(exc)}})

    @application.exception_handler(BudgetExceeded)
    async def quota_handler(_: Request, exc: BudgetExceeded):
        return JSONResponse(status_code=429, content={"error":{"code":"QUOTA_EXCEEDED","message":str(exc)}})

    @application.exception_handler(CapacityExceeded)
    async def capacity_handler(_: Request, exc: CapacityExceeded):
        return JSONResponse(status_code=429, headers={"Retry-After": "5"},
                            content={"error": {"code": "CAPACITY_EXCEEDED", "message": str(exc)}})

    @application.exception_handler(BillingConfigurationError)
    async def billing_config_handler(_: Request, exc: BillingConfigurationError):
        return JSONResponse(status_code=503, content={"error":{"code":"BILLING_NOT_CONFIGURED","message":str(exc)}})

    @application.exception_handler(AuthenticationError)
    async def authentication_error_handler(_: Request, exc: AuthenticationError):
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            content={"error": {"code": "AUTHENTICATION_FAILED", "message": str(exc)}},
        )

    @application.exception_handler(AuthNotFoundError)
    async def auth_not_found_handler(_: Request, exc: AuthNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "USER_NOT_FOUND", "message": str(exc)}},
        )

    @application.exception_handler(AuthConflictError)
    async def auth_conflict_handler(_: Request, exc: AuthConflictError):
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "USER_CONFLICT", "message": str(exc)}},
        )

    @application.exception_handler(AuthValidationError)
    async def auth_validation_handler(_: Request, exc: AuthValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "USER_VALIDATION", "message": str(exc)}},
        )

    @application.exception_handler(AuthAuthorizationError)
    async def auth_authorization_handler(_: Request, exc: AuthAuthorizationError):
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "AUTHORIZATION_FAILED", "message": str(exc)}},
        )

    @application.exception_handler(AuthRateLimitError)
    async def auth_rate_limit_handler(_: Request, exc: AuthRateLimitError):
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
            content={"error": {"code": "AUTH_RATE_LIMITED", "message": str(exc)}},
        )

    @application.exception_handler(KnowledgeError)
    async def knowledge_error_handler(_: Request, exc: KnowledgeError):
        if isinstance(exc, KnowledgeNotFoundError):
            status_code = 404
        elif isinstance(exc, KnowledgeConflictError):
            status_code = 409
        else:
            status_code = 422
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @application.exception_handler(CodingError)
    async def coding_error_handler(_: Request, exc: CodingError):
        if isinstance(exc, CodingNotFoundError):
            status_code = 404
        elif isinstance(exc, (CodingConflictError, SandboxUnavailableError)):
            status_code = 409
        else:
            status_code = 422
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @application.get("/livez")
    async def liveness():
        return {"status": "alive", "service": "platform-api"}

    @application.get('/metrics', include_in_schema=False)
    async def metrics(request: Request):
        # A distinct collector credential, not a user's bearer/cookie session.
        return MetricsResponse(request.app.state.telemetry)

    @application.get("/readyz")
    async def readiness(request: Request):
        result = request.app.state.services.health.snapshot()
        return JSONResponse(result, status_code=200 if result["status"] == "healthy" else 503,
                            headers={"Cache-Control": "no-store"})

    @application.get("/health")
    async def health(request: Request):
        services = request.app.state.services
        result = services.health.snapshot(details=not security.production)
        if not security.production:
            result.update(plugins_loaded=services.plugin_load_report.plugin_count,
                          skills_loaded=services.plugin_load_report.skill_count)
        return JSONResponse(result, status_code=200 if result["status"] == "healthy" else 503,
                            headers={"Cache-Control": "no-store"})

    application.include_router(auth_router)
    application.include_router(native_router)
    application.include_router(knowledge_router)
    application.include_router(repository_router)
    application.include_router(coding_router)
    application.include_router(routing_router)
    application.include_router(evaluation_router)
    application.include_router(billing_router)
    application.include_router(model_router)
    application.include_router(release_router)

    # A production web build can be served by the API process for the local
    # reference deployment. Kubernetes deployments may serve it independently.
    mount_web_console(application, root / "apps" / "web" / "dist")
    return application


app = create_app()
