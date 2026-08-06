"""AgentHub ASGI application composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.models.clock import now_ms


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own process-wide resources; database and broker join here in Phase 1."""
    app.state.started_ms = now_ms()
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="AgentHub",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    return application


app = create_app()
