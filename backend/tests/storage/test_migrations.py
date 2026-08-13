"""Alembic builds the database, and only Alembic does.

`docs/phase-1.md` B2 asks for proof that the migration builds an empty database.
``metadata.create_all`` is not that proof: a migration that has never run is not
a migration, and the first person to run it would be a user.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.runtime.environment import EnvironmentContext
from sqlmodel import SQLModel

from app.config import DEFAULT_ROOT, Settings
from app.storage.db import (
    MIGRATIONS_DIR,
    StorageError,
    alembic_config,
    database_path,
    downgrade_database_sync,
    sync_url,
    upgrade_database_sync,
)

EXPECTED_TABLES = {
    "ai_preference",
    "session",
    "node",
    "node_dependency",
    "run",
    "usage_event",
    "acceptance_result",
    "node_review",
    "node_transition",
    "system_metric_minute",
    "symbol_source",
    "code_symbol",
    "semantic_source",
    "semantic_chunk",
}

# The last revision of Phase 1. Databases at this revision exist on real
# machines with accepted runs in them (`docs/acceptance-phase-1.md`).
PHASE_1_REVISION = "a83db6150739"


def table_names(url: str) -> set[str]:
    engine = sa.create_engine(sync_url(url))
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def rows(url: str, statement: str) -> list[tuple[object, ...]]:
    engine = sa.create_engine(sync_url(url))
    try:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(sa.text(statement))]
    finally:
        engine.dispose()


def test_migration_builds_the_database_from_nothing(settings: Settings) -> None:
    assert not settings.db_path.exists()

    upgrade_database_sync(settings.database_url)

    assert settings.db_path.exists()
    assert EXPECTED_TABLES <= table_names(settings.database_url)


def test_migration_creates_the_parent_directory(settings: Settings) -> None:
    # ~/.agenthub does not exist on a clean machine (docs/architecture.md §4).
    assert not settings.root.exists()
    upgrade_database_sync(settings.database_url)
    assert settings.db_path.parent.is_dir()


def test_upgrade_is_idempotent(migrated_url: str) -> None:
    upgrade_database_sync(migrated_url)
    assert EXPECTED_TABLES <= table_names(migrated_url)


def test_downgrade_removes_everything_it_created(migrated_url: str) -> None:
    """A revision you cannot reverse is a revision you cannot test twice."""
    downgrade_database_sync(migrated_url)
    assert not (EXPECTED_TABLES & table_names(migrated_url))


def test_schema_matches_the_models(migrated_url: str) -> None:
    """The drift check: no pending autogenerate operations after upgrading.

    This is what catches a column added to ``app/models/tables.py`` without a
    migration — the change works locally, where SQLModel built the table in
    memory, and fails on the next machine.
    """
    engine = sa.create_engine(sync_url(migrated_url))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            diff = compare_metadata(context, SQLModel.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"models and migrations disagree: {diff}"


def test_batch_mode_is_configured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite cannot ALTER, so every migration must run in batch mode.

    Asserted by watching the real ``context.configure`` call rather than by
    reading the source: the flag matters at the moment ``env.py`` runs, and this
    fails if a future edit drops it from only one of the two code paths.
    """
    recorded: dict[str, object] = {}
    original = EnvironmentContext.configure

    def spy(self: EnvironmentContext, **kwargs: object) -> None:
        recorded.update(kwargs)
        return original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(EnvironmentContext, "configure", spy)
    upgrade_database_sync(settings.database_url)

    assert recorded["render_as_batch"] is True


