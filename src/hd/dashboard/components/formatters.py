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
        "DEEP_DISCOUNT": "local_fire_department",
        "BACK_IN_STOCK": "inventory",
        "OOS": "remove_shopping_cart",
        "IN_STORE_CLEARANCE": "store",
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


def infer_in_stock(data: dict) -> bool | None:
    """Infer in_stock from inventory_qty when the API didn't provide isInStock."""
    in_stock = data.get("in_stock")
    if in_stock is not None:
        return in_stock
    qty = data.get("inventory_qty")
    if qty is not None:
        return qty > 0
    return None


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
        "DEEP_DISCOUNT": "Deep Discount",
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


def fmt_history_span(days: Union[int, None], cap_days: int) -> str:
    """Describe how much price history backs a verdict, e.g. '3d' or '3mo+'.

    The honesty chip must not claim more history than it has, so the span is
    reported as observed and only rounded up once it reaches cap_days — the
    point beyond which snapshots are pruned and the true span is unknowable.
    Stays a compact token so it reads as a unit inside a chip ("flat 3mo+
    price"), matching the 'mo' abbreviation fmt_ts_relative already uses.
    """
    if days is None:
        return "-"
    if days >= cap_days:
        return f"{max(1, cap_days // 30)}mo+"
    return f"{days}d"


def fmt_low_date(val: Union[datetime, str, None]) -> str:
    """Date a witnessed low was set, e.g. 'May 10' — or 'May 10 2025' across years.

    The date is what makes the anchor auditable: a price with no date is a claim,
    a price with a date is a record the reader can check.
    """
    if val is None:
        return ""
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except (ValueError, TypeError):
            return ""
    now = datetime.now(timezone.utc)
    if val.year != now.year:
        return val.strftime("%b %-d %Y")
    return val.strftime("%b %-d")


def store_price_verdict(
    effective_price: Union[float, int, None],
    stats: dict | None,
    recency_days: int | None = None,
) -> tuple[str, str] | None:
    """Our history's one-chip verdict on the price you'd pay at a store today.

    Returns (label, css_class) in the deal-board chip vocabulary, or None when
    the record has nothing to say — silence is the correct empty state, and a
    single observation is not evidence worth a chip.

    The verdict compares against witnessed prices only (gap-immune facts from
    item_price_stats), never a percentage across the coverage gap.

    recency_days applies the deal board's warning-recency rule (see
    deal_tier): a low set within the bound keeps the warning dress; an older
    low is dated context, one salience tier down — the SAME words either
    way, so every surface tells the same fact and only the urgency differs.
    None keeps the warning dress at any age.
    """
    if effective_price is None or not stats:
        return None
    low = stats.get("low_price")
    high = stats.get("high_price")
    if low is None or high is None:
        return None
    if (stats.get("obs_days") or 0) < 2:
        return None

    if effective_price < low:
        # Cheaper than everything we ever witnessed (e.g. a clearance price
        # under a shelf price that never moved)
        return ("below recorded low", "best")
    if low != high:
        if effective_price <= low:
            return ("lowest recorded", "best")
        when = fmt_low_date(stats.get("low_ts"))
        label = f"seen ${low:,.2f}" + (f" · {when}" if when else "")
        cls = "above"
        low_ts = stats.get("low_ts")
        if recency_days is not None and low_ts is not None:
            if isinstance(low_ts, str):
                try:
                    low_ts = datetime.fromisoformat(low_ts)
                except (ValueError, TypeError):
                    low_ts = None
            if low_ts is not None:
                if low_ts.tzinfo is None:
                    low_ts = low_ts.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - low_ts
                if age.days > recency_days:
                    cls = "context"
        return (label, cls)
    # Never varied — say so, dated, so the claim is auditable
    since = fmt_low_date(stats.get("first_ts"))
    if since:
        return (f"flat since {since}", "flat")
    return None


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

    if alert_type == "DEEP_DISCOUNT":
        a_price = fmt_price(after.get("price_value"))
        pct_off = payload.get("percentage_off") or after.get("percentage_off")
        pct_str = f" ({pct_off}% off)" if pct_off else ""
        return f"Deep discount: {a_price}{pct_str}"

    if alert_type == "IN_STORE_CLEARANCE":
        cl_price = fmt_price(payload.get("clearance_value"))
        pct = payload.get("clearance_percentage_off")
        pct_str = f" ({pct}% off)" if pct else ""
        prev = payload.get("prev_clearance_value")
        if prev is not None:
            return f"In-store clearance {fmt_price(prev)} → {cl_price}{pct_str}"
        return f"In-store clearance {cl_price}{pct_str}"

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
    if alert_type == "IN_STORE_CLEARANCE":
        cl = payload.get("clearance_value")
        pct = payload.get("clearance_percentage_off")
        return f"${cl} ({pct}% off)" if cl else ""
    title = payload.get("product_title", "")
    return title[:50] if title else ""
