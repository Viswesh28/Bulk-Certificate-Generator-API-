"""Application factory.

Run with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

A factory (rather than a module-level ``app``) means importing this module has
no side effects — no database file is created, no worker starts — which keeps
the tests isolated and lets each test build its own app with its own settings.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .api.routes import router
from .config import Settings, get_settings
from .database import make_engine, make_session_factory, init_db
from .services.storage import LocalStorage
from .services.worker import BackgroundWorker

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()

    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    storage = LocalStorage(settings.storage_dir)
    worker = BackgroundWorker(session_factory, storage, concurrency=settings.worker_concurrency)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            await worker.start()
        try:
            yield
        finally:
            if start_worker:
                await worker.stop()
            engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "Submit a list of recipients, generate a PDF certificate for each one in the "
            "background, poll job progress, and download results."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.storage = storage
    app.state.worker = worker

    # The browser UI is served from this same origin. CORS is still enabled
    # because sandboxed previews (and other front ends) call the API cross-origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    app.include_router(router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
