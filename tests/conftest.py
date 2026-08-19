"""Test fixtures and configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hd.config import Settings
from hd.db.models import Base


@pytest_asyncio.fixture(autouse=True)
async def _dispose_shared_engine():
    """Dispose the module-level engine after every test.

    hd.db.base keeps one Database instance whose engine is bound to the event
    loop that created it. pytest-asyncio gives each test a fresh loop, so an
    engine left open is reused from a loop that no longer exists and its
    aiosqlite worker thread raises "Event loop is closed" at shutdown.
    """
    yield
    from hd.db.base import close_db

    await close_db()


@pytest.fixture
def sample_response() -> dict:
    """Load the sample searchModel response fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "sample_searchModel_response.json"
    return json.loads(fixture_path.read_text())


@pytest.fixture
def settings() -> Settings:
    """Create a test settings instance using in-memory SQLite."""
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        stores="2619,8452",
        brands="Milwaukee",
        product_line_filters="M12,M18",
        store_raw_json=False,
    )


@pytest_asyncio.fixture
async def db_session(settings: Settings) -> AsyncSession:
    """Provide an async DB session backed by in-memory SQLite."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()