def seed_a_phase_1_database(url: str) -> None:
    """One session, one node, one run and two usage rows — written as SQL.

    Raw SQL and not the repository on purpose: the models in
    ``app/models/tables.py`` describe *today's* schema, and this database is
    deliberately at yesterday's. Inserting through them would either fail or
    quietly prove nothing.
    """
    engine = sa.create_engine(sync_url(url))
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO session VALUES ('sess_1', 'add a docstring',"
                    " '/repo', '/ws/sess_1', 'agenthub/sess_1/integration', 0,"
                    " 'running', 1700000000000, 1700000000001)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO node (id, session_id, name, prompt,"
                    " acceptance_criteria, harness, model, worktree_path, branch,"
                    " base_ref, status, created_ms, updated_ms) VALUES"
                    " ('node_1', 'sess_1', 'main', 'add a docstring to foo()',"
                    " 'pytest passes', 'codex', 'gpt-5.6-terra', '/ws/sess_1/node_1',"
                    " 'agenthub/sess_1/node_1', 'abc123', 'done',"
                    " 1700000000000, 1700000000002)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO run (id, node_id, session_id, attempt, status,"
                    " harness, model, cwd, pid, harness_session_id,"
                    " harness_version, events_path, started_ms, finished_ms,"
                    " exit_code, summary, event_count, permission_denial_count,"
                    " created_ms) VALUES ('run_1', 'node_1', 'sess_1', 1,"
                    " 'success', 'codex', 'gpt-5.6-terra', '/ws/sess_1/node_1',"
                    " 4242, 'thread-abc', '0.101.0',"
                    " '/root/runs/run_1/events.ndjson', 1700000000010,"
                    " 1700000000090, 0, 'done', 11, 0, 1700000000000)"
                )
            )
            for seq, tokens in ((0, 21), (1, 13)):
                connection.execute(
                    sa.text(
                        "INSERT INTO usage_event (run_id, node_id, session_id, seq,"
                        " ts, harness, model, source, input_tokens, output_tokens,"
                        " cache_read_tokens, cache_write_tokens,"
                        " cache_write_5m_tokens, cache_write_1h_tokens,"
                        " price_table_version, cost_usd) VALUES ('run_1', 'node_1',"
                        f" 'sess_1', {seq}, 1700000000050, 'codex', 'gpt-5.6-terra',"
                        f" 'reported', {tokens}, 254, 21737, 6513, 0, 6513, 1, 0.42)"
                    )
                )
    finally:
        engine.dispose()


def test_a_populated_phase_1_database_migrates_forward(settings: Settings) -> None:
    """C1's real risk: there is history in these files.

    A migration that drops and recreates ``node`` to add a column would take an
    accepted run's node with it. So this builds a database at the Phase 1
    revision, fills it, upgrades, and checks that every authored value survived
    unchanged — and that the new columns arrived with usable defaults rather
    than as NULLs the application would have to special-case forever.
    """
    upgrade_database_sync(settings.database_url, revision=PHASE_1_REVISION)
    seed_a_phase_1_database(settings.database_url)

    upgrade_database_sync(settings.database_url)

    assert rows(
        settings.database_url,
        "SELECT id, session_id, name, prompt, acceptance_criteria, harness, model,"
        " worktree_path, branch, base_ref, status, created_ms, updated_ms FROM node",
    ) == [
        (
            "node_1",
            "sess_1",
            "main",
            "add a docstring to foo()",
            # Rewritten from prose into a one-element array, losslessly. A
            # Phase 1 value was free text; splitting it on newlines would be a
            # guess about what the operator meant.
            '["pytest passes"]',
            "codex",
            "gpt-5.6-terra",
            "/ws/sess_1/node_1",
            "agenthub/sess_1/node_1",
            "abc123",
            "done",
            1700000000000,
            1700000000002,
        )
    ]
    # The new authored columns: "we were never told" reads as an empty list, not
    # as NULL. Nothing downstream has to write `node.touches or ()`.
    assert rows(
        settings.database_url, "SELECT touches, estimated_effort FROM node"
    ) == [("[]", None)]
    # Existing nodes retain the historical human gate. A migration must never
    # silently turn an already-authored graph into unattended execution.
    assert rows(settings.database_url, "SELECT requires_review FROM node") == [(1,)]
    # And nothing above or below the node moved.
    assert rows(settings.database_url, "SELECT id, title, status FROM session") == [
        ("sess_1", "add a docstring", "running")
    ]
    assert rows(settings.database_url, "SELECT final_branch FROM session") == [
        ("agenthub/sess_1/result",)
    ]
    assert rows(
        settings.database_url, "SELECT id, attempt, status, event_count FROM run"
    ) == [("run_1", 1, "success", 11)]
    assert rows(
        settings.database_url,
        "SELECT node_id, status, ts FROM node_transition",
    ) == [("node_1", "done", 1700000000002)]
    engine = sa.create_engine(sync_url(settings.database_url))
    try:
        with engine.begin() as connection:
            with pytest.raises(sa.exc.DatabaseError, match="append-only"):
                connection.execute(
                    sa.text("UPDATE node_transition SET status = 'failed'")
                )
    finally:
        engine.dispose()
    assert rows(
        settings.database_url,
        "SELECT count(*), sum(input_tokens), sum(cost_usd) FROM usage_event",
    ) == [(2, 34, 0.84)]


