"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from context_engine.api.routes import (
    ask,
    auth,
    feedback,
    health,
    history,
    impact,
    indexing,
    overlay,
    search,
)
from context_engine.api.routes.deps import MainRouteDeps, configure_main_routes
from context_engine.api.routes.indexing import IndexingRouteDeps, configure_indexing_routes
from context_engine.api.state import SidecarState
from context_engine.database.provider import close_database_provider


def create_app(state: SidecarState, *, main_module: Any) -> FastAPI:
    configure_main_routes(MainRouteDeps(main=main_module, state=state))
    configure_indexing_routes(
        IndexingRouteDeps(
            main=main_module,
            state=state,
            indexing=state.indexing_service,
        )
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state.git_delta_poller.start()
        try:
            yield
        finally:
            state.git_delta_poller.close()
            state.index_queue.close()
            close_database_provider()

    app = FastAPI(title="Surgical Context Sidecar", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(indexing.router)
    app.include_router(search.router)
    app.include_router(overlay.router)
    app.include_router(ask.router)
    app.include_router(history.router)
    app.include_router(feedback.router)
    app.include_router(auth.router)
    app.include_router(impact.router)
    return app
