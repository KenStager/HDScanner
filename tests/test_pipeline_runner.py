"""Tests for the dashboard pipeline runner."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from hd.config import Settings
from hd.dashboard._state import PipelineState


@pytest.fixture
def runner_settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        stores="2619,8425",
        brands="Milwaukee",
        product_line_filters="M12,M18",
        store_raw_json=False,
        stage_delay_seconds=0,  # no delay in tests
    )


@pytest.fixture
def fresh_state():
    """Return a fresh PipelineState and patch the module-level singleton."""
    state = PipelineState()
    with patch("hd.dashboard.pipeline_runner.pipeline_state", state):
        yield state


# Patch targets: where the names are looked up (pipeline_runner module)
_DISCOVERY = "hd.dashboard.pipeline_runner.run_discovery"
_SNAPSHOTS = "hd.dashboard.pipeline_runner.run_snapshots"
_DIFF = "hd.dashboard.pipeline_runner.run_diff"
_ALERTS = "hd.dashboard.pipeline_runner.write_alerts"


class TestPipelineRunner:
    async def test_concurrent_run_prevention(self, runner_settings, fresh_state):
        """If lock is held, runner returns immediately without calling pipeline functions."""
        from hd.dashboard.pipeline_runner import run_pipeline_background

        # Acquire the lock before calling runner
        await fresh_state._lock.acquire()

        mock_discovery = AsyncMock(return_value=10)
        with patch(_DISCOVERY, mock_discovery):
            await run_pipeline_background(runner_settings)

        # Discovery should NOT have been called
        mock_discovery.assert_not_called()

        # Clean up lock
        fresh_state._lock.release()

    async def test_state_updated_on_success(self, runner_settings, fresh_state):
        """On successful run, state should have result and no error."""
        from hd.dashboard.pipeline_runner import run_pipeline_background

        with (
            patch(_DISCOVERY, new_callable=AsyncMock, return_value=10),
            patch(_SNAPSHOTS, new_callable=AsyncMock, return_value=50),
            patch(_DIFF, new_callable=AsyncMock, return_value=[]),
            patch(_ALERTS, new_callable=AsyncMock, return_value=0),
        ):
            await run_pipeline_background(runner_settings)

        assert fresh_state.is_running is False
        assert fresh_state.last_run_error is None
        assert fresh_state.last_run_result is not None
        assert fresh_state.last_run_result["products"] == 10
        assert fresh_state.last_run_result["snapshots"] == 50
        assert fresh_state.last_run_result["alerts"] == 0
        assert fresh_state.last_run_ts is not None

    async def test_state_updated_on_failure(self, runner_settings, fresh_state):
        """On failure, state should have error message and is_running=False."""
        from hd.dashboard.pipeline_runner import run_pipeline_background

        with patch(_DISCOVERY, new_callable=AsyncMock, side_effect=Exception("API down")):
            await run_pipeline_background(runner_settings)

        assert fresh_state.is_running is False
        assert fresh_state.last_run_error == "API down"
