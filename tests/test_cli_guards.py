"""Tests for the "run hd setup first" guards.

Shipped defaults are empty so nothing scans a stranger's stores, which means
an unconfigured run must stop with a pointer to setup. The subtlety — and the
bug these pin — is that the guard must check what the command will actually
use, not what the config says: `hd snapshot --stores 2619` supplies its own
store and has to keep working on an install that has never been configured.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from hd.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch, tmp_path):
    """An install with no stores, brands or tokens."""
    for key in ("STORES", "BRANDS", "BRAND_TOKENS", "PRODUCT_LINE_FILTERS"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/guard.db")
    monkeypatch.chdir(tmp_path)


class TestUnconfiguredIsRefused:
    @pytest.mark.parametrize("args", [["snapshot"], ["browse"], ["run-once"], ["catch-up"]])
    def test_store_commands_stop(self, args):
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "hd setup" in result.output

    def test_discover_stops_without_brands(self):
        result = runner.invoke(app, ["discover"])
        assert result.exit_code == 1
        assert "hd setup" in result.output


class TestExplicitArgumentsStillWork:
    """The guard must not block a command that was handed its arguments."""

    def test_snapshot_accepts_explicit_stores(self, monkeypatch):
        seen = {}

        async def fake(*, settings, store_ids, limit):
            seen["stores"] = store_ids
            return 0

        monkeypatch.setattr("hd.pipeline.snapshot.run_snapshots", fake)
        result = runner.invoke(app, ["snapshot", "--stores", "2619"])
        assert result.exit_code == 0, result.output
        assert seen["stores"] == ["2619"]

    def test_discover_accepts_explicit_brand(self, monkeypatch):
        seen = {}

        async def fake(*, settings, brands, **kw):
            seen["brands"] = brands
            return 0

        monkeypatch.setattr("hd.pipeline.discovery.run_discovery", fake)
        result = runner.invoke(app, ["discover", "--brand", "DEWALT"])
        assert result.exit_code == 0, result.output
        assert seen["brands"] == ["DEWALT"]  # --brand is a repeatable option

    def test_browse_accepts_explicit_stores(self, monkeypatch):
        from hd.pipeline.browse import BrowseSummary

        seen = {}

        async def fake(*, settings, store_ids, tiers):
            seen["stores"] = store_ids
            return BrowseSummary()

        monkeypatch.setenv("BRAND_TOKENS", "RYOBI:m5d")
        monkeypatch.setattr("hd.pipeline.browse.run_browse", fake)
        result = runner.invoke(app, ["browse", "--stores", "6542", "--tier", "shelf"])
        assert result.exit_code == 0, result.output
        assert seen["stores"] == ["6542"]

    def test_browse_still_requires_tokens(self, monkeypatch):
        """Browse walks only brands with a token, so without one it scans nothing."""
        result = runner.invoke(app, ["browse", "--stores", "6542"])
        assert result.exit_code == 1
        assert "token" in result.output.lower()


class TestUnguardedCommands:
    """Reading commands must work on an unconfigured install."""

    @pytest.mark.parametrize("args", [["--help"], ["alerts", "--limit", "1"], ["health"]])
    def test_no_guard(self, args):
        assert runner.invoke(app, args).exit_code == 0
