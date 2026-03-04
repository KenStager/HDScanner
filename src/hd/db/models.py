"""SQLAlchemy ORM models."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
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
    BACK_IN_STOCK = "BACK_IN_STOCK"
    OOS = "OOS"
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
    first_seen_ts: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    last_seen_ts: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Store(Base):
    __tablename__ = "stores"

    store_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(10), nullable=True)


class StoreSnapshot(Base):
    __tablename__ = "store_snapshots"
    __table_args__ = (
        Index("ix_snapshot_store_item_ts", "store_id", "item_id", "ts"),
        Index("ix_snapshot_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
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
    inventory_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    limited_qty: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    out_of_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    store_id: Mapped[str] = mapped_column(String(10), nullable=False)
    item_id: Mapped[str] = mapped_column(String(20), nullable=False)
    alert_type: Mapped[AlertType] = mapped_column(Enum(AlertType), nullable=False)
    severity: Mapped[Severity] = mapped_column(Enum(Severity), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
