"""Alembic environment configuration."""

from __future__ import annotations

import os
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Get database URL from environment - this is the primary and most reliable method
sqlalchemy_url = os.getenv(
    "SQLALCHEMY_DATABASE_URI",
    os.getenv("DATABASE_URL", "sqlite:///nexus_dev.db")
)

if not sqlalchemy_url:
    raise ValueError(
        "Database URL not configured! Set SQLALCHEMY_DATABASE_URI or DATABASE_URL environment variable."
    )

# Set the database URL in Alembic config
config.set_main_option("sqlalchemy.url", sqlalchemy_url)

# Target metadata - try to get from Flask app if available, otherwise use None
target_metadata = None
try:
    from flask import has_request_context, current_app
    # Only try to get metadata if we're in an app context
    if has_request_context() or current_app:
        try:
            db = current_app.extensions.get("sqlalchemy")
            if db:
                target_metadata = db.metadata
        except Exception:
            pass
except Exception:
    pass

logger = logging.getLogger("alembic.env")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = sqlalchemy_url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
