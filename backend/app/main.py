"""AgentHub ASGI application composition root."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.models.clock import now_ms
from app.models.pricing import load_price_table
from app.orchestrator.service import SingleRunService
from app.storage.db import Database, upgrade_database


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database and application service for the process lifetime."""
    app.state.started_ms = now_ms()
    settings: Settings = app.state.settings
    await upgrade_database(settings.database_url)
    prices = await asyncio.to_thread(load_price_table, settings.pricing_path)
    database = Database.from_settings(settings)
    app.state.database = database
    app.state.orchestrator = SingleRunService(
        database=database,
        settings=settings,
        prices=prices,
    )
    try:
        yield
    finally:
        await database.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI(
        title="AgentHub",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = settings or get_settings()
    application.include_router(health_router)
    return application


app = create_app()
