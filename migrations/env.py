import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from repository.models import Base


def run_migrations_offline() -> None:
    # 带 --sql 时按 MySQL 语法生成 SQL，不读取数据库连接地址，也不联网。
    context.configure(
        dialect_name="mysql",
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations(connection: Connection) -> None:
    # 把当前表模型交给 Alembic，供比较数据库表结构使用。
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    value = Settings().support_database_url
    if not value:
        raise SystemExit("Set SUPPORT_DATABASE_URL in .env before running migrations.")
    url = make_url(value)
    if url.drivername != "mysql+asyncmy" or not url.database:
        raise SystemExit("SUPPORT_DATABASE_URL must use mysql+asyncmy and name the support database.")

    # 迁移是一次性命令，不需要复用数据库连接。
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            # 用异步连接运行 Alembic 的同步迁移函数。
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
