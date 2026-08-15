"""Page-window rotation across scheduled runs.

The catalog is far larger than one run's request budget: a single keyword can
need 90+ pages while the whole run is capped at ~100 requests. Restarting every
run at page 0 means the deep tail is never fetched, no matter how often we run.

Rotation keeps a per-(keyword, store, storefilter) cursor on disk. Each run
walks a slice of pages starting where the previous run stopped, wrapping at the
end. Six runs a day therefore cover roughly six slices of depth instead of the
same first slice six times.

The cursor is advisory. A missing or corrupt file just restarts at page 0 —
never a hard failure, since losing coverage is preferable to losing the run.
"""

from __future__ import annotations

import json
from pathlib import Path

from hd.logging import get_logger

log = get_logger("rotation")


def _key(keyword: str, store_id: str, storefilter: str) -> str:
    return f"{keyword}|{store_id}|{storefilter}"


def load_cursors(path: str) -> dict[str, int]:
    """Read the cursor map. Returns {} when absent or unreadable."""
    try:
        p = Path(path)
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            return {}
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception as e:
        log.warning("Could not read rotation cursor, starting at page 0", error=str(e))
        return {}


def save_cursors(path: str, cursors: dict[str, int]) -> None:
    """Persist the cursor map. Failure to save is logged, never raised."""
    try:
        Path(path).write_text(json.dumps(cursors, indent=2, sort_keys=True))
    except Exception as e:
        log.warning("Could not persist rotation cursor", error=str(e))


def next_window(
    cursors: dict[str, int],
    keyword: str,
    store_id: str,
    storefilter: str,
    slice_pages: int,
    max_pages: int,
) -> list[int]:
    """Page numbers this run should walk for one keyword/store/storefilter.

    Always includes page 0 — the first page carries the freshest, best-matching
    results and totalProducts, so we never trade away the head to reach the tail.
    """
    if slice_pages <= 0 or max_pages <= 0:
        return [0]

    start = cursors.get(_key(keyword, store_id, storefilter), 0) % max_pages
    window = [(start + i) % max_pages for i in range(min(slice_pages, max_pages))]

    if 0 not in window:
        window = [0] + window[:-1] if len(window) > 1 else [0]
    # Ascending order keeps startIndex monotonic, which matches how a browser pages.
    return sorted(set(window))


def advance(
    cursors: dict[str, int],
    keyword: str,
    store_id: str,
    storefilter: str,
    slice_pages: int,
    max_pages: int,
) -> None:
    """Move the cursor forward by one slice, wrapping at max_pages."""
    if max_pages <= 0:
        return
    k = _key(keyword, store_id, storefilter)
    cursors[k] = (cursors.get(k, 0) + max(slice_pages, 1)) % max_pages
