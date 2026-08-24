from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from apps.platform_api.native_api.routes import router as native_router
from packages.application.approval_service import ApprovalService
from packages.application.services import (
    AgentService,
    ConflictError,
    NotFoundError,
    seed_reference_data,
)
from packages.compiler import AgentPlanCompiler
from packages.persistence import Database
from packages.runtime import RunOrchestrator, RunService
from packages.runtime.event_emitter import EventEmitter


def create_app(database_path: str | None = None, seed: bool = True) -> FastAPI:
    root = Path(__file__).resolve().parents[2]
    db_path = database_path or os.getenv("DEEPAGENT_DB_PATH", str(root / "data" / "deepagent.db"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        db = Database(db_path)
        db.initialize()
        if seed:
            seed_reference_data(db)
        compiler = AgentPlanCompiler()
        events = EventEmitter(db)
        orchestrator = RunOrchestrator(db, events)
        run_service = RunService(db, events, orchestrator)
        approvals = ApprovalService(db, events, orchestrator)
        application.state.services = SimpleNamespace(
            db=db,
            compiler=compiler,
            events=events,
            orchestrator=orchestrator,
            agents=AgentService(db, compiler),
            runs=run_service,
            approvals=approvals,
        )
        await orchestrator.start()
        yield
        await orchestrator.stop()
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

    @application.get("/health")
    async def health(request: Request):
        services = request.app.state.services
        return {
            "status": "healthy",
            "service": "platform-api",
            "worker_id": services.orchestrator.worker_id,
            "queue_depth": services.orchestrator.queue.qsize(),
        }

    application.include_router(native_router)

    # A production web build can be served by the API process for the local
    # reference deployment. Kubernetes deployments may serve it independently.
    web_dist = root / "apps" / "web" / "dist"
    if web_dist.exists():
        application.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="web-assets")

        @application.get("/{spa_path:path}", include_in_schema=False)
        async def web_console(spa_path: str):
            candidate = web_dist / spa_path
            if spa_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")
    return application


app = create_app()
