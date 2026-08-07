"""Alembic environment.

Synchronous on purpose. Migrations are not on the request path — they run at
startup or from the CLI, and :func:`app.storage.db.upgrade_database` already
pushes them off the event loop with ``asyncio.to_thread``. Driving Alembic
through the async engine would buy nothing and add a greenlet bridge to the one
place where a clear stack trace matters most.

Two SQLite-specific settings below are not optional:

``render_as_batch=True``
    SQLite cannot ``ALTER`` a column, drop a constraint, or add one. Alembic's
    batch mode emulates all three by rebuilding the table. Without it the
    *second* migration is the one that discovers this, in production.

foreign keys left ``OFF``
    Batch mode copies a table and drops the original. With enforcement on, that
    window looks like a violation to SQLite. Application connections always have
    it on — see ``app/storage/db.py``.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from app.config import get_settings
from app.models.tables import PathType, StringTupleType
from app.storage.db import install_pragmas, sync_url

# Importing the tables is what populates SQLModel.metadata; autogenerate
# compares against it, so a table missing from this import is a table Alembic
# will happily propose dropping.
import app.models.tables  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def database_url() -> str:
    """``-x url=...`` beats the injected URL, which beats the settings default."""
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return sync_url(override)
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return sync_url(configured)
    return sync_url(get_settings().database_url)


def render_item(
    type_: str, obj: Any, autogen_context: AutogenContext
) -> str | Literal[False]:
    """Make autogenerate emit imports for the non-``sa.`` types we use.

    Without this a generated revision references ``PathType`` and
    ``sqlmodel.sql.sqltypes.AutoString`` that nothing imported, and the
    migration fails at import time rather than at review time.
    """
    if type_ == "type":
        if isinstance(obj, PathType):
            autogen_context.imports.add("from app.models.tables import PathType")
            return "PathType()"
        if isinstance(obj, StringTupleType):
            autogen_context.imports.add("from app.models.tables import StringTupleType")
            return "StringTupleType()"
        if obj.__class__.__module__.startswith("sqlmodel."):
            autogen_context.imports.add("import sqlmodel")
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    install_pragmas(connectable, enforce_foreign_keys=False)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            compare_server_default=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