def test_migrating_a_populated_database_keeps_the_append_only_trigger(
    settings: Settings,
) -> None:
    """B2's trap, checked after the migration that could have sprung it.

    A ``batch_alter_table`` rebuilds a table and SQLite drops its triggers with
    it. ``test_usage_event_append_only_trigger_exists`` proves the trigger is
    there on a fresh database; this proves the *upgrade path* did not quietly
    remove it from an existing one.
    """
    upgrade_database_sync(settings.database_url, revision=PHASE_1_REVISION)
    seed_a_phase_1_database(settings.database_url)
    upgrade_database_sync(settings.database_url)

    engine = sa.create_engine(sync_url(settings.database_url))
    try:
        with engine.begin() as connection:
            with pytest.raises(sa.exc.DatabaseError, match="append-only"):
                connection.execute(sa.text("UPDATE usage_event SET input_tokens = 0"))
    finally:
        engine.dispose()


def test_usage_event_append_only_trigger_exists(migrated_url: str) -> None:
    engine = sa.create_engine(sync_url(migrated_url))
    try:
        with engine.connect() as connection:
            triggers = set(
                connection.execute(
                    sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                ).scalars()
            )
    finally:
        engine.dispose()
    assert "usage_event_is_append_only" in triggers


def test_alembic_config_points_at_the_repository_migrations() -> None:
    config = alembic_config("sqlite+aiosqlite:////tmp/does-not-matter.db")
    assert config.get_main_option("script_location") == str(MIGRATIONS_DIR)
    # The async driver is stripped: Alembic runs off the loop, synchronously.
    assert (
        config.get_main_option("sqlalchemy.url") == "sqlite:////tmp/does-not-matter.db"
    )


def test_alembic_config_fails_loudly_when_migrations_are_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.storage.db.MIGRATIONS_DIR", tmp_path / "nope")
    with pytest.raises(StorageError, match="migrations directory"):
        alembic_config("sqlite+aiosqlite:////tmp/x.db")


def test_url_helpers() -> None:
    assert sync_url("sqlite+aiosqlite:////var/db/agenthub.db") == (
        "sqlite:////var/db/agenthub.db"
    )
    assert database_path("sqlite+aiosqlite:////var/db/agenthub.db") == Path(
        "/var/db/agenthub.db"
    )
    assert database_path("sqlite+aiosqlite://") is None


def test_a_test_never_migrates_the_real_database(settings: Settings) -> None:
    """The fixture must not point at ``~/.agenthub`` (docs/architecture.md §4).

    The default root is the user's real session history. A fixture that forgot
    to override it would migrate — and, in another test, truncate — the machine
    the suite is running on.
    """
    assert settings.db_path != DEFAULT_ROOT / "agenthub.db"
    assert Path.home() not in settings.db_path.parents

    upgrade_database_sync(settings.database_url)
    assert settings.db_path.exists()
    assert EXPECTED_TABLES <= table_names(settings.database_url)


def test_prose_acceptance_criteria_survive_as_one_criterion(
    settings: Settings,
) -> None:
    """Revision dab2c49d6ccb rewrites the column; it must not lose the text.

    The read path is the point: after the upgrade the column holds JSON, and
    ``json.loads`` on the old bare prose would raise. A row that migrated but
    can no longer be loaded is not a migrated row.
    """
    upgrade_database_sync(settings.database_url, revision=PHASE_1_REVISION)
    seed_a_phase_1_database(settings.database_url)
    upgrade_database_sync(settings.database_url)

    (value,) = rows(
        settings.database_url,
        "SELECT acceptance_criteria FROM node WHERE id = 'node_1'",
    )[0]
    assert json.loads(value) == ["pytest passes"]


