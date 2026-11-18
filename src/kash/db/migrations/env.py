import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import kash.db.models  # noqa: F401 - Register all models with Base.metadata
from kash.config import get_settings
from kash.db.base import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # Keep loggers that already exist (e.g. kash.*) working after the CLI configures logging
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Database URL priority:
#   1. `alembic -x url=...` on the command line
#   2. A real URL set programmatically on the Config (e.g. by tests)
#   3. Kash settings (DATABASE_URL env var / .env)
# The placeholder in alembic.ini is never used.
x_args = context.get_x_argument(as_dictionary=True)
cli_url = x_args.get("url") or x_args.get("sqlalchemy.url")
current_url = config.get_main_option("sqlalchemy.url") or ""
if cli_url:
    config.set_main_option("sqlalchemy.url", cli_url)
elif not current_url or current_url.startswith("driver://"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # Enables SQLite compatibility for schema changes
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
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
