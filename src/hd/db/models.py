"""SQLAlchemy ORM models."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,  # always declared timezone=True: every timestamp here is UTC-aware
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class AlertType(str, enum.Enum):
    PRICE_DROP = "PRICE_DROP"
    CLEARANCE = "CLEARANCE"
    SPECIAL_BUY = "SPECIAL_BUY"
    DEEP_DISCOUNT = "DEEP_DISCOUNT"
    PRICING_ERROR = "PRICING_ERROR"
    BACK_IN_STOCK = "BACK_IN_STOCK"
    OOS = "OOS"
    IN_STORE_CLEARANCE = "IN_STORE_CLEARANCE"
    HEALTH_DEGRADED = "HEALTH_DEGRADED"
    HEALTH_RECOVERED = "HEALTH_RECOVERED"


class Severity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Product(Base):
    __tablename__ = "products"

    item_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    brand: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # The manufacturer's UPC, as the API reports it. Kept beside model_number
    # because the two answer different questions: model_number is how a human
    # or a specialist retailer names the thing, upc is how a machine joins it
    # to a catalogue that has never heard of Home Depot. Nullable on purpose —
    # a store-composed bundle need not have one.
    upc: Mapped[str | None] = mapped_column(String(20), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_seen_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Store(Base):
    __tablename__ = "stores"

    store_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Home Depot store pages live at /l/<name>/<state>/<city>/<zip>/<store_id>.
    # City is usually the store name but not always ("N. Cambridge" in Cambridge),
    # so it is stored rather than assumed.
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)


class StoreSnapshot(Base):
    __tablename__ = "store_snapshots"
    __table_args__ = (
        Index("ix_snapshot_store_item_ts", "store_id", "item_id", "ts"),
        Index("ix_snapshot_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    store_id: Mapped[str] = mapped_column(String(10), nullable=False)
    item_id: Mapped[str] = mapped_column(String(20), nullable=False)
    price_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_original: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    promotion_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    promotion_tag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    savings_center: Mapped[str | None] = mapped_column(String(50), nullable=True)
    dollar_off: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    percentage_off: Mapped[int | None] = mapped_column(Integer, nullable=True)
    special_buy: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    clearance_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    clearance_dollar_off: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    clearance_percentage_off: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inventory_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    limited_qty: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    out_of_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DailyDealPick(Base):
    """One brand-matched item from a day's Daily Deals set.

    The sweep discovers the set from HD's daily-deals page; only the items
    matching a tracked brand are recorded (unmatched items were never priced,
    so there is nothing honest to show for them). end_date is HD's own label
    for the set — the day the deals expire — and doubles as the currency
    check: a set whose end_date has passed is dead.
    """

    __tablename__ = "daily_deal_picks"

    end_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    store_id: Mapped[str] = mapped_column(String(10), nullable=False)
    item_id: Mapped[str] = mapped_column(String(20), nullable=False)
    alert_type: Mapped[AlertType] = mapped_column(Enum(AlertType), nullable=False)
    severity: Mapped[Severity] = mapped_column(Enum(Severity), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ItemPriceStat(Base):
    """Durable per-(store, item) price facts that outlive snapshot pruning.

    store_snapshots is the raw record, and `hd prune` deletes everything past
    snapshot_retention_days — today that is 94% of the table, including every
    row for items last seen before the cutoff. The verdicts on the deal board
    rest on that history, so the parts a verdict needs are folded in here as
    each snapshot lands: the lowest price ever witnessed and when, the running
    sum and count behind the average, and how many distinct days back it.

    Maintained incrementally and never rebuilt from store_snapshots in normal
    operation — the rows it was derived from may already be gone. `hd
    backfill-stats` reconstructs it only while the raw history still exists.
    """

    __tablename__ = "item_price_stats"

    store_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(20), primary_key=True)

    low_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    low_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    high_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    high_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # mean = price_sum / obs_count, kept as running totals so the average
    # survives the deletion of the observations that produced it
    price_sum: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    obs_count: Mapped[int] = mapped_column(Integer, default=0)
    # distinct calendar days, the sample measure worth gating a verdict on —
    # six scans in one afternoon are not six days of evidence
    obs_days: Mapped[int] = mapped_column(Integer, default=0)

    first_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_item_price_stats_item", "item_id"),
    )


class DismissedDeal(Base):
    """A deal the user marked as not real (phantom clearance, bad data).

    One row per (store_id, item_id); item_id-scope uses store_id "online" for
    the online tab. dismissed_value records the deal price at dismissal time —
    the deal stays hidden while the current price is at or above it, and
    resurfaces automatically if the deal later gets deeper.
    """

    __tablename__ = "dismissed_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    store_id: Mapped[str] = mapped_column(String(10), nullable=False)
    item_id: Mapped[str] = mapped_column(String(20), nullable=False)
    dismissed_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        Index("ix_dismissed_store_item", "store_id", "item_id", unique=True),
    )


class ScanRun(Base):
    """One browse run's outcome — the frame its walk coverage hangs from.

    The most dangerous failure a scanner has is covering less than it meant
    to and not saying so: downstream, an item missing from a short scan reads
    as a dead deal, an ended clearance, a price that "disappeared". The facts
    that guard against that — what a run attempted, how it ended — lived only
    in log lines, and logs rotate. This table is the durable version: one row
    per browse run, status "complete" or "aborted", finalized in the same
    place the run summary is logged.
    """

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tiers: Mapped[str] = mapped_column(String(50), nullable=False)  # "shelf,network"
    # running → complete | aborted. A row stuck at "running" is a crashed run.
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="running")
    walks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requests_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_scan_runs_started", "started"),
    )


class WalkCoverage(Base):
    """What one walk promised and what it delivered, per run.

    status is judged conservatively: "complete" only when every itemId the
    node claimed was actually seen ("failed" when not even page 0 was usable,
    "truncated" for everything between — planner truncation, a mid-walk error
    or throttle, or a shortfall against the node's own live total). Catalog
    churn mid-walk can therefore mark an honest walk truncated; that errs
    toward under-claiming coverage, which is the safe direction — nothing may
    ever reason from an item's ABSENCE except against a walk recorded
    complete. A walk deferred by shelf rotation writes no row at all: not
    attempted is not evidence either.
    """

    __tablename__ = "walk_coverage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    store_id: Mapped[str] = mapped_column(String(10), nullable=False)
    tier: Mapped[str] = mapped_column(String(10), nullable=False)  # IN_STORE | ALL
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    started: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    # The denominator the status was judged against: the node's live page-0
    # total when the walk read one, its planned total otherwise.
    items_expected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Distinct itemIds the API returned across the walk's pages, before any
    # brand filter — what the walk SAW, not what it chose to keep.
    items_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_walk_coverage_run", "run_id"),
    )
