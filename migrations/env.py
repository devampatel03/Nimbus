# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import src.personality.agent_character
import src.personality.user_profile  # noqa: F401  (registers user_trait, trait_evidence)
from config.settings import settings
from src.memory.schemas import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # disable_existing_loggers=False: otherwise running migrations in-process
    # (e.g. from tests) silently disables every already-created app logger.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# `src.memory.schemas` imports every model module so Base.metadata is
# complete (graph_node, graph_edge, session, commitment, decision, person).
target_metadata = Base.metadata

# Default to the app DSN, but respect a URL already set programmatically
# (tests point this at a scratch database). The alembic.ini placeholder
# ("driver://...") counts as unset.
_existing_url = config.get_main_option("sqlalchemy.url") or ""
if _existing_url.startswith("driver://") or not _existing_url:
    config.set_main_option("sqlalchemy.url", settings.postgres_dsn)

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
