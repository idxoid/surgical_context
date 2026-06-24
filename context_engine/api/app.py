"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
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
from context_engine.api.warmup import warm_context_engine
from context_engine.database.provider import close_database_provider

logger = logging.getLogger(__name__)


def create_app(state: SidecarState, *, main_module: Any) -> FastAPI:
    main_deps = MainRouteDeps(main=main_module, state=state)
    indexing_deps = IndexingRouteDeps(
        main=main_module,
        state=state,
        indexing=state.indexing_service,
    )
    # Direct (non-HTTP) callers — e.g. unit tests invoking route functions
    # without a Request — fall back to these; the HTTP path resolves per-app
    # from ``app.state`` below so multiple app instances stay isolated.
    configure_main_routes(main_deps)
    configure_indexing_routes(indexing_deps)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state.git_delta_poller.start()
        await asyncio.to_thread(warm_context_engine, state)
        try:
            yield
        finally:
            state.git_delta_poller.close()
            state.index_queue.close()
            close_database_provider()

    app = FastAPI(title="Surgical Context Sidecar", lifespan=lifespan)
    app.state.route_deps = main_deps
    app.state.indexing_deps = indexing_deps
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
