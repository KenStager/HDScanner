"""Alert grouping logic — shared between dashboard and notifiers.

Groups alerts by (item_id, alert_type) within a configurable time window
so that the same event detected at multiple stores collapses into one group.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
GROUP_WINDOW_MINUTES: int = 10


def parse_ts(ts: datetime | str | None) -> datetime:
    """Coerce a timestamp to an aware datetime, default to epoch on None."""
    if ts is None:
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    if isinstance(ts, str):
        dt = datetime.fromisoformat(ts)
    else:
        dt = ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_group(store_alerts: list[dict]) -> dict:
    """Build a representative group dict from one or more per-store alerts."""

    def _rank(a: dict) -> tuple[int, float]:
        sev = SEVERITY_RANK.get(str(a.get("severity", "")).lower(), -1)
        pct = float((a.get("payload") or {}).get("pct_drop") or 0)
        return (sev, pct)

    rep = max(store_alerts, key=_rank)
    most_recent = max(store_alerts, key=lambda a: parse_ts(a.get("ts")))
    earliest = min(store_alerts, key=lambda a: a.get("id", 0))

    store_ids = sorted({str(a.get("store_id", "")) for a in store_alerts})
    store_ids_display = ", ".join(store_ids)

    item_id = rep.get("item_id", "")
    alert_type = rep.get("alert_type", "")
    earliest_id = earliest.get("id", 0)
    group_key = f"{item_id}_{alert_type}_{earliest_id}"

    return {
        "group_key": group_key,
        "store_count": len(store_ids),
        "store_ids_display": store_ids_display,
        "ts": most_recent.get("ts"),
        "ts_dt": parse_ts(most_recent.get("ts")),
        "item_id": item_id,
        "alert_type": alert_type,
        "severity": rep.get("severity", ""),
        "payload": rep.get("payload") or {},
        "product_title": rep.get("product_title") or "",
        "store_alerts": store_alerts,
    }


def group_alerts(alerts_list: list[dict]) -> list[dict]:
    """Group alerts by (item_id, alert_type) within a 10-minute window.

    Returns a list of *group* dicts (one per group), sorted most-recent first.
    """
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for a in alerts_list:
        key = (str(a.get("item_id", "")), str(a.get("alert_type", "")))
        buckets[key].append(a)

    groups: list[dict] = []
    window = timedelta(minutes=GROUP_WINDOW_MINUTES)

    for _key, bucket in buckets.items():
        bucket.sort(key=lambda a: parse_ts(a.get("ts")))

        sub_group: list[dict] = [bucket[0]]
        for a in bucket[1:]:
            prev_ts = parse_ts(sub_group[-1].get("ts"))
            cur_ts = parse_ts(a.get("ts"))
            if (cur_ts - prev_ts) <= window:
                sub_group.append(a)
            else:
                groups.append(build_group(sub_group))
                sub_group = [a]
        groups.append(build_group(sub_group))

    groups.sort(key=lambda g: g["ts_dt"], reverse=True)
    return groups
