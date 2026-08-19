"""Slack canvas — persistent daily deal rundown per store.

Queries the latest snapshot state, formats a markdown document organized
by store, and creates or updates a Slack canvas via the canvases API.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, select

from hd.config import Settings
from hd.dashboard.components.formatters import fmt_price, fmt_ts_relative, infer_in_stock
from hd.dashboard.queries import _first_price_subquery, _latest_snapshots_subquery
from hd.db.base import get_session
from hd.db.models import Alert, AlertType, Product, Store, StoreSnapshot
from hd.logging import get_logger

log = get_logger("notifiers.canvas")

CANVAS_TITLE = "Milwaukee Deal Rundown"
MARKDOWN_MAX_CHARS = 35_000

SLACK_CANVAS_CREATE_URL = "https://slack.com/api/canvases.create"
SLACK_CANVAS_EDIT_URL = "https://slack.com/api/canvases.edit"

_DEAL_ALERT_TYPES = {
    AlertType.PRICE_DROP,
    AlertType.CLEARANCE,
    AlertType.SPECIAL_BUY,
    AlertType.DEEP_DISCOUNT,
    AlertType.IN_STORE_CLEARANCE,
}


# ── Data query ────────────────────────────────────────────────────────────────


async def get_active_deals(settings: Settings) -> dict[str, list[dict[str, Any]]]:
    """Query all currently-active deals from the latest snapshots.

    A "deal" is one of:
      1. In-store clearance: clearance_value is not null in latest snapshot
      2. Online deal: item has a recent deal alert (PRICE_DROP, SPECIAL_BUY,
         CLEARANCE, DEEP_DISCOUNT) within the last 7 days — this ensures
         the diff engine validated the discount rather than trusting the
         permanent "Special Buys" savings_center tag.

    Returns a dict keyed by store_id, each value a list of deal dicts.
    """
    async with get_session(settings) as session:
        latest_sub = _latest_snapshots_subquery()
        first_price_sub = _first_price_subquery()

        # ── Prong 1: In-store clearance (from latest snapshots) ──────────
        clearance_result = await session.execute(
            select(
                StoreSnapshot,
                Product.title,
                Product.canonical_url,
                first_price_sub.c.first_price,
            )
            .join(
                latest_sub,
                and_(
                    StoreSnapshot.store_id == latest_sub.c.store_id,
                    StoreSnapshot.item_id == latest_sub.c.item_id,
                    StoreSnapshot.ts == latest_sub.c.max_ts,
                ),
            )
            .outerjoin(Product, StoreSnapshot.item_id == Product.item_id)
            .outerjoin(
                first_price_sub,
                and_(
                    StoreSnapshot.store_id == first_price_sub.c.store_id,
                    StoreSnapshot.item_id == first_price_sub.c.item_id,
                ),
            )
            .where(
                StoreSnapshot.clearance_value.isnot(None),
                # Freshness: items unseen by recent scans left the catalog
                StoreSnapshot.ts
                >= datetime.now(timezone.utc)
                - timedelta(hours=settings.deal_freshness_hours),
            )
        )
        # Same actionability rule as alerting: drop deals that are OOS locally
        # with a clearance price not purchasable online — and deals the user
        # dismissed as not real (they resurface only if the price gets deeper).
        from hd.pipeline.diff import _load_dismissals, clearance_purchasable
        dismissals = await _load_dismissals(settings)

        def _dismissed(snap) -> bool:
            key = (snap.store_id, snap.item_id)
            if key not in dismissals:
                return False
            dv = dismissals[key]
            return dv is None or snap.clearance_value is None or float(snap.clearance_value) >= dv

        clearance_rows = [
            row for row in clearance_result.all()
            if clearance_purchasable(row[0]) and not _dismissed(row[0])
        ]

        # ── Prong 2: Online deals (validated by recent alerts) ───────────
        # Get (store_id, item_id) pairs with deal alerts in the last 7 days
        alert_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        _online_types = {
            AlertType.PRICE_DROP,
            AlertType.SPECIAL_BUY,
            AlertType.CLEARANCE,
            AlertType.DEEP_DISCOUNT,
        }
        alerted_result = await session.execute(
            select(Alert.store_id, Alert.item_id)
            .where(Alert.alert_type.in_(_online_types), Alert.ts >= alert_cutoff)
            .distinct()
        )
        alerted_pairs = {(r.store_id, r.item_id) for r in alerted_result.all()}

        # Fetch snapshots for alerted items (exclude those already in clearance)
        clearance_keys = {(s.store_id, s.item_id) for s, _, _, _ in clearance_rows}
        online_pairs = alerted_pairs - clearance_keys

        online_rows = []
        if online_pairs:
            # Build snapshot query for alerted items
            online_result = await session.execute(
                select(
                    StoreSnapshot,
                    Product.title,
                    Product.canonical_url,
                    first_price_sub.c.first_price,
                )
                .join(
                    latest_sub,
                    and_(
                        StoreSnapshot.store_id == latest_sub.c.store_id,
                        StoreSnapshot.item_id == latest_sub.c.item_id,
                        StoreSnapshot.ts == latest_sub.c.max_ts,
                    ),
                )
                .outerjoin(Product, StoreSnapshot.item_id == Product.item_id)
                .outerjoin(
                    first_price_sub,
                    and_(
                        StoreSnapshot.store_id == first_price_sub.c.store_id,
                        StoreSnapshot.item_id == first_price_sub.c.item_id,
                    ),
                )
            )
            all_latest = online_result.all()
            online_rows = [
                row for row in all_latest
                if (row[0].store_id, row[0].item_id) in online_pairs
            ]

        # Deal age: earliest alert per (store_id, item_id) for deal types
        age_result = await session.execute(
            select(
                Alert.store_id,
                Alert.item_id,
                func.min(Alert.ts).label("first_alert_ts"),
            )
            .where(Alert.alert_type.in_(_DEAL_ALERT_TYPES))
            .group_by(Alert.store_id, Alert.item_id)
        )
        age_map: dict[tuple[str, str], datetime] = {
            (r.store_id, r.item_id): r.first_alert_ts for r in age_result.all()
        }

    deals_by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()  # (store_id, item_id) dedup

    # Process clearance rows
    for snap, title, canonical_url, first_price in clearance_rows:
        key = (snap.store_id, snap.item_id)
        if key in seen:
            continue
        deal = _build_deal_dict(
            snap, title, canonical_url, first_price, "in_store_clearance", age_map
        )
        if deal:
            seen.add(key)
            deals_by_store[snap.store_id].append(deal)

    # Process online rows
    for snap, title, canonical_url, first_price in online_rows:
        key = (snap.store_id, snap.item_id)
        if key in seen:
            continue
        deal = _build_deal_dict(
            snap, title, canonical_url, first_price, "online", age_map
        )
        if deal:
            seen.add(key)
            deals_by_store[snap.store_id].append(deal)

    return dict(deals_by_store)


def _build_deal_dict(
    snap: Any,
    title: str | None,
    canonical_url: str | None,
    first_price: Decimal | None,
    deal_type: str,
    age_map: dict[tuple[str, str], datetime],
) -> dict[str, Any] | None:
    """Build a deal dict from a snapshot row."""
    if deal_type == "in_store_clearance":
        effective_price = float(snap.clearance_value)
        effective_discount_pct = snap.clearance_percentage_off
    else:
        effective_price = float(snap.price_value) if snap.price_value is not None else None
        # Compute observed discount from first price baseline
        if (
            first_price is not None
            and snap.price_value is not None
            and first_price > 0
            and snap.price_value < first_price
        ):
            effective_discount_pct = round(
                float((first_price - snap.price_value) / first_price * 100)
            )
        else:
            effective_discount_pct = snap.percentage_off

    # Stock status
    stock_data = {
        "in_stock": snap.in_stock,
        "inventory_qty": snap.inventory_qty,
    }
    in_stock = infer_in_stock(stock_data)

    # Deal age
    age_key = (snap.store_id, snap.item_id)
    deal_age_ts = age_map.get(age_key) or snap.ts

    # Product URL
    if canonical_url:
        product_url = f"https://www.homedepot.com{canonical_url}"
    else:
        product_url = f"https://www.homedepot.com/s/{snap.item_id}"

    # Only include in-stock items on the canvas
    if not in_stock:
        return None

    return {
        "item_id": snap.item_id,
        "title": title or snap.item_id,
        "product_url": product_url,
        "price_value": float(snap.price_value) if snap.price_value is not None else None,
        "clearance_value": float(snap.clearance_value) if snap.clearance_value is not None else None,
        "savings_center": snap.savings_center,
        "deal_type": deal_type,
        "effective_price": effective_price,
        "effective_discount_pct": effective_discount_pct,
        "in_stock": in_stock,
        "inventory_qty": snap.inventory_qty,
        "deal_age_ts": deal_age_ts,
    }


# ── Markdown formatting ───────────────────────────────────────────────────────


def _sort_deals(deals: list[dict]) -> list[dict]:
    """Sort deals by discount % descending."""
    def key(d: dict) -> float:
        pct = d.get("effective_discount_pct") or 0
        return -pct
    return sorted(deals, key=key)


def _fmt_qty(qty: int | None) -> str:
    """Format inventory quantity with correct pluralization."""
    if qty is None:
        return "In Stock"
    return f"{qty} unit{'s' if qty != 1 else ''}"


def _format_deal_line(deal: dict) -> str:
    """Format a single deal as a two-line markdown bullet.

    Line 1: linked title — price (discount%)
    Line 2: supplementary details (online price, stock, deal age)
    """
    title = deal["title"]
    url = deal.get("product_url", "")
    price = fmt_price(deal.get("effective_price"))
    pct = deal.get("effective_discount_pct")
    pct_str = f"{pct}% off" if pct else ""

    # Line 1: title (as link) + price + discount
    if url:
        name = f"[{title}]({url})"
    else:
        name = f"**{title}**"

    if pct_str:
        line1 = f"- **{name}** — {price} ({pct_str})"
    else:
        line1 = f"- **{name}** — {price}"

    # Line 2: supplementary details
    details: list[str] = []

    if deal["deal_type"] == "in_store_clearance":
        online = fmt_price(deal.get("price_value"))
        details.append(f"Online: {online}")
    else:
        center = deal.get("savings_center") or ""
        if center:
            details.append(center.replace("_", " ").title())

    qty = deal.get("inventory_qty")
    details.append(_fmt_qty(qty))

    age = fmt_ts_relative(deal.get("deal_age_ts"))
    if age != "-":
        details.append(f"First seen {age}")

    line2 = "  " + " · ".join(details)

    return f"{line1}\n{line2}"


def format_canvas_markdown(
    deals_by_store: dict[str, list[dict]],
    store_names: dict[str, str],
    store_order: list[str] | None = None,
) -> str:
    """Build the full canvas markdown document."""
    now = datetime.now(timezone.utc)
    ts_str = now.strftime("%b %d, %Y at %I:%M %p UTC")

    lines: list[str] = [
        f"# {CANVAS_TITLE}",
        f"*Updated: {ts_str}*",
        "",
    ]

    stores = store_order or sorted(deals_by_store.keys())
    # Include stores with no deals too
    all_stores = sorted(set(stores) | set(store_names.keys()))

    for i, store_id in enumerate(all_stores):
        if i > 0:
            lines.append("---")
            lines.append("")

        name = store_names.get(store_id, "")
        header = f"## Store {store_id}" + (f" — {name}" if name else "")
        lines.append(header)
        lines.append("")

        deals = deals_by_store.get(store_id, [])

        # Split by deal type
        clearance = [d for d in deals if d["deal_type"] == "in_store_clearance"]
        online = [d for d in deals if d["deal_type"] == "online"]

        # In-Store Clearance section
        lines.append(f"### In-Store Clearance ({len(clearance)} items)")
        lines.append("")
        if clearance:
            for d in _sort_deals(clearance):
                lines.append(_format_deal_line(d))
            lines.append("")
        else:
            lines.append("> No in-store clearance deals at this store.")
            lines.append("")

        # Online Deals section
        lines.append(f"### Online Deals ({len(online)} items)")
        lines.append("")
        if online:
            for d in _sort_deals(online):
                lines.append(_format_deal_line(d))
            lines.append("")
        else:
            lines.append("> No online deals at this store.")
            lines.append("")

    markdown = "\n".join(lines)

    # Safety truncation
    if len(markdown) > MARKDOWN_MAX_CHARS:
        truncated = markdown[:MARKDOWN_MAX_CHARS]
        # Find last complete line
        last_newline = truncated.rfind("\n")
        if last_newline > 0:
            truncated = truncated[:last_newline]
        total_deals = sum(len(d) for d in deals_by_store.values())
        truncated += f"\n\n*...truncated ({total_deals} total deals)*"
        return truncated

    return markdown


# ── Slack Canvas API ──────────────────────────────────────────────────────────


async def _slack_api_call(
    token: str, url: str, payload: dict
) -> tuple[bool, dict]:
    """POST to a Slack API endpoint via curl. Returns (success, response_data)."""
    body = json.dumps(payload)
    cmd_parts = [
        "curl", "-s", "-w", "\n%{http_code}",
        "-X", "POST",
        "-H", "Content-Type: application/json; charset=utf-8",
        "-H", f"Authorization: Bearer {token}",
        "-d", body,
        "--max-time", "15",
        url,
    ]
    cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        output = stdout.decode().strip()
        parts = output.rsplit("\n", 1)
        response_body = parts[0] if len(parts) > 1 else ""
        status_code = parts[-1]

        if not status_code.startswith("2"):
            log.warning("Slack API HTTP error", url=url, status=status_code)
            return False, {}

        try:
            data = json.loads(response_body)
        except json.JSONDecodeError:
            log.warning("Slack API response not JSON", body=response_body[:200])
            return False, {}

        if data.get("ok"):
            return True, data
        else:
            log.warning("Slack API error", url=url, error=data.get("error", "unknown"))
            return False, data

    except asyncio.TimeoutError:
        log.warning("Slack API call timed out", url=url)
        return False, {}
    except Exception as exc:
        log.warning("Slack API call failed", url=url, error=str(exc))
        return False, {}


async def create_canvas(settings: Settings, title: str, markdown: str) -> str | None:
    """Create a new Slack canvas. Returns canvas_id on success, None on failure."""
    if not settings.slack_bot_token:
        log.warning("SLACK_BOT_TOKEN not configured")
        return None

    payload: dict[str, Any] = {
        "title": title,
        "document_content": {"type": "markdown", "markdown": markdown},
    }
    if settings.slack_channel_id:
        payload["channel_id"] = settings.slack_channel_id

    ok, data = await _slack_api_call(settings.slack_bot_token, SLACK_CANVAS_CREATE_URL, payload)
    if ok:
        canvas_id = data.get("canvas_id")
        log.info("Canvas created", canvas_id=canvas_id)
        return canvas_id
    return None


async def update_canvas(settings: Settings, canvas_id: str, markdown: str) -> bool:
    """Update an existing Slack canvas. Returns True on success."""
    if not settings.slack_bot_token:
        log.warning("SLACK_BOT_TOKEN not configured")
        return False

    payload = {
        "canvas_id": canvas_id,
        "changes": [{
            "operation": "replace",
            "document_content": {"type": "markdown", "markdown": markdown},
        }],
    }

    ok, data = await _slack_api_call(settings.slack_bot_token, SLACK_CANVAS_EDIT_URL, payload)
    if not ok and data.get("error") == "canvas_not_found":
        log.warning("Canvas not found, will re-create", canvas_id=canvas_id)
    return ok


# ── Orchestrator ──────────────────────────────────────────────────────────────


async def run_canvas_update(
    settings: Settings, dry_run: bool = False
) -> tuple[str, int]:
    """Query deals, format canvas, create or update in Slack.

    Returns (markdown, deal_count). Returns ("", 0) when the canvas is
    disabled, so callers need no special case.
    """
    if not settings.canvas_enabled:
        log.info("Canvas disabled — skipping")
        return "", 0

    deals_by_store = await get_active_deals(settings)
    deal_count = sum(len(d) for d in deals_by_store.values())

    # Load store names
    async with get_session(settings) as session:
        result = await session.execute(select(Store))
        store_names = {s.store_id: s.name for s in result.scalars().all() if s.name}

    markdown = format_canvas_markdown(deals_by_store, store_names, settings.store_list)

    if dry_run:
        return markdown, deal_count

    # Read canvas ID from file
    canvas_path = Path(settings.canvas_id_path)
    canvas_id: str | None = None
    if canvas_path.exists():
        try:
            canvas_id = canvas_path.read_text().strip() or None
        except OSError:
            pass

    # Try update, fall back to create
    if canvas_id:
        success = await update_canvas(settings, canvas_id, markdown)
        if success:
            log.info("Canvas updated", canvas_id=canvas_id, deals=deal_count)
            return markdown, deal_count
        # Update failed (canvas deleted?) — fall through to create
        canvas_id = None

    # Create new canvas
    canvas_id = await create_canvas(settings, CANVAS_TITLE, markdown)
    if canvas_id:
        try:
            canvas_path.write_text(canvas_id)
        except OSError as e:
            log.warning("Failed to persist canvas_id", error=str(e))
        log.info("Canvas created and persisted", canvas_id=canvas_id, deals=deal_count)
    else:
        log.warning("Canvas creation failed")

    return markdown, deal_count
