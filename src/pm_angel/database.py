from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

engine = None
async_session: async_sessionmaker[AsyncSession] | None = None


async def init_db(db_path: str) -> None:
    global engine, async_session

    from pm_angel.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    if async_session is None:
        raise RuntimeError("Database not initialized. Call init_db first.")
    async with async_session() as session:
        yield session


async def close_db() -> None:
    global engine
    if engine:
        await engine.dispose()
