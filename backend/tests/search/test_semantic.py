"""E6 incremental chunks and sqlite-vec ranking."""

from pathlib import Path

from sqlmodel import select

from app.config import Settings
from app.models.ids import new_session_id
from app.models.tables import SemanticChunk
from app.search.semantic import SemanticIndexService
from app.search.symbols import SymbolIndexService
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository


async def semantic_target(
    tmp_path: Path,
) -> tuple[SemanticIndexService, SymbolIndexService, Database, str, Path]:
    settings = Settings(root=tmp_path / "agenthub")
    upgrade_database_sync(settings.database_url)
    database = Database.from_settings(settings)
    session_id = new_session_id()
    workspace = settings.workspaces_root / session_id
    integration = workspace / "integration"
    integration.mkdir(parents=True)
    async with database.session() as db_session:
        await Repository(db_session).create_session(
            session_id=session_id,
            title="Semantic index",
            repo_path=tmp_path / "repo",
            workspace_root=workspace,
            integration_branch=f"agenthub/{session_id}/integration",
        )
    return (
        SemanticIndexService(database),
        SymbolIndexService(database),
        database,
        session_id,
        integration,
    )


async def test_semantic_index_is_incremental_and_ranks_bounded_chunks(
    tmp_path: Path,
) -> None:
    semantic, symbols, database, session_id, integration = await semantic_target(
        tmp_path
    )
    (integration / "pricing.py").write_text(
        "def recurring_discount(customer):\n"
        "    if customer.order_count >= 3:\n"
        "        return 0.10\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (integration / "health.py").write_text(
        "def readiness_probe():\n    return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    try:
        await symbols.sync_session(session_id, integration)
        assert await semantic.sync_session(session_id, integration) == 2
        assert await semantic.sync_session(session_id, integration) == 0

        result = await semantic.search(session_id, "recurring customer discount")
        assert result.matches
        assert result.matches[0].path == "pricing.py"
        assert result.matches[0].line == 1
        assert result.matches[0].distance >= 0

        (integration / "health.py").write_text(
            "def liveness_probe():\n    return True\n", encoding="utf-8"
        )
        assert await semantic.sync_session(session_id, integration) == 1
        async with database.session() as db_session:
            chunks = (await db_session.exec(select(SemanticChunk))).all()
        assert {chunk.path for chunk in chunks} == {"health.py", "pricing.py"}
    finally:
        await database.dispose()
