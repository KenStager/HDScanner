"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hd.config import Settings
from hd.db.models import Base


def _get_engine_kwargs(url: str) -> dict:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


class Database:
    """Holds engine and session factory as instance state."""

    def __init__(self) -> None:
        self._engine = None
        self._session_factory = None

    def get_engine(self, settings: Settings | None = None):
        if self._engine is None:
            if settings is None:
                settings = Settings()
            self._engine = create_async_engine(
                settings.database_url,
                echo=False,
                **_get_engine_kwargs(settings.database_url),
            )
        return self._engine

    def get_session_factory(self, settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            engine = self.get_engine(settings)
            self._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return self._session_factory

    @asynccontextmanager
    async def get_session(self, settings: Settings | None = None) -> AsyncGenerator[AsyncSession, None]:
        factory = self.get_session_factory(settings)
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def init_db(self, settings: Settings | None = None) -> None:
        """Create all tables directly (for dev/init use)."""
        engine = self.get_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close_db(self) -> None:
        """Dispose of the engine."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


# Default instance + backward-compatible module-level functions
_default = Database()


def get_engine(settings: Settings | None = None):
    return _default.get_engine(settings)


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    return _default.get_session_factory(settings)


def get_session(settings: Settings | None = None) -> AsyncGenerator[AsyncSession, None]:
    return _default.get_session(settings)


async def init_db(settings: Settings | None = None) -> None:
    return await _default.init_db(settings)


async def close_db() -> None:
    return await _default.close_db()
