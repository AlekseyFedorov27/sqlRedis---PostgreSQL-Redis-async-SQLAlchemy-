# alembic/env.py
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 🔽 ЗАМЕНИ ЭТИ ИМПОРТЫ НА СВОИ ИЗ start.py
# Например, если Base и модели лежат в start.py в той же папке:
from start import Base
# Или если ты вынес модели в отдельный файл:
# from models import Base
# ==========================================

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (без подключения к БД)."""
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
    """Синхронная функция, которую вызовет run_sync."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,  # ловит изменения типов колонок
        # compare_server_default=True,  # раскомментируй, если хочешь ловить изменения DEFAULT
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Асинхронная обёртка: создаёт движок и гоняет миграции через run_sync."""
    # URL берётся из alembic.ini, если он там задан.
    # Если у тебя URL хранится в коде (например, в start.py), импортируй его и вставь сюда:
    # from start import DATABASE_URL
    # config.set_main_option("sqlalchemy.url", DATABASE_URL)
    from start import DATABASE_URL  # или как у тебя называется переменная
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Точка входа для онлайн-миграций."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()