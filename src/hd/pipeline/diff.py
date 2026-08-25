"""Diff engine — compares consecutive snapshots and generates alerts."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from decimal import Decimal

from sqlalchemy import select, desc, func

from hd.config import Settings
from hd.db.base import get_session
from hd.db.models import StoreSnapshot, Alert, AlertType, Severity, Product
from hd.logging import get_logger

log = get_logger("pipeline.diff")


def _product_url(product: Product | None, item_id: str) -> str:
    """Build a Home Depot product URL, falling back to search when canonical_url is missing."""
    if product and product.canonical_url:
        return f"https://www.homedepot.com{product.canonical_url}"
    return f"https://www.homedepot.com/s/{item_id}"


def _is_combo_kit(product: Product | None) -> bool:
    """Combo kits have inflated price_original (sum of individual tools)."""
    if not product:
        return False
    model = product.model_number or ""
    # Milwaukee convention: -20/-21 = single tool, -22+ = multi-tool kit
    suffix_match = re.search(r"-(\d{2})(?:\b|$)", model)
    if suffix_match and int(suffix_match.group(1)) >= 22:
        return True
    title = (product.title or "").lower()
    return "combo kit" in title or "-tool)" in title


def _reference_price(
    baseline_price: Decimal | None,
    curr: StoreSnapshot,
    prev: StoreSnapshot | None,
    product: Product | None,
) -> Decimal | None:
    """Compute the best reference price for discount calculations.

    Uses 30-day max baseline as primary. Falls back to price_original for
    non-combo items when baseline is missing and price_original is stable.
    """
    ref = baseline_price

    # Supplement with price_original for non-combos when baseline is weak
    if not _is_combo_kit(product) and curr.price_original is not None and curr.price_original > 0:
        orig_stable = (
            prev is not None
            and prev.price_original is not None
            and prev.price_original == curr.price_original
        )
        if ref is None and (orig_stable or prev is None):
            ref = curr.price_original
        elif ref is not None and orig_stable:
            # Use whichever is higher — conservative reference
            ref = max(ref, curr.price_original)

    return ref


def clearance_purchasable(snap: StoreSnapshot) -> bool:
    """Whether an in-store clearance deal can actually be had.

    True when the item is on the local shelf, or when the online price is at
    (or below) the clearance price so it can be bought without shelf stock.
    Items OOS locally whose clearance price exists only in-store (e.g.
    ship-to-store listings still at full online price) are not actionable —
    alerting on them is noise.
    """
    if snap.in_stock or (snap.inventory_qty or 0) > 0:
        return True
    if snap.price_value is not None and snap.clearance_value is not None:
        return float(snap.price_value) <= float(snap.clearance_value)
    return False


async def _load_dismissals(settings: Settings) -> dict[tuple[str, str], float | None]:
    """Map of (store_id, item_id) -> dismissed price for user-dismissed deals."""
    from hd.db.models import DismissedDeal

    async with get_session(settings) as session:
        rows = (await session.execute(select(DismissedDeal))).scalars().all()
    return {
        (d.store_id, d.item_id): float(d.dismissed_value) if d.dismissed_value is not None else None
        for d in rows
    }


def _drop_dismissed(
    alerts: list[Alert],
    dismissals: dict[tuple[str, str], float | None],
) -> list[Alert]:
    """Drop IN_STORE_CLEARANCE alerts the user has marked as not real.

    A dismissal keeps suppressing while the clearance price stays at or above
    the price it was dismissed at; a deeper price is a new deal and alerts.
    """
    if not dismissals:
        return alerts
    kept = []
    for a in alerts:
        if a.alert_type == AlertType.IN_STORE_CLEARANCE:
            key = (a.store_id, a.item_id)
            if key in dismissals:
                dismissed_value = dismissals[key]
                current = (a.payload or {}).get("clearance_value")
                if dismissed_value is None or current is None or current >= dismissed_value:
                    continue
        kept.append(a)
    return kept


async def run_catch_up(settings: Settings) -> list[Alert]:
    """One-time state-based scan: alert on anything currently ≥50% off or in
    Special Buys that has never had an alert of that type.

    Unlike run_diff (which is transition-based), this looks at the latest
    snapshot state and checks the alerts table for prior history.
    """
    alerts: list[Alert] = []
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=settings.baseline_window_days)

    async with get_session(settings) as session:
        # Get the latest snapshot per (store_id, item_id)
        latest_ts_sub = (
            select(
                StoreSnapshot.store_id,
                StoreSnapshot.item_id,
                func.max(StoreSnapshot.ts).label("max_ts"),
            )
            .group_by(StoreSnapshot.store_id, StoreSnapshot.item_id)
            .subquery()
        )

        result = await session.execute(
            select(StoreSnapshot)
            .join(
                latest_ts_sub,
                (StoreSnapshot.store_id == latest_ts_sub.c.store_id)
                & (StoreSnapshot.item_id == latest_ts_sub.c.item_id)
                & (StoreSnapshot.ts == latest_ts_sub.c.max_ts),
            )
        )
        latest_snapshots = result.scalars().all()

        # Load all existing alerts to check "never alerted" condition
        existing_result = await session.execute(
            select(Alert.store_id, Alert.item_id, Alert.alert_type)
        )
        existing_alerts = {
            (r.store_id, r.item_id, r.alert_type) for r in existing_result.all()
        }

        # Load products for payload
        prod_result = await session.execute(select(Product))
        product_map = {p.item_id: p for p in prod_result.scalars().all()}

        # Load baseline prices: 30-day max price per (store_id, item_id)
        baseline_sub = (
            select(
                StoreSnapshot.store_id,
                StoreSnapshot.item_id,
                func.max(StoreSnapshot.price_value).label("max_price"),
            )
            .where(
                StoreSnapshot.price_value.isnot(None),
                StoreSnapshot.ts >= cutoff_30d,
            )
            .group_by(StoreSnapshot.store_id, StoreSnapshot.item_id)
            .subquery()
        )
        baseline_result = await session.execute(
            select(
                baseline_sub.c.store_id,
                baseline_sub.c.item_id,
                baseline_sub.c.max_price,
            )
        )
        baseline_map: dict[tuple[str, str], Decimal] = {
            (r.store_id, r.item_id): r.max_price
            for r in baseline_result.all()
            if r.max_price is not None
        }

        now = datetime.now(timezone.utc)

        for snap in latest_snapshots:
            product = product_map.get(snap.item_id)
            baseline_price = baseline_map.get((snap.store_id, snap.item_id))
            ref = _reference_price(baseline_price, snap, None, product)

            # Compute observed discount from our own price history
            observed_pct_off: float | None = None
            if ref and snap.price_value is not None and ref > 0:
                if snap.price_value < ref:
                    observed_pct_off = float(
                        (ref - snap.price_value) / ref * 100
                    )

            # Build a minimal payload (no "before" since this is state-based)
            payload = {
                "after": _snapshot_to_dict(snap),
                "product_title": product.title if product else None,
                "product_url": _product_url(product, snap.item_id),
                "image_url": product.image_url if product else None,
                "baseline_price": float(baseline_price) if baseline_price else None,
                "reference_price": float(ref) if ref else None,
                "catch_up": True,
            }

            # DEEP_DISCOUNT — use observed drop ≥50%, fall back to HD's
            # percentage_off only when we have no baseline
            if ref is not None:
                if observed_pct_off is not None and observed_pct_off >= 50:
                    key = (snap.store_id, snap.item_id, AlertType.DEEP_DISCOUNT)
                    if key not in existing_alerts:
                        alerts.append(Alert(
                            ts=now,
                            store_id=snap.store_id,
                            item_id=snap.item_id,
                            alert_type=AlertType.DEEP_DISCOUNT,
                            severity=Severity.HIGH,
                            payload={
                                **payload,
                                "percentage_off": round(observed_pct_off),
                                "observed_pct_off": round(observed_pct_off, 1),
                            },
                        ))
                        existing_alerts.add(key)
            else:
                curr_pct = snap.percentage_off or 0
                if curr_pct >= 50:
                    key = (snap.store_id, snap.item_id, AlertType.DEEP_DISCOUNT)
                    if key not in existing_alerts:
                        alerts.append(Alert(
                            ts=now,
                            store_id=snap.store_id,
                            item_id=snap.item_id,
                            alert_type=AlertType.DEEP_DISCOUNT,
                            severity=Severity.HIGH,
                            payload={**payload, "percentage_off": curr_pct},
                        ))
                        existing_alerts.add(key)

            # PRICING_ERROR — extreme discount without promo metadata
            if (
                ref is not None
                and snap.price_value is not None
                and ref > 0
            ):
                pct_off_ref = float((ref - snap.price_value) / ref * 100)
                no_promo = not snap.savings_center and not snap.promotion_tag
                if pct_off_ref >= settings.pricing_error_threshold_pct and no_promo:
                    key = (snap.store_id, snap.item_id, AlertType.PRICING_ERROR)
                    if key not in existing_alerts:
                        alerts.append(Alert(
                            ts=now,
                            store_id=snap.store_id,
                            item_id=snap.item_id,
                            alert_type=AlertType.PRICING_ERROR,
                            severity=Severity.HIGH,
                            payload={
                                **payload,
                                "reference_price": float(ref),
                                "pct_off_reference": round(pct_off_ref, 1),
                                "detection_reason": "extreme_discount_no_promo",
                            },
                        ))
                        existing_alerts.add(key)

            # IN_STORE_CLEARANCE — catch up on items with clearance pricing.
            # Only when the deal is actually obtainable (on shelf, or priced
            # online at the clearance value) — see clearance_purchasable.
            if snap.clearance_value is not None and clearance_purchasable(snap):
                key = (snap.store_id, snap.item_id, AlertType.IN_STORE_CLEARANCE)
                if key not in existing_alerts:
                    alerts.append(Alert(
                        ts=now,
                        store_id=snap.store_id,
                        item_id=snap.item_id,
                        alert_type=AlertType.IN_STORE_CLEARANCE,
                        severity=Severity.HIGH,
                        payload={
                            **payload,
                            "clearance_value": float(snap.clearance_value),
                            "clearance_percentage_off": snap.clearance_percentage_off,
                            "online_price": float(snap.price_value) if snap.price_value is not None else None,
                            "catch_up": True,
                        },
                    ))
                    existing_alerts.add(key)

    alerts = _drop_dismissed(alerts, await _load_dismissals(settings))
    log.info("Catch-up complete", snapshots=len(latest_snapshots), alerts=len(alerts))
    return alerts


async def run_diff(settings: Settings) -> list[Alert]:
    """Compare latest vs previous snapshots for all (store, item) pairs.

    Returns a list of Alert ORM objects (not yet persisted).
    """
    alerts: list[Alert] = []
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=settings.baseline_window_days)

    async with get_session(settings) as session:
        # Get all distinct (store_id, item_id) pairs
        pairs_result = await session.execute(
            select(StoreSnapshot.store_id, StoreSnapshot.item_id)
            .distinct()
        )
        pairs = pairs_result.all()

        # Pre-load all products for O(1) lookup (M6 fix)
        prod_result = await session.execute(select(Product))
        product_map = {p.item_id: p for p in prod_result.scalars().all()}

        # Pre-load baseline prices: 30-day max price per (store_id, item_id)
        baseline_sub = (
            select(
                StoreSnapshot.store_id,
                StoreSnapshot.item_id,
                func.max(StoreSnapshot.price_value).label("max_price"),
            )
            .where(
                StoreSnapshot.price_value.isnot(None),
                StoreSnapshot.ts >= cutoff_30d,
            )
            .group_by(StoreSnapshot.store_id, StoreSnapshot.item_id)
            .subquery()
        )
        baseline_result = await session.execute(
            select(
                baseline_sub.c.store_id,
                baseline_sub.c.item_id,
                baseline_sub.c.max_price,
            )
        )
        baseline_map: dict[tuple[str, str], Decimal] = {
            (r.store_id, r.item_id): r.max_price
            for r in baseline_result.all()
            if r.max_price is not None
        }

        now = datetime.now(timezone.utc)

        for store_id, item_id in pairs:

            # Fetch recent snapshots and deduplicate by timestamp
            # (items can appear on multiple pages, creating duplicates per run)
            result = await session.execute(
                select(StoreSnapshot)
                .where(
                    StoreSnapshot.store_id == store_id,
                    StoreSnapshot.item_id == item_id,
                )
                .order_by(desc(StoreSnapshot.ts))
                .limit(8)
            )
            raw_snapshots = result.scalars().all()

            # Deduplicate: keep first snapshot per distinct timestamp
            seen_ts: set[datetime] = set()
            snapshots: list[StoreSnapshot] = []
            for s in raw_snapshots:
                if s.ts not in seen_ts:
                    seen_ts.add(s.ts)
                    snapshots.append(s)
                if len(snapshots) == 2:
                    break

            product = product_map.get(item_id)

            if len(snapshots) < 2:
                # First snapshot — check for cold-start clearance
                if snapshots:
                    curr = snapshots[0]
                    cold_alerts = _cold_start_check(curr, product, settings)
                    alerts.extend(cold_alerts)
                continue

            curr = snapshots[0]
            prev = snapshots[1]

            gap_hours = (curr.ts - prev.ts).total_seconds() / 3600

            # Stale gap: skip entirely
            if gap_hours > settings.diff_stale_gap_hours:
                log.warning(
                    "Stale gap, skipping diff",
                    store_id=store_id,
                    item_id=item_id,
                    gap_hours=round(gap_hours, 1),
                )
                continue

            baseline_price = baseline_map.get((store_id, item_id))
            pair_alerts = _diff_snapshots(prev, curr, product, baseline_price, settings)

            # Moderate gap: annotate alerts
            if gap_hours > settings.diff_gap_threshold_hours:
                for alert in pair_alerts:
                    alert.payload = {
                        **(alert.payload or {}),
                        "gap_warning": True,
                        "gap_hours": round(gap_hours, 1),
                    }

            alerts.extend(pair_alerts)

    alerts = _drop_dismissed(alerts, await _load_dismissals(settings))
    log.info("Diff complete", pairs=len(pairs), alerts=len(alerts))
    return alerts


def _cold_start_check(
    curr: StoreSnapshot,
    product: Product | None,
    settings: Settings,
) -> list[Alert]:
    """Check first-snapshot items for cold-start clearance."""
    alerts: list[Alert] = []
    now = datetime.now(timezone.utc)

    if (
        curr.price_original is not None
        and curr.price_value is not None
        and curr.price_original > 0
        and not _is_combo_kit(product)
    ):
        pct_off_orig = float(
            (curr.price_original - curr.price_value) / curr.price_original * 100
        )
        if pct_off_orig >= settings.cold_start_clearance_pct and curr.savings_center:
            payload = {
                "after": _snapshot_to_dict(curr),
                "product_title": product.title if product else None,
                "product_url": _product_url(product, curr.item_id),
                "image_url": product.image_url if product else None,
                "cold_start": True,
                "pct_off_original": round(pct_off_orig, 1),
            }
            severity = Severity.HIGH if pct_off_orig >= 50 else Severity.MEDIUM
            alert_type = AlertType.DEEP_DISCOUNT if pct_off_orig >= 50 else AlertType.CLEARANCE
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=alert_type,
                severity=severity,
                payload=payload,
            ))

        # Cold-start pricing error: extreme discount with no promo
        no_promo = not curr.savings_center and not curr.promotion_tag
        if pct_off_orig >= settings.pricing_error_threshold_pct and no_promo:
            payload = {
                "after": _snapshot_to_dict(curr),
                "product_title": product.title if product else None,
                "product_url": _product_url(product, curr.item_id),
                "image_url": product.image_url if product else None,
                "cold_start": True,
                "reference_price": float(curr.price_original),
                "pct_off_reference": round(pct_off_orig, 1),
                "detection_reason": "extreme_discount_no_promo",
            }
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.PRICING_ERROR,
                severity=Severity.HIGH,
                payload=payload,
            ))

    # Cold-start in-store clearance — only when the deal is obtainable
    if curr.clearance_value is not None and clearance_purchasable(curr):
        alerts.append(Alert(
            ts=now,
            store_id=curr.store_id,
            item_id=curr.item_id,
            alert_type=AlertType.IN_STORE_CLEARANCE,
            severity=Severity.HIGH,
            payload={
                "after": _snapshot_to_dict(curr),
                "product_title": product.title if product else None,
                "product_url": _product_url(product, curr.item_id),
                "image_url": product.image_url if product else None,
                "cold_start": True,
                "clearance_value": float(curr.clearance_value),
                "clearance_percentage_off": curr.clearance_percentage_off,
                "online_price": float(curr.price_value) if curr.price_value is not None else None,
            },
        ))

    return alerts


def _diff_snapshots(
    prev: StoreSnapshot,
    curr: StoreSnapshot,
    product: Product | None,
    baseline_price: Decimal | None = None,
    settings: Settings | None = None,
) -> list[Alert]:
    """Apply diff rules between two snapshots. Return Alert objects.

    baseline_price is the 30-day max observed price for this (store, item) pair,
    used to validate that discounts are real observed drops — not just
    structural offsets (e.g. combo kits whose percentage_off reflects
    "sum of individual tools" rather than an actual markdown).
    """
    if settings is None:
        settings = Settings()

    alerts: list[Alert] = []
    now = datetime.now(timezone.utc)
    base_payload = _build_base_payload(prev, curr, product)

    # Compute reference price (30-day max + price_original for non-combos)
    ref = _reference_price(baseline_price, curr, prev, product)

    # Compute observed discount from reference price
    observed_pct_off: float | None = None
    if ref and curr.price_value is not None and ref > 0:
        if curr.price_value < ref:
            observed_pct_off = float(
                (ref - curr.price_value) / ref * 100
            )

    # PRICE_DROP — snapshot-to-snapshot >25% OR cumulative >35% from baseline
    if (
        curr.price_value is not None
        and prev.price_value is not None
        and curr.price_value < prev.price_value
    ):
        pct_drop = float((prev.price_value - curr.price_value) / prev.price_value * 100)
        if pct_drop > 25:
            severity = Severity.HIGH if pct_drop > 50 else Severity.MEDIUM
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.PRICE_DROP,
                severity=severity,
                payload={
                    **base_payload,
                    "pct_drop": round(pct_drop, 1),
                    "baseline_price": float(baseline_price) if baseline_price else None,
                    "observed_pct_off": round(observed_pct_off, 1) if observed_pct_off else None,
                },
            ))
        elif observed_pct_off is not None and observed_pct_off >= 35:
            # Cumulative step-down: single step was small but total from baseline is large
            severity = Severity.HIGH if observed_pct_off >= 50 else Severity.MEDIUM
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.PRICE_DROP,
                severity=severity,
                payload={
                    **base_payload,
                    "pct_drop": round(pct_drop, 1),
                    "cumulative_drop": True,
                    "baseline_price": float(baseline_price) if baseline_price else None,
                    "observed_pct_off": round(observed_pct_off, 1),
                },
            ))

    # CLEARANCE (savingsCenter == "CLEARANCE") — VERIFIED DEAD 2026-08-22.
    # savings_center has never once held "CLEARANCE" across 78k+ snapshots (only
    # NULL / "Special Buys" / "New Lower Prices"), so this branch is unreachable
    # on the current API. Per-store clearance is detected via pricing.clearance{}
    # (the IN_STORE_CLEARANCE rules); the AlertType.CLEARANCE that does fire comes
    # from the cold-start rule above, not here. Kept rather than deleted in case
    # HD ever populates the field — but do not rely on it firing.
    if (
        curr.savings_center == "CLEARANCE"
        and prev.savings_center != "CLEARANCE"
    ):
        # Trust HD's percentage_off for clearance only if we also see
        # the price below our baseline (or we don't have enough history yet)
        hd_pct_off = curr.percentage_off or 0
        is_confirmed = observed_pct_off is not None and observed_pct_off >= 10
        is_new_product = baseline_price is None
        if is_confirmed or is_new_product:
            effective_pct = round(observed_pct_off) if observed_pct_off else hd_pct_off
            severity = Severity.HIGH if effective_pct >= 50 else Severity.MEDIUM
            # Escalate to HIGH if price_value < 60% of price_original (non-combo)
            if (
                not _is_combo_kit(product)
                and curr.price_original is not None
                and curr.price_value is not None
                and curr.price_original > 0
                and curr.price_value < Decimal("0.6") * curr.price_original
            ):
                severity = Severity.HIGH
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.CLEARANCE,
                severity=severity,
                payload={
                    **base_payload,
                    "percentage_off": effective_pct,
                    "baseline_price": float(baseline_price) if baseline_price else None,
                    "observed_pct_off": round(observed_pct_off, 1) if observed_pct_off else None,
                },
            ))

    # SPECIAL_BUY — require observed price drop ≥15% from our baseline
    if (
        curr.savings_center == "Special Buys"
        and prev.savings_center != "Special Buys"
    ):
        if observed_pct_off is not None and observed_pct_off >= 20:
            severity = Severity.HIGH if observed_pct_off >= 50 else Severity.MEDIUM
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.SPECIAL_BUY,
                severity=severity,
                payload={
                    **base_payload,
                    "percentage_off": round(observed_pct_off),
                    "baseline_price": float(baseline_price) if baseline_price else None,
                    "observed_pct_off": round(observed_pct_off, 1),
                },
            ))
        elif (
            baseline_price is None
            and not _is_combo_kit(product)
            and curr.price_original is not None
            and curr.price_value is not None
            and curr.price_original > 0
            and curr.price_value < Decimal("0.6") * curr.price_original
        ):
            # Fallback for products with no baseline: use price_original
            pct_off_orig = float(
                (curr.price_original - curr.price_value) / curr.price_original * 100
            )
            severity = Severity.HIGH if pct_off_orig >= 50 else Severity.MEDIUM
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.SPECIAL_BUY,
                severity=severity,
                payload={
                    **base_payload,
                    "percentage_off": round(pct_off_orig),
                    "pct_off_original": round(pct_off_orig, 1),
                    "fallback_to_original": True,
                },
            ))

    # DAILY_DEAL detection — Special Buy that disappeared (likely 24h deal expired)
    if (
        prev.savings_center == "Special Buys"
        and curr.savings_center != "Special Buys"
        and curr.price_value is not None
        and prev.price_value is not None
        and curr.price_value > prev.price_value
    ):
        log.info(
            "Special Buy expired (likely daily deal)",
            item_id=curr.item_id,
            store_id=curr.store_id,
            prev_price=float(prev.price_value),
            curr_price=float(curr.price_value),
        )

    # DEEP_DISCOUNT — require observed price drop ≥50% from our reference
    # Falls back to HD's percentage_off only if we lack history
    if ref is not None:
        if observed_pct_off is not None and observed_pct_off >= 50:
            prev_observed: float | None = None
            if ref > 0 and prev.price_value is not None:
                if prev.price_value < ref:
                    prev_observed = float(
                        (ref - prev.price_value) / ref * 100
                    )
            if prev_observed is None or prev_observed < 50:
                alerts.append(Alert(
                    ts=now,
                    store_id=curr.store_id,
                    item_id=curr.item_id,
                    alert_type=AlertType.DEEP_DISCOUNT,
                    severity=Severity.HIGH,
                    payload={
                        **base_payload,
                        "percentage_off": round(observed_pct_off),
                        "baseline_price": float(baseline_price) if baseline_price else None,
                        "reference_price": float(ref),
                        "observed_pct_off": round(observed_pct_off, 1),
                    },
                ))
    else:
        # No baseline yet — fall back to HD's percentage_off for transition detection
        prev_pct = prev.percentage_off or 0
        curr_pct = curr.percentage_off or 0
        if curr_pct >= 50 and prev_pct < 50:
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.DEEP_DISCOUNT,
                severity=Severity.HIGH,
                payload={**base_payload, "percentage_off": curr_pct},
            ))

    # PRICING_ERROR — extreme discount without promotional metadata
    if (
        ref is not None
        and curr.price_value is not None
        and ref > 0
    ):
        pct_off_ref = float((ref - curr.price_value) / ref * 100)
        no_promo = not curr.savings_center and not curr.promotion_tag
        if pct_off_ref >= settings.pricing_error_threshold_pct and no_promo:
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.PRICING_ERROR,
                severity=Severity.HIGH,
                payload={
                    **base_payload,
                    "reference_price": float(ref),
                    "pct_off_reference": round(pct_off_ref, 1),
                    "detection_reason": "extreme_discount_no_promo",
                },
            ))

        # Single-step crash without promo (>60% drop in one cycle)
        if (
            prev.price_value is not None
            and curr.price_value < prev.price_value
            and no_promo
        ):
            step_drop = float(
                (prev.price_value - curr.price_value) / prev.price_value * 100
            )
            if step_drop >= 60 and pct_off_ref < settings.pricing_error_threshold_pct:
                alerts.append(Alert(
                    ts=now,
                    store_id=curr.store_id,
                    item_id=curr.item_id,
                    alert_type=AlertType.PRICING_ERROR,
                    severity=Severity.HIGH,
                    payload={
                        **base_payload,
                        "reference_price": float(ref),
                        "pct_off_reference": round(pct_off_ref, 1),
                        "step_drop_pct": round(step_drop, 1),
                        "detection_reason": "single_step_crash_no_promo",
                    },
                ))

    # IN_STORE_CLEARANCE — clearance pricing appeared or deepened.
    # Only when the deal is obtainable (on shelf, or priced online at the
    # clearance value); OOS-local + in-store-only pricing is unactionable noise.
    if curr.clearance_value is not None and clearance_purchasable(curr):
        prev_cl = prev.clearance_value
        cl_payload = {
            **base_payload,
            "clearance_value": float(curr.clearance_value),
            "clearance_percentage_off": curr.clearance_percentage_off,
            "online_price": float(curr.price_value) if curr.price_value is not None else None,
        }
        if prev_cl is None:
            # Clearance just appeared
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.IN_STORE_CLEARANCE,
                severity=Severity.HIGH,
                payload=cl_payload,
            ))
        elif curr.clearance_value < prev_cl:
            # Clearance price dropped further
            cl_payload["prev_clearance_value"] = float(prev_cl)
            alerts.append(Alert(
                ts=now,
                store_id=curr.store_id,
                item_id=curr.item_id,
                alert_type=AlertType.IN_STORE_CLEARANCE,
                severity=Severity.HIGH,
                payload=cl_payload,
            ))

    # Dedup: if SPECIAL_BUY or CLEARANCE fired, suppress redundant PRICE_DROP
    promo_types = {a.alert_type for a in alerts}
    if AlertType.SPECIAL_BUY in promo_types or AlertType.CLEARANCE in promo_types:
        alerts = [a for a in alerts if a.alert_type != AlertType.PRICE_DROP]

    return alerts


def _build_base_payload(
    prev: StoreSnapshot,
    curr: StoreSnapshot,
    product: Product | None,
) -> dict[str, Any]:
    """Build the common payload with before/after values."""
    return {
        "before": _snapshot_to_dict(prev),
        "after": _snapshot_to_dict(curr),
        "product_title": product.title if product else None,
        "product_url": _product_url(product, curr.item_id),
        "image_url": product.image_url if product else None,
    }


def _snapshot_to_dict(snap: StoreSnapshot) -> dict[str, Any]:
    return {
        "price_value": float(snap.price_value) if snap.price_value is not None else None,
        "price_original": float(snap.price_original) if snap.price_original is not None else None,
        "savings_center": snap.savings_center,
        "percentage_off": snap.percentage_off,
        "special_buy": snap.special_buy,
        "clearance_value": float(snap.clearance_value) if snap.clearance_value is not None else None,
        "clearance_percentage_off": snap.clearance_percentage_off,
        "in_stock": snap.in_stock,
        "inventory_qty": snap.inventory_qty,
    }
