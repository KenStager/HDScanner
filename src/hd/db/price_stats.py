"""Maintenance of item_price_stats, the price history that survives pruning.

One pure fold (`apply_observation`) is the single definition of how an
observation changes an item's running facts. Both callers use it — the write
path as snapshots land, and the backfill that reconstructs the table from raw
history — so the two can never drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Sequence

import structlog
from sqlalchemy import delete, select

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import ItemPriceStat, StoreSnapshot

log = structlog.get_logger(__name__)

# SQLite caps bound parameters per statement; chunk IN () lookups below it
_IN_CHUNK = 500


def _naive_utc(ts: datetime) -> datetime:
    """Normalize to naive UTC so stored and in-flight timestamps compare."""
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def apply_observation(
    stat: ItemPriceStat, price: Decimal | None, ts: datetime
) -> None:
    """Fold one priced observation into an item's running stats.

    Assumes observations arrive in time order, which both callers guarantee —
    the write path appends at `now`, the backfill streams ordered by ts. Out of
    order input can only overcount obs_days; the price extremes stay correct.
    """
    if price is None:
        return
    ts = _naive_utc(ts)

    # A fresh ORM object has no column defaults applied until flush
    if stat.obs_count is None:
        stat.obs_count = 0
    if stat.obs_days is None:
        stat.obs_days = 0
    if stat.price_sum is None:
        stat.price_sum = Decimal("0")

    if stat.low_price is None or price < stat.low_price:
        stat.low_price, stat.low_ts = price, ts
    if stat.high_price is None or price > stat.high_price:
        stat.high_price, stat.high_ts = price, ts

    stat.price_sum = stat.price_sum + price
    stat.obs_count += 1

    # Count the day before last_ts moves — six scans in one afternoon are one day
    if stat.last_ts is None or ts.date() != _naive_utc(stat.last_ts).date():
        stat.obs_days += 1

    if stat.first_ts is None or ts < _naive_utc(stat.first_ts):
        stat.first_ts = ts
    if stat.last_ts is None or ts > _naive_utc(stat.last_ts):
        stat.last_ts = ts


def mean_price(stat: ItemPriceStat) -> float | None:
    """The average price behind this item, or None if nothing was observed."""
    if not stat.obs_count or stat.price_sum is None:
        return None
    return float(stat.price_sum) / stat.obs_count


async def record_observations(
    session, store_id: str, observations: Sequence[tuple[str, Decimal | None, datetime]]
) -> int:
    """Fold a batch of (item_id, price, ts) into the aggregate, in-session.

    Runs inside the caller's transaction so the aggregate commits with the
    snapshots that produced it — a half-applied batch would leave the stats
    permanently understating history no later pass could detect.
    """
    priced = [(i, p, t) for i, p, t in observations if p is not None]
    if not priced:
        return 0

    item_ids = sorted({i for i, _, _ in priced})
    by_id: dict[str, ItemPriceStat] = {}
    for start in range(0, len(item_ids), _IN_CHUNK):
        chunk = item_ids[start:start + _IN_CHUNK]
        rows = await session.execute(
            select(ItemPriceStat).where(
                ItemPriceStat.store_id == store_id,
                ItemPriceStat.item_id.in_(chunk),
            )
        )
        for stat in rows.scalars():
            by_id[stat.item_id] = stat

    for item_id, price, ts in priced:
        stat = by_id.get(item_id)
        if stat is None:
            stat = ItemPriceStat(
                store_id=store_id, item_id=item_id,
                price_sum=Decimal("0"), obs_count=0, obs_days=0,
            )
            session.add(stat)
            by_id[item_id] = stat
        apply_observation(stat, price, ts)

    return len(priced)


async def backfill(settings: Settings, chunk_size: int = 50_000) -> tuple[int, int]:
    """Rebuild the whole aggregate from surviving raw snapshots.

    Safe to run only while raw history exists — it is a reconstruction, not a
    merge, so it replaces the table wholesale. Reads just the four columns it
    needs, leaving the ~1 GB of raw_json untouched on disk.
    """
    folded: dict[tuple[str, str], ItemPriceStat] = {}
    scanned = 0
    last_id = 0

    async with get_session(settings) as session:
        while True:
            rows = (await session.execute(
                select(
                    StoreSnapshot.id,
                    StoreSnapshot.store_id,
                    StoreSnapshot.item_id,
                    StoreSnapshot.price_value,
                    StoreSnapshot.ts,
                )
                .where(StoreSnapshot.id > last_id, StoreSnapshot.price_value.isnot(None))
                .order_by(StoreSnapshot.id)
                .limit(chunk_size)
            )).all()
            if not rows:
                break
            for row_id, store_id, item_id, price, ts in rows:
                key = (store_id, item_id)
                stat = folded.get(key)
                if stat is None:
                    stat = ItemPriceStat(
                        store_id=store_id, item_id=item_id,
                        price_sum=Decimal("0"), obs_count=0, obs_days=0,
                    )
                    folded[key] = stat
                apply_observation(stat, price, ts)
                last_id = row_id
            scanned += len(rows)
            log.info("Backfill progress", scanned=scanned, items=len(folded))

    async with get_session(settings) as session:
        await session.execute(delete(ItemPriceStat))
        for stat in folded.values():
            session.add(stat)

    log.info("Backfill complete", scanned=scanned, items=len(folded))
    return scanned, len(folded)
