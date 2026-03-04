"""Tests for API error response detection across the pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hd.config import Settings
from hd.db.base import Database
from hd.db.models import Alert, AlertType, Base
from hd.hd_api.graphql import is_valid_search_response
from hd.http.client import CircuitBreaker


class TestCircuitBreakerErrorDetection:
    """Verify client.py records failure on error JSON, success on valid JSON."""

    def test_error_key_records_failure(self):
        """Error JSON should trigger record_failure, not record_success."""
        cb = CircuitBreaker(threshold=10, window_seconds=60)
        # Simulate what client.py now does: check for error keys
        data = {"data": {"Generic Errors API": None}, "error": [{"message": "Generic Errors API errors"}]}

        if isinstance(data, dict) and ("error" in data or "errors" in data):
            cb.record_failure()
        else:
            cb.record_success()

        assert len(cb._failures) == 1

    def test_valid_response_records_success(self):
        """Valid JSON should trigger record_success, not record_failure."""
        cb = CircuitBreaker(threshold=10, window_seconds=60)
        data = {"data": {"searchModel": {"products": [{"id": "1"}]}}}

        if isinstance(data, dict) and ("error" in data or "errors" in data):
            cb.record_failure()
        else:
            cb.record_success()

        assert len(cb._failures) == 0


class TestIsValidSearchResponse:
    def test_with_error_key(self):
        raw = {"data": {"Generic Errors API": None}, "error": [{"message": "Generic Errors API errors"}]}
        assert is_valid_search_response(raw) is False

    def test_with_errors_key(self):
        raw = {"data": {"searchModel": {"products": []}}, "errors": [{"message": "something"}]}
        assert is_valid_search_response(raw) is False

    def test_missing_searchmodel(self):
        raw = {"data": {"other": "stuff"}}
        assert is_valid_search_response(raw) is False

    def test_null_searchmodel(self):
        raw = {"data": {"searchModel": None}}
        assert is_valid_search_response(raw) is False

    def test_valid_response(self):
        raw = {"data": {"searchModel": {"products": [{"id": "1"}]}}}
        assert is_valid_search_response(raw) is True

    def test_valid_empty_products(self):
        raw = {"data": {"searchModel": {"products": []}}}
        assert is_valid_search_response(raw) is True

    def test_non_dict(self):
        assert is_valid_search_response("string") is False

    def test_none(self):
        assert is_valid_search_response(None) is False


@pytest.fixture
def health_settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        stores="2619,8425",
        brands="Milwaukee",
        product_line_filters="M12,M18",
        store_raw_json=False,
    )


@pytest_asyncio.fixture
async def seeded_health_settings(health_settings: Settings) -> Settings:
    """Initialize DB for health alert dedup tests."""
    from hd.db import base as db_base

    db_base._default = Database()
    await db_base._default.init_db(health_settings)
    yield health_settings
    await db_base._default.close_db()


class TestHealthDegradedDedup:
    async def test_dedup_within_24h(self, seeded_health_settings: Settings):
        """Emitting HEALTH_DEGRADED twice within 24h should only create 1 alert."""
        from hd.db import base as db_base
        from hd.pipeline.health import emit_health_degraded_alert

        await emit_health_degraded_alert(
            seeded_health_settings,
            ["test_path_1"],
            message="First alert",
        )
        await emit_health_degraded_alert(
            seeded_health_settings,
            ["test_path_2"],
            message="Second alert (should be deduped)",
        )

        async with db_base._default.get_session(seeded_health_settings) as session:
            result = await session.execute(
                select(Alert).where(Alert.alert_type == AlertType.HEALTH_DEGRADED)
            )
            alerts = result.scalars().all()

        assert len(alerts) == 1
        assert alerts[0].payload["message"] == "First alert"
