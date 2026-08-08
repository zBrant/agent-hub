"""AgentHub ASGI application composition root."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.dashboard import router as dashboard_router
from app.api.graph import router as graph_router
from app.api.health import router as health_router
from app.api.node import router as node_router
from app.api.session import router as session_router
from app.config import Settings, get_settings
from app.metrics.dashboard import DashboardService
from app.models.clock import now_ms
from app.models.pricing import load_price_table
from app.models.tables import Node
from app.orchestrator.planner import create_planner
from app.orchestrator.scheduler import GraphScheduler
from app.orchestrator.service import NodeRunService
from app.storage.db import Database, upgrade_database
from app.ws.broker import EventBroker
from app.ws.router import router as websocket_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database and application service for the process lifetime."""
    app.state.started_ms = now_ms()
    settings: Settings = app.state.settings
    await upgrade_database(settings.database_url)
    prices = await asyncio.to_thread(load_price_table, settings.pricing_path)
    database = Database.from_settings(settings)
    broker = EventBroker()
    app.state.database = database
    app.state.broker = broker

    async def publish_transition(node: Node) -> None:
        await broker.publish_node_status(
            session_id=node.session_id,
            node_id=node.id,
            status=node.status.value,
            ts=node.updated_ms,
        )

    orchestrator = NodeRunService(
        database=database,
        settings=settings,
        prices=prices,
        broadcast=broker.publish,
        register_run=broker.register_run,
        on_transition=publish_transition,
    )
    scheduler = GraphScheduler(
        lifecycle=orchestrator,
        database=database,
        settings=settings,
    )
    planner = create_planner(settings, prices)
    app.state.orchestrator = orchestrator
    app.state.scheduler = scheduler
    app.state.planner = planner
    app.state.dashboard = DashboardService(database)
    try:
        yield
    finally:
        await scheduler.close()
        await planner.close()
        await database.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI(
        title="AgentHub",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = settings or get_settings()
    application.include_router(health_router)
    application.include_router(dashboard_router)
    application.include_router(session_router)
    # Registered after the session router so that `/api/sessions/{session_id}`
    # keeps matching before the node prefix that extends it; FastAPI resolves in
    # declaration order and both are unambiguous, but the reading order matches
    # the addressing hierarchy.
    application.include_router(node_router)
    application.include_router(graph_router)
    application.include_router(websocket_router)
    return application


app = create_app()
