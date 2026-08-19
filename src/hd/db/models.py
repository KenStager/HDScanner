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
