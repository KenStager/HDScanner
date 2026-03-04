"""Internal dataclasses for normalized API data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class NormalizedProduct:
    item_id: str
    brand: str | None = None
    title: str | None = None
    canonical_url: str | None = None
    model_number: str | None = None


@dataclass
class NormalizedSnapshot:
    item_id: str
    store_id: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    price_value: float | None = None
    price_original: float | None = None
    promotion_type: str | None = None
    promotion_tag: str | None = None
    savings_center: str | None = None
    dollar_off: float | None = None
    percentage_off: int | None = None
    special_buy: bool | None = None
    inventory_qty: int | None = None
    in_stock: bool | None = None
    limited_qty: bool | None = None
    out_of_stock: bool | None = None
    raw: dict = field(default_factory=dict)
