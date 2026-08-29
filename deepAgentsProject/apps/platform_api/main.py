from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from apps.platform_api.native_api.knowledge_routes import router as knowledge_router
from apps.platform_api.native_api.routes import router as native_router
from packages.application.approval_service import ApprovalService
from packages.application.services import (
    AgentService,
    ConflictError,
    NotFoundError,
    seed_reference_data,
)
from packages.compiler import AgentPlanCompiler
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
from packages.runtime import RunOrchestrator, RunService
from packages.runtime.event_emitter import EventEmitter


logger = logging.getLogger(__name__)


def create_app(database_path: str | None = None, seed: bool = True) -> FastAPI:
    root = Path(__file__).resolve().parents[2]
    db_path = database_path or os.getenv("DEEPAGENT_DB_PATH", str(root / "data" / "deepagent.db"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
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
        compiler = AgentPlanCompiler(skill_registry)
        if seed:
            seed_reference_data(db, compiler)
        object_storage = create_object_storage(root / "data" / "knowledge_objects")
        knowledge = KnowledgeService(db, object_storage, create_embedding_provider())
        events = EventEmitter(db)
        orchestrator = RunOrchestrator(db, events, knowledge)
        run_service = RunService(db, events, orchestrator)
        approvals = ApprovalService(db, events, orchestrator)
        application.state.services = SimpleNamespace(
            db=db,
            compiler=compiler,
            plugins=skill_registry,
            skills=skill_registry,
            plugin_load_report=plugin_report,
            knowledge=knowledge,
            events=events,
            orchestrator=orchestrator,
            agents=AgentService(db, compiler),
            runs=run_service,
            approvals=approvals,
        )
        await knowledge.start()
        await orchestrator.start()
        yield
        await orchestrator.stop()
        await knowledge.stop()
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

    @application.get("/health")
    async def health(request: Request):
        services = request.app.state.services
        return {
            "status": "healthy",
            "service": "platform-api",
            "worker_id": services.orchestrator.worker_id,
            "queue_depth": services.orchestrator.queue.qsize(),
            "plugins_loaded": services.plugin_load_report.plugin_count,
            "skills_loaded": services.plugin_load_report.skill_count,
        }

    application.include_router(native_router)
    application.include_router(knowledge_router)

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
