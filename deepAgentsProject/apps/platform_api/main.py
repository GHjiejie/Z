from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from apps.platform_api.native_api.knowledge_routes import router as knowledge_router
from apps.platform_api.native_api.coding_routes import router as coding_router
from apps.platform_api.native_api.repository_routes import router as repository_router
from apps.platform_api.native_api.routing_routes import router as routing_router
from apps.platform_api.native_api.routes import router as native_router
from packages.application.approval_service import ApprovalService
from packages.application.services import (
    AgentService,
    ConflictError,
    NotFoundError,
    seed_reference_data,
)
from packages.compiler import AgentPlanCompiler
from packages.config import load_environment
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
from packages.persistence import Database
from packages.plugins import PluginLoader, SkillRegistry
from packages.repositories import RepositoryService
from packages.routing import IntentRoutingService
from packages.runtime import RunOrchestrator, RunService
from packages.runtime.coding_model import create_coding_chat_model
from packages.runtime.deepagents_executor import DeepAgentsRuntimeExecutor
from packages.runtime.event_emitter import EventEmitter
from packages.runtime.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    OpenAICompatibleModelGateway,
)
from packages.sandbox import DockerSandboxProvider, SandboxManager
from packages.sandbox.ports import SandboxProvider

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


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

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        loaded_environment_files = load_environment(root) if load_env else []
        application.state.trust_identity_headers = (
            trust_identity_headers
            if trust_identity_headers is not None
            else (
                not load_env
                or os.getenv("DEEPAGENT_TRUST_IDENTITY_HEADERS", "false").lower()
                in {"1", "true", "yes"}
            )
        )
        application.state.allow_demo_identity = (
            allow_demo_identity
            if allow_demo_identity is not None
            else (
                not load_env
                or os.getenv("DEEPAGENT_ALLOW_DEMO_IDENTITY", "false").lower()
                in {"1", "true", "yes"}
            )
        )
        db_path = database_path or os.getenv(
            "DEEPAGENT_DB_PATH", str(root / "data" / "deepagent.db")
        )
        db = Database(db_path)
        db.initialize()
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
            providers.append(
                DockerSandboxProvider(
                    image=os.getenv(
                        "DEEPAGENT_CODING_IMAGE", "deepagent/coding-runtime:0.1.0"
                    ),
                    dockerfile_root=str(root / "docker" / "coding-runtime"),
                    auto_build=os.getenv("DEEPAGENT_CODING_AUTO_BUILD", "true").lower()
                    in {"1", "true", "yes"},
                )
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
            else:
                coding_sandbox = SandboxProfileSpec()
            seed_reference_data(db, compiler, coding_sandbox=coding_sandbox)
        object_storage = create_object_storage(Path(db_path).parent / "knowledge_objects")
        knowledge = KnowledgeService(db, object_storage, create_embedding_provider())
        events = EventEmitter(db)
        active_model_gateway = model_gateway or OpenAICompatibleModelGateway.from_environment()
        repository_roots = (
            os.getenv("DEEPAGENT_REPOSITORY_ROOTS", "").strip() or str(root.parent)
        )
        repositories = RepositoryService(
            db,
            Path(db_path).parent / "repository_snapshots",
            [
                Path(value.strip())
                for value in repository_roots.split(os.pathsep)
                if value.strip()
            ],
        )
        sandbox_manager = SandboxManager(
            db,
            events,
            repositories,
            providers,
            Path(db_path).parent / "workspace_snapshots",
        )
        coding = CodingService(db, repositories, sandbox_manager)
        checkpointer_context = AsyncSqliteSaver.from_conn_string(
            str(Path(db_path).with_suffix(".checkpoints.db"))
        )
        checkpointer = await checkpointer_context.__aenter__()
        await checkpointer.setup()
        active_coding_model = None
        try:
            active_coding_model = create_coding_chat_model(
                active_model_gateway, coding_model
            )
        except ModelGatewayError:
            logger.info(
                "Native Coding Agent model is not configured; non-coding runs remain available"
            )
        orchestrator = RunOrchestrator(db, events, knowledge, active_model_gateway)
        if active_coding_model is not None:
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
        routing = IntentRoutingService(
            db, active_model_gateway, run_service, events
        )
        approvals = ApprovalService(db, events, orchestrator)
        application.state.services = SimpleNamespace(
            db=db,
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
            runs=run_service,
            routing=routing,
            approvals=approvals,
        )
        await knowledge.start()
        await sandbox_manager.start()
        await orchestrator.start()
        yield
        await orchestrator.stop()
        await sandbox_manager.stop()
        await knowledge.stop()
        await checkpointer_context.__aexit__(None, None, None)
        db.close()

    application = FastAPI(
        title="DeepAgent Platform API",
        version="0.1.0",
        description="Native Phase 1 control plane and execution runtime",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv("DEEPAGENT_CORS_ORIGINS", "http://localhost:5173").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": str(exc)}})

    @application.exception_handler(ConflictError)
    async def conflict_handler(_: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"error": {"code": "CONFLICT", "message": str(exc)}})

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

    @application.get("/health")
    async def health(request: Request):
        services = request.app.state.services
        model_identity = services.model_gateway.identity()
        return {
            "status": "healthy",
            "service": "platform-api",
            "worker_id": services.orchestrator.worker_id,
            "queue_depth": services.orchestrator.queue.qsize(),
            "plugins_loaded": services.plugin_load_report.plugin_count,
            "skills_loaded": services.plugin_load_report.skill_count,
            "model": {
                "configured": True,
                "provider": model_identity["provider"],
                "name": model_identity["model"],
                "route": model_identity["route"],
            },
        }

    application.include_router(native_router)
    application.include_router(knowledge_router)
    application.include_router(repository_router)
    application.include_router(coding_router)
    application.include_router(routing_router)

    # A production web build can be served by the API process for the local
    # reference deployment. Kubernetes deployments may serve it independently.
    web_dist = root / "apps" / "web" / "dist"
    if (web_dist / "index.html").is_file() and (web_dist / "assets").is_dir():
        application.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="web-assets")

        @application.get("/{spa_path:path}", include_in_schema=False)
        async def web_console(spa_path: str):
            candidate = web_dist / spa_path
            if spa_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")
    return application


app = create_app()
