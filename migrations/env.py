"""Alembic environment.

The database URL comes from application settings, so migrations and the
service can never drift onto different databases.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from apps.orchestrator.config import get_settings

# Importing the models registers every table on Base.metadata.
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.db.types import StrEnumType

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _render_item(type_, obj, autogen_context):
    """Render StrEnumType as a plain String in migrations.

    The custom type exists only to validate values in Python; at the database
    level it is a VARCHAR, and rendering it as one keeps generated migrations
    free of application imports.
    """
    if type_ == "type" and isinstance(obj, StrEnumType):
        autogen_context.imports.add("import sqlalchemy as sa")
        return f"sa.String(length={obj.impl_instance.length})"
    return False


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_item=_render_item,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_db_engine(_database_url())
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
            compare_type=True,
            render_item=_render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
