"""Alembic environment configuration for Flask-SQLAlchemy migrations."""

from __future__ import annotations

import os
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

# Get database URL from environment variable set by Flask
sqlalchemy_url = os.getenv(
    "SQLALCHEMY_DATABASE_URI",
    "sqlite:///nexus_dev.db"
)

# Try to get target metadata from Flask app if possible
try:
    from flask import current_app
    if current_app:
        config.set_main_option(
            "sqlalchemy.url",
            current_app.config.get(
                "SQLALCHEMY_DATABASE_URI",
                sqlalchemy_url
            )
        )
        from flask_sqlalchemy import SQLAlchemy
        db = current_app.extensions.get("sqlalchemy")
        if db:
            target_metadata = db.metadata
        else:
            target_metadata = None
    else:
        target_metadata = None
except Exception as e:
    logger.warning(f"Could not get metadata from Flask app: {e}")
    target_metadata = None
    # Set URL from environment if not in Flask app context
    if "sqlalchemy.url" not in config.config:
        config.set_main_option("sqlalchemy.url", sqlalchemy_url)


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


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = (
        config.get_main_option("sqlalchemy.url")
        or os.getenv("SQLALCHEMY_DATABASE_URI", "sqlite:///nexus_dev.db")
    )

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
