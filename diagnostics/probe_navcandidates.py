"""Recon: find a navParam that spans categories (so Garage items are reachable).

Also re-verifies the "broad keyword + navParam=null returns empty" claim with a
full response dump, since that conclusion rested on a single observation.

Read-only.
"""

import asyncio
import json

from hd.config import Settings
from hd.hd_api import graphql
from hd.http.client import HDClient

TARGET = "337128401"

# N-5yc1v is the root. Zc1xy = Tools. Zmki = Milwaukee (per research doc, unconfirmed).
CANDIDATES = [
    ("root only",            "N-5yc1v",            "Milwaukee"),
    ("Milwaukee brand only", "N-5yc1vZmki",        None),
    ("Milwaukee brand + kw", "N-5yc1vZmki",        "Milwaukee"),
    ("Tools + Milwaukee",    "N-5yc1vZc1xyZmki",   None),
    ("Tools (current)",      "N-5yc1vZc1xy",       "Milwaukee"),
]


def summarize(raw):
    if "errors" in raw:
        errs = raw.get("errors") or []
        msg = errs[0].get("message", "")[:90] if errs else ""
        return f"GRAPHQL ERROR: {msg}"
    sm = (raw.get("data") or {}).get("searchModel")
    if sm is None:
        return f"searchModel=None  top-level keys={list(raw.keys())}"
    total = (sm.get("searchReport") or {}).get("totalProducts")
    products = sm.get("products") or []
    hit = any(str(p.get("itemId")) == TARGET for p in products)
    return f"total={total} returned={len(products)} target_on_p0={'YES' if hit else 'no'}"


async def main():
    settings = Settings()
    store = settings.store_list[0]
    client = HDClient(settings)
    try:
        print("=== navParam candidates (storefilter=ALL, page 0) ===")
        for label, nav, kw in CANDIDATES:
            raw = await graphql.search(
                client, keyword=kw, nav_param=nav, store_id=store,
                start_index=0, page_size=24, storefilter="ALL",
            )
            print(f"  [{label:22}] nav={nav:18} kw={kw!r:14} -> {summarize(raw)}")

        print("\n=== re-verify: broad keyword, navParam=null (full dump) ===")
        raw = await graphql.search(
            client, keyword="Milwaukee PACKOUT", nav_param=None, store_id=store,
            start_index=0, page_size=24, storefilter="ALL",
        )
        print(f"  summary: {summarize(raw)}")
        print(f"  raw (first 600 chars): {json.dumps(raw)[:600]}")
    finally:
        await client.close()


asyncio.run(main())
