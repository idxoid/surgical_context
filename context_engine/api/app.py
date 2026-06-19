"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from context_engine.api.state import SidecarState
from context_engine.database.provider import close_database_provider


def create_app(state: SidecarState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state.git_delta_poller.start()
        try:
            yield
        finally:
            state.git_delta_poller.close()
            state.index_queue.close()
            close_database_provider()

    return FastAPI(title="Surgical Context Sidecar", lifespan=lifespan)
