"""Tests for the Slack canvas deal rundown feature."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from hd.notifiers.canvas import (
    _sort_deals,
    _format_deal_line,
    format_canvas_markdown,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _deal(
    *,
    title: str = "Test Tool",
    deal_type: str = "in_store_clearance",
    effective_price: float = 50.0,
    effective_discount_pct: int = 50,
    price_value: float = 100.0,
    clearance_value: float | None = 50.0,
    savings_center: str | None = None,
    in_stock: bool | None = True,
    inventory_qty: int | None = 3,
    deal_age_ts: datetime | None = None,
    product_url: str = "https://www.homedepot.com/p/123",
    item_id: str = "123",
    discount_observed: bool = True,
) -> dict:
    return {
        "item_id": item_id,
        "title": title,
        "product_url": product_url,
        "price_value": price_value,
        "clearance_value": clearance_value,
        "savings_center": savings_center,
        "deal_type": deal_type,
        "effective_price": effective_price,
        "effective_discount_pct": effective_discount_pct,
        "discount_observed": discount_observed,
        "in_stock": in_stock,
        "inventory_qty": inventory_qty,
        "deal_age_ts": deal_age_ts or datetime(2026, 3, 20, tzinfo=timezone.utc),
    }


# ── _sort_deals ───────────────────────────────────────────────────────────────


class TestSortDeals:
    def test_higher_discount_first(self):
        low = _deal(effective_discount_pct=20)
        high = _deal(effective_discount_pct=80)
        result = _sort_deals([low, high])
        assert result[0]["effective_discount_pct"] == 80
        assert result[1]["effective_discount_pct"] == 20

    def test_none_discount_treated_as_zero(self):
        no_pct = _deal(effective_discount_pct=None)
        has_pct = _deal(effective_discount_pct=50)
        result = _sort_deals([no_pct, has_pct])
        assert result[0]["effective_discount_pct"] == 50


# ── _format_deal_line ─────────────────────────────────────────────────────────


class TestFormatDealLine:
    def test_in_store_clearance_format(self):
        d = _deal(
            title="M18 Battery",
            deal_type="in_store_clearance",
            effective_price=25.0,
            effective_discount_pct=81,
            price_value=129.0,
            in_stock=True,
            inventory_qty=4,
        )
        line = _format_deal_line(d)
        assert "M18 Battery" in line
        assert "$25.00" in line
        assert "81% off" in line
        assert "Online: $129.00" in line
        assert "4 units" in line
        assert line.startswith("- ")

    def test_online_deal_format(self):
        d = _deal(
            title="M12 Ratchet",
            deal_type="online",
            effective_price=98.10,
            effective_discount_pct=51,
            savings_center="Special Buys",
            clearance_value=None,
            in_stock=True,
            inventory_qty=4,
        )
        line = _format_deal_line(d)
        assert "M12 Ratchet" in line
        assert "$98.10" in line
        assert "51% off" in line
        assert "Special Buys" in line

    def test_observed_discount_reads_as_measured(self):
        d = _deal(deal_type="online", effective_discount_pct=51,
                  discount_observed=True)
        assert "51% off" in _format_deal_line(d)

    def test_unobserved_discount_is_labelled_as_hds_claim(self):
        """When we measured no drop of our own, HD's number must read as a
        claim — never as a discount we confirmed."""
        d = _deal(deal_type="online", effective_discount_pct=47,
                  discount_observed=False)
        line = _format_deal_line(d)
        assert "HD claims 47%" in line
        assert "47% off" not in line

    def test_title_is_linked(self):
        d = _deal(title="Test Tool", product_url="https://www.homedepot.com/p/123")
        line = _format_deal_line(d)
        assert "[Test Tool](https://www.homedepot.com/p/123)" in line

    def test_two_line_format(self):
        d = _deal(in_stock=True, inventory_qty=7)
        line = _format_deal_line(d)
        lines = line.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("- ")
        assert lines[1].startswith("  ")

    def test_singular_unit(self):
        d = _deal(in_stock=True, inventory_qty=1)
        line = _format_deal_line(d)
        assert "1 unit" in line
        assert "1 units" not in line

    def test_plural_units(self):
        d = _deal(in_stock=True, inventory_qty=5)
        line = _format_deal_line(d)
        assert "5 units" in line

    def test_stock_without_qty(self):
        d = _deal(in_stock=True, inventory_qty=None)
        line = _format_deal_line(d)
        assert "In Stock" in line

    def test_first_seen_label(self):
        d = _deal()
        line = _format_deal_line(d)
        assert "First seen" in line


# ── format_canvas_markdown ────────────────────────────────────────────────────


class TestFormatCanvasMarkdown:
    def test_empty_deals(self):
        md = format_canvas_markdown({}, {"2619": "Local Store"})
        assert "# Deal Rundown" in md

    def test_title_is_configurable(self):
        """The heading was hardcoded to one brand; it is a setting now."""
        md = format_canvas_markdown({}, {"2619": "Local Store"}, title="Ryobi Watch")
        assert md.startswith("# Ryobi Watch")
        assert "Store 2619" in md
        assert "No in-store clearance deals" in md
        assert "No online deals" in md

    def test_in_store_clearance_section(self):
        deals = {
            "2619": [
                _deal(title="Socket Set", deal_type="in_store_clearance", in_stock=True),
            ],
        }
        md = format_canvas_markdown(deals, {"2619": "Test"})
        assert "### In-Store Clearance (1 items)" in md
        assert "Socket Set" in md

    def test_online_deals_section(self):
        deals = {
            "2619": [
                _deal(title="M12 Ratchet", deal_type="online", savings_center="Special Buys"),
            ],
        }
        md = format_canvas_markdown(deals, {"2619": "Test"})
        assert "### Online Deals (1 items)" in md
        assert "M12 Ratchet" in md

    def test_sorted_by_discount_in_output(self):
        deals = {
            "2619": [
                _deal(title="Low Discount", deal_type="in_store_clearance", effective_discount_pct=20, item_id="a"),
                _deal(title="High Discount", deal_type="in_store_clearance", effective_discount_pct=80, item_id="b"),
            ],
        }
        md = format_canvas_markdown(deals, {"2619": "Test"})
        high_pos = md.find("High Discount")
        low_pos = md.find("Low Discount")
        assert high_pos < low_pos

    def test_multiple_stores(self):
        deals = {
            "2619": [_deal(title="Tool A", item_id="a")],
            "8452": [_deal(title="Tool B", item_id="b")],
        }
        md = format_canvas_markdown(deals, {"2619": "Store A", "8452": "Store B"})
        assert "## Store 2619" in md
        assert "## Store 8452" in md
        assert "---" in md  # divider between stores

    def test_clearance_trumps_online(self):
        """Items with both clearance_value and savings_center should be in clearance section."""
        deals = {
            "2619": [
                _deal(
                    title="Dual Signal",
                    deal_type="in_store_clearance",  # already classified correctly
                    clearance_value=25.0,
                    savings_center="CLEARANCE",
                ),
            ],
        }
        md = format_canvas_markdown(deals, {"2619": "Test"})
        assert "### In-Store Clearance (1 items)" in md
        assert "### Online Deals (0 items)" in md

    def test_store_order_respected(self):
        deals = {
            "8452": [_deal(title="B", item_id="b")],
            "2619": [_deal(title="A", item_id="a")],
        }
        md = format_canvas_markdown(
            deals, {"2619": "First", "8452": "Second"}, store_order=["2619", "8452"]
        )
        pos_2619 = md.find("Store 2619")
        pos_8452 = md.find("Store 8452")
        assert pos_2619 < pos_8452

    def test_truncation_on_large_content(self):
        # Generate enough deals to exceed MARKDOWN_MAX_CHARS
        big_deals = {
            "2619": [
                _deal(
                    title=f"Super Long Product Name Number {i} With Extra Text To Fill Space " * 3,
                    item_id=str(i),
                    product_url=f"https://www.homedepot.com/p/{i}" + "x" * 200,
                )
                for i in range(500)
            ],
        }
        md = format_canvas_markdown(big_deals, {"2619": "Test"})
        assert len(md) <= 36_000  # some buffer for the truncation note
        assert "truncated" in md


# ── Canvas ID persistence ─────────────────────────────────────────────────────


def _mock_session_ctx():
    """Build a mock async context manager for get_session that returns empty stores."""
    from unittest.mock import MagicMock

    mock_session = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value = mock_scalars

    # session.execute() is awaited, so make it an AsyncMock returning sync result
    mock_session.execute = AsyncMock(return_value=mock_execute_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx


class TestCanvasIdPersistence:
    @pytest.mark.asyncio
    async def test_create_writes_id_file(self, tmp_path):
        from hd.notifiers.canvas import run_canvas_update

        canvas_file = tmp_path / ".hd_canvas_id"

        settings = AsyncMock()
        settings.canvas_id_path = str(canvas_file)
        settings.slack_bot_token = "xoxb-test"
        settings.slack_channel_id = "C123"
        settings.store_list = ["2619"]

        with (
            patch("hd.notifiers.canvas.get_active_deals", new_callable=AsyncMock, return_value={}),
            patch("hd.notifiers.canvas.get_session", return_value=_mock_session_ctx()),
            patch("hd.notifiers.canvas.create_canvas", new_callable=AsyncMock, return_value="F_canvas_123"),
        ):
            await run_canvas_update(settings, dry_run=False)

        assert canvas_file.exists()
        assert canvas_file.read_text() == "F_canvas_123"

    @pytest.mark.asyncio
    async def test_update_uses_existing_id(self, tmp_path):
        from hd.notifiers.canvas import run_canvas_update

        canvas_file = tmp_path / ".hd_canvas_id"
        canvas_file.write_text("F_existing_456")

        settings = AsyncMock()
        settings.canvas_id_path = str(canvas_file)
        settings.slack_bot_token = "xoxb-test"
        settings.slack_channel_id = "C123"
        settings.store_list = ["2619"]

        with (
            patch("hd.notifiers.canvas.get_active_deals", new_callable=AsyncMock, return_value={}),
            patch("hd.notifiers.canvas.get_session", return_value=_mock_session_ctx()),
            patch("hd.notifiers.canvas.update_canvas", new_callable=AsyncMock, return_value=True) as mock_update,
        ):
            await run_canvas_update(settings, dry_run=False)

        mock_update.assert_called_once()
        assert mock_update.call_args[0][1] == "F_existing_456"

    @pytest.mark.asyncio
    async def test_recreate_on_not_found(self, tmp_path):
        from hd.notifiers.canvas import run_canvas_update

        canvas_file = tmp_path / ".hd_canvas_id"
        canvas_file.write_text("F_deleted_789")

        settings = AsyncMock()
        settings.canvas_id_path = str(canvas_file)
        settings.slack_bot_token = "xoxb-test"
        settings.slack_channel_id = "C123"
        settings.store_list = ["2619"]

        with (
            patch("hd.notifiers.canvas.get_active_deals", new_callable=AsyncMock, return_value={}),
            patch("hd.notifiers.canvas.get_session", return_value=_mock_session_ctx()),
            patch("hd.notifiers.canvas.update_canvas", new_callable=AsyncMock, return_value=False) as mock_update,
            patch("hd.notifiers.canvas.create_canvas", new_callable=AsyncMock, return_value="F_new_999") as mock_create,
        ):
            await run_canvas_update(settings, dry_run=False)

        mock_update.assert_called_once()
        mock_create.assert_called_once()
        assert canvas_file.read_text() == "F_new_999"


# ── Dry run ───────────────────────────────────────────────────────────────────


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_skips_slack(self):
        from hd.notifiers.canvas import run_canvas_update

        settings = AsyncMock()
        settings.store_list = ["2619"]
        settings.canvas_enabled = True
        settings.canvas_title = "Deal Rundown"

        with (
            patch("hd.notifiers.canvas.get_active_deals", new_callable=AsyncMock, return_value={}),
            patch("hd.notifiers.canvas.get_session", return_value=_mock_session_ctx()),
            patch("hd.notifiers.canvas.create_canvas", new_callable=AsyncMock) as mock_create,
            patch("hd.notifiers.canvas.update_canvas", new_callable=AsyncMock) as mock_update,
        ):
            markdown, count = await run_canvas_update(settings, dry_run=True)

        mock_create.assert_not_called()
        mock_update.assert_not_called()
        assert "# Deal Rundown" in markdown
