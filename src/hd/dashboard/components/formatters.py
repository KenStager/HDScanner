"""Price/date/severity formatting helpers for the dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Union


def fmt_price(val: Union[Decimal, float, int, None]) -> str:
    """Format a price value as $X,XXX.XX or '-' if None."""
    if val is None:
        return "-"
    return f"${val:,.2f}"


def fmt_pct(val: Union[int, float, None]) -> str:
    """Format a percentage value as XX% or '-' if None."""
    if val is None:
        return "-"
    return f"{val}%"


def fmt_ts(val: Union[datetime, str, None]) -> str:
    """Format a timestamp as YYYY-MM-DD HH:MM:SS or '-' if None."""
    if val is None:
        return "-"
    if isinstance(val, str):
        return val[:19]
    return val.strftime("%Y-%m-%d %H:%M:%S")


def severity_color(severity: str) -> str:
    """Return a CSS color name for the given severity level."""
    mapping = {
        "low": "blue",
        "medium": "orange",
        "high": "red",
    }
    return mapping.get(severity, "grey")


def alert_type_icon(alert_type: str) -> str:
    """Return a Material icon name for the given alert type."""
    mapping = {
        "PRICE_DROP": "trending_down",
        "CLEARANCE": "local_offer",
        "SPECIAL_BUY": "star",
        "BACK_IN_STOCK": "inventory",
        "OOS": "remove_shopping_cart",
        "HEALTH_DEGRADED": "warning",
    }
    return mapping.get(alert_type, "info")


def stock_badge(in_stock: bool | None) -> tuple[str, str]:
    """Return (label, color) tuple for a stock status badge."""
    if in_stock is None:
        return ("Unknown", "blue-grey")
    if in_stock:
        return ("In Stock", "green")
    return ("Out of Stock", "red")


def fmt_pct_nonzero(val: Union[int, float, None]) -> str:
    """Format a percentage, returning '-' for None and 0."""
    if val is None or val == 0:
        return "-"
    return f"{val}%"


def fmt_savings_center(val: str | None) -> str:
    """Map raw HD savings_center values to human-readable labels."""
    if not val:
        return "-"
    _MAP = {
        "CLEARANCE": "Clearance",
        "SPECIAL_BUY": "Special Buy",
        "SPECIAL_BUYS": "Special Buy",
    }
    return _MAP.get(val.upper(), val.replace("_", " ").title())


def fmt_ts_relative(val: Union[datetime, str, None]) -> str:
    """Return a relative time string like '2h ago', '3d ago', 'just now'."""
    if val is None:
        return "-"
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except (ValueError, TypeError):
            return val[:19]
    # Make both sides offset-aware for comparison
    now = datetime.now(timezone.utc)
    if val.tzinfo is None:
        val = val.replace(tzinfo=timezone.utc)
    diff = now - val
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago"


def fmt_observed_drop(
    current_price: Union[float, int, None],
    baseline_price: Union[float, int, None],
) -> str | None:
    """Return a formatted observed-drop string or None if no drop has occurred.

    Uses the first-ever recorded price (baseline_price) as the reference point,
    not the API's price_original field (which is the sum of individual tool prices
    for combo kits and does not reflect a real historical selling price).

    Returns None when:
    - Either value is missing
    - Current price is at or above baseline (no drop, or price went up)
    - Baseline is zero or negative (guard against division by zero)
    """
    if current_price is None or baseline_price is None:
        return None
    if baseline_price <= 0:
        return None
    if current_price >= baseline_price:
        return None
    pct = (baseline_price - current_price) / baseline_price * 100
    return f"{pct:.0f}% below baseline"


def product_status_badge(
    savings_centers: list[str | None],
    price_pairs: list[tuple[float | None, float | None]],
) -> tuple[str, str] | None:
    """Return (label, color) for a product's status badge, or None.

    Priority: CLEARANCE (red) > largest observed price drop (orange) > None.

    Args:
        savings_centers: savings_center value per store (may contain None).
        price_pairs: (current_price, baseline_price) per store.
    """
    # Clearance wins if any store reports it
    if any(sc == "CLEARANCE" for sc in savings_centers if sc is not None):
        return ("CLEARANCE", "red")

    # Compute the largest observed drop across stores
    max_drop_pct: float = 0.0
    for current, baseline in price_pairs:
        if current is None or baseline is None or baseline <= 0:
            continue
        if current < baseline:
            drop = (baseline - current) / baseline * 100
            if drop > max_drop_pct:
                max_drop_pct = drop

    if max_drop_pct > 0:
        return (f"{max_drop_pct:.0f}% drop", "orange")

    return None


def fmt_inventory_qty(qty: int | None, in_stock: bool | None) -> str:
    """Return inventory quantity string, falling back to stock status label."""
    if qty is not None and qty > 0:
        return f"{qty} units"
    label, _ = stock_badge(in_stock)
    return label


def format_price_change(alert_type: str, payload: dict | None) -> str:
    """Return a rich one-line summary of an alert's price/stock change."""
    if not payload:
        return ""
    before = payload.get("before", {})
    after = payload.get("after", {})

    if alert_type == "PRICE_DROP":
        b_price = fmt_price(before.get("price_value"))
        a_price = fmt_price(after.get("price_value"))
        pct = payload.get("pct_drop")
        pct_str = f" (-{pct:.0f}%)" if pct else ""
        return f"{b_price} → {a_price}{pct_str}"

    if alert_type == "CLEARANCE":
        a_price = fmt_price(after.get("price_value"))
        pct_off = after.get("percentage_off")
        pct_str = f" ({pct_off}% off)" if pct_off else ""
        return f"{a_price}{pct_str}"

    if alert_type in ("OOS", "BACK_IN_STOCK"):
        b_label, _ = stock_badge(before.get("in_stock"))
        a_label, _ = stock_badge(after.get("in_stock"))
        return f"{b_label} → {a_label}"

    if alert_type == "SPECIAL_BUY":
        a_price = fmt_price(after.get("price_value"))
        return f"Special Buy at {a_price}"

    title = payload.get("product_title", "")
    return title[:50] if title else ""


def format_alert_details(alert_type: str, payload: dict | None) -> str:
    """Format alert payload into a human-readable details string."""
    if not payload:
        return ""
    if alert_type == "PRICE_DROP":
        before = payload.get("before", {}).get("price_value", "?")
        after = payload.get("after", {}).get("price_value", "?")
        return f"${before} → ${after}"
    if alert_type == "CLEARANCE":
        pct = payload.get("after", {}).get("percentage_off", "?")
        return f"{pct}% off"
    title = payload.get("product_title", "")
    return title[:50] if title else ""