def test_the_rebuild_of_node_keeps_its_foreign_keys_enforcing(
    settings: Settings,
) -> None:
    """``node`` is the parent of two foreign keys and batch mode copies it out.

    ``run.node_id`` and ``node_dependency``'s composite pair both point here.
    SQLite drops a table's triggers when it is rebuilt (see a83db6150739) and
    it is equally capable of leaving a foreign key pointing at nothing, so the
    guarantee is asserted after the copy rather than assumed to survive it.
    """
    upgrade_database_sync(settings.database_url, revision=PHASE_1_REVISION)
    seed_a_phase_1_database(settings.database_url)
    upgrade_database_sync(settings.database_url)

    with closing(sqlite3.connect(database_path(settings.database_url))) as connection:
        connection.execute("PRAGMA foreign_keys = ON")

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO run (id, node_id, session_id, attempt, status,"
                " harness, events_path, event_count, permission_denial_count,"
                " created_ms) VALUES ('run_x', 'node_missing', 'sess_1', 1,"
                " 'running', 'codex', '/x', 0, 0, 1)"
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO node_dependency (node_id, depends_on_id, session_id,"
                " created_ms) VALUES ('node_1', 'node_missing', 'sess_1', 1)"
            )


def test_the_gate_tables_arrive_on_a_populated_phase_1_database(
    settings: Settings,
) -> None:
    """Revision e9f4b9cfa8c1 is additive, and the point is what it does *not* do.

    ``node`` carries an accepted run in real databases. C7 needed somewhere to
    put a reviewer's verdict, and the way to get that wrong is to hang it off
    ``run`` and then reach for ``batch_alter_table`` — which rebuilds a table
    and drops its triggers with it (a83db6150739). Two new tables and no ALTER
    means nothing above them can move.
    """
    upgrade_database_sync(settings.database_url, revision=PHASE_1_REVISION)
    seed_a_phase_1_database(settings.database_url)

    upgrade_database_sync(settings.database_url)

    assert {"acceptance_result", "node_review"} <= table_names(settings.database_url)
    # The pre-existing node is untouched and can be reviewed as it stands.
    assert rows(settings.database_url, "SELECT count(*) FROM acceptance_result") == [
        (0,)
    ]
    with closing(sqlite3.connect(database_path(settings.database_url))) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO acceptance_result (node_id, attempt, position, criterion,"
            " outcome, created_ms, updated_ms) VALUES"
            " ('node_1', 1, 0, 'pytest passes', 'unevaluated', 1, 1)"
        )
        connection.execute(
            "INSERT INTO node_review (node_id, attempt, decision, feedback,"
            " reviewed_ms) VALUES ('node_1', 1, 'rejected', 'try again', 1)"
        )

        # The closed vocabularies are the database's, not Python's: a typo'd
        # outcome renders as a state with no colour and no icon.
        with pytest.raises(sqlite3.IntegrityError, match="criterion_outcome"):
            connection.execute(
                "INSERT INTO acceptance_result (node_id, attempt, position,"
                " criterion, outcome, created_ms, updated_ms) VALUES"
                " ('node_1', 1, 1, 'x', 'probably', 1, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="review_decision"):
            connection.execute(
                "INSERT INTO node_review (node_id, attempt, decision, reviewed_ms)"
                " VALUES ('node_1', 2, 'maybe', 1)"
            )
        # ...and neither table can outlive the node it judges.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO node_review (node_id, attempt, decision, reviewed_ms)"
                " VALUES ('node_missing', 1, 'approved', 1)"
            )


def test_the_gate_tables_do_not_hang_off_run(settings: Settings) -> None:
    """Invariant 4, enforced by where the foreign keys point.

    ``app/storage/replay.py`` deletes the ``run`` row to rebuild it from the
    log. A verdict that cascaded from ``run`` would be destroyed by an ordinary
    replay, so the schema must make that impossible rather than rely on nobody
    doing it.
    """
    upgrade_database_sync(settings.database_url)

    with closing(sqlite3.connect(database_path(settings.database_url))) as connection:
        for table in ("acceptance_result", "node_review"):
            referenced = {
                row[2]
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            assert referenced == {"node"}, f"{table} must not reference {referenced}"
