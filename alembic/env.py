"""Entorno de Alembic. Toma metadata de los modelos y la URL del settings."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from spend_pipe.config import settings
from spend_pipe.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    configure_kwargs = {
        "url": settings.sqlalchemy_database_url,
        "target_metadata": target_metadata,
        "literal_binds": True,
        "dialect_opts": {"paramstyle": "named"},
    }
    if settings.is_sqlite:
        configure_kwargs["render_as_batch"] = True
    context.configure(
        **configure_kwargs,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        configure_kwargs = {
            "connection": connection,
            "target_metadata": target_metadata,
        }
        if settings.is_sqlite:
            configure_kwargs["render_as_batch"] = True
        context.configure(**configure_kwargs)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
