"""Engine, connection pragmas, and the migration runner.

Three things this module exists to guarantee.

**Nothing blocks the event loop (invariant 5).** ``sqlite3`` is synchronous and
a stalled loop stalls every PTY stream at once, so the application talks to
SQLite only through ``aiosqlite`` behind SQLAlchemy's async engine. Alembic is
the one synchronous consumer and it runs through :func:`asyncio.to_thread`.

**Pragmas are per connection, not per database.** ``journal_mode=WAL`` is
persistent — it is written into the file header once and survives reopening.
``foreign_keys`` is **not**: it defaults to ``OFF`` on every new connection, and
a pooled connection that skips it silently stops enforcing every FK in the
schema. Half-enforced referential integrity is worse than none, because it looks
fine until the one connection that matters. They are therefore wired to the
``connect`` event, which fires for *every* connection the pool ever opens.

**There is no ``create_all`` here, on purpose.** The schema comes from Alembic
and only from Alembic. A ``metadata.create_all`` in a test fixture would mean
the migration is exercised for the first time on a real user's database.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import Settings

# backend/app/storage/db.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = BACKEND_ROOT / "migrations"

# Applied to every connection, in this order.
#
# journal_mode  readers never block the single writer; the whole reason SQLite is
#               sufficient here (`design.md` §5).
# foreign_keys  off by default in SQLite, for backwards compatibility with 2004.
# busy_timeout  WAL still allows exactly one writer. Without a timeout a second
#               concurrent writer gets SQLITE_BUSY immediately instead of
#               waiting the few milliseconds the first one needs.
# synchronous   NORMAL is the documented companion to WAL: durable across
#               process crashes, which is the failure this project cares about.
PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", "5000"),
    ("synchronous", "NORMAL"),
)


class StorageError(Exception):
    """The database could not be opened, migrated, or configured."""


def sync_url(url: str) -> str:
    """The blocking equivalent of an async URL, for Alembic only."""
    return make_url(url).set(drivername="sqlite").render_as_string(hide_password=False)


def database_path(url: str) -> Path | None:
    """The file an async or sync SQLite URL points at, if it is a file."""
    database = make_url(url).database
    return None if database in (None, "", ":memory:") else Path(str(database))


def install_pragmas(engine: Engine, *, enforce_foreign_keys: bool = True) -> None:
    """Wire :data:`PRAGMAS` to the engine's ``connect`` event.

    ``enforce_foreign_keys=False`` is for Alembic only: batch migrations rebuild
    a table by copying it and dropping the original, and enforcement during that
    window makes a legal migration look like a violation.
    """
    pragmas = (
        PRAGMAS
        if enforce_foreign_keys
        else tuple(p for p in PRAGMAS if p[0] != "foreign_keys")
    )

    def listener(dbapi_connection: Any, record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for name, value in pragmas:
                cursor.execute(f"PRAGMA {name}={value}")
        finally:
            cursor.close()

    event.listen(engine, "connect", listener)


class Database:
    """One async engine plus its session factory.

    Owned by the application lifespan and by tests; nothing else constructs an
    engine. Sessions use ``expire_on_commit=False`` — an async session that
    expires its objects re-loads them on the next attribute access, which under
    asyncio raises rather than quietly doing I/O behind your back.
    """

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self._url = url
        self._engine = create_async_engine(url, echo=echo)
        install_pragmas(self._engine.sync_engine)
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(settings.database_url, echo=settings.database_echo)

    @property
    def url(self) -> str:
        return self._url

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A unit of work. Rolls back and closes on any exception."""
        session = self._sessionmaker()
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def dispose(self) -> None:
        await self._engine.dispose()


def alembic_config(url: str) -> Config:
    """An Alembic ``Config`` built in code, pointed at ``backend/migrations``.

    ``alembic.ini`` deliberately carries no ``sqlalchemy.url``: the database
    location comes from :class:`app.config.Settings`, so there is exactly one
    answer to "which file am I writing to" and a test cannot accidentally
    migrate the real one.
    """
    if not MIGRATIONS_DIR.is_dir():
        raise StorageError(f"migrations directory is missing: {MIGRATIONS_DIR}")
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", sync_url(url))
    return config


def upgrade_database_sync(url: str, *, revision: str = "head") -> None:
    """Run migrations. Blocking — call :func:`upgrade_database` from the loop."""
    path = database_path(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        command.upgrade(alembic_config(url), revision)
    except sqlite3.Error as exc:  # pragma: no cover - corrupt or unwritable file
        raise StorageError(f"cannot migrate {url}: {exc}") from exc


async def upgrade_database(url: str, *, revision: str = "head") -> None:
    """Migrate off the event loop (invariant 5)."""
    await asyncio.to_thread(upgrade_database_sync, url, revision=revision)


def downgrade_database_sync(url: str, *, revision: str = "base") -> None:
    """Reverse migrations. Blocking; tests and `agenthub` maintenance only."""
    command.downgrade(alembic_config(url), revision)
