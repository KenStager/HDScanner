"""Test points 1 and 4: clearance navParam yield, and larger page_size.

Aborts immediately on 206/throttle so we never hammer a quota wall.
Logs are NOT filtered by the caller — read them.
"""

import asyncio

from hd.config import Settings
from hd.hd_api import graphql
from hd.http.client import HDClient

TARGET = "337128401"


def read(raw):
    if "errors" in raw:
        return None, 0, "GRAPHQL ERROR"
    sm = (raw.get("data") or {}).get("searchModel")
    if sm is None:
        return None, 0, "no searchModel"
    total = (sm.get("searchReport") or {}).get("totalProducts")
    products = sm.get("products") or []
    return total, len(products), None


def discount_stats(raw):
    """How many returned products are actually discounted?"""
    sm = (raw.get("data") or {}).get("searchModel") or {}
    products = sm.get("products") or []
    disc = 0
    for p in products:
        pricing = p.get("pricing") or {}
        promo = pricing.get("promotion") or {}
        clearance = pricing.get("clearance") or {}
        if (promo.get("percentageOff") or promo.get("dollarOff")
                or promo.get("savingsCenter") or pricing.get("specialBuy")
                or clearance.get("value")):
            disc += 1
    return disc, len(products)


async def main():
    s = Settings()
    store = s.store_list[0]
    c = HDClient(s)
    try:
        # --- quota check: one request, current production shape ---
        raw = await graphql.search(
            c, keyword="Milwaukee", nav_param=s.tools_nav_param, store_id=store,
            start_index=0, page_size=24, storefilter="ALL",
        )
        total, n, err = read(raw)
        if c.is_throttled or (total is None and n == 0):
            print(f"QUOTA STILL EXHAUSTED (throttled={c.is_throttled}) — aborting, no further calls.")
            return
        d, t = discount_stats(raw)
        print(f"[baseline  Tools nav, kw=Milwaukee] total={total} returned={n} discounted={d}/{t}")

        # --- POINT 1: clearance navParam yield ---
        clear_nav = f"{s.tools_nav_param}Z{s.clearance_token}"
        raw = await graphql.search(
            c, keyword=None, nav_param=clear_nav, store_id=store,
            start_index=0, page_size=24, storefilter="ALL",
        )
        total, n, err = read(raw)
        if c.is_throttled:
            print("throttled — aborting"); return
        d, t = discount_stats(raw)
        print(f"[P1 clearance nav {clear_nav}] total={total} returned={n} discounted={d}/{t}")

        # clearance nav + brand keyword
        raw = await graphql.search(
            c, keyword="Milwaukee", nav_param=clear_nav, store_id=store,
            start_index=0, page_size=24, storefilter="ALL",
        )
        total, n, err = read(raw)
        if c.is_throttled:
            print("throttled — aborting"); return
        d, t = discount_stats(raw)
        print(f"[P1 clearance nav + kw=Milwaukee] total={total} returned={n} discounted={d}/{t}")

        # --- POINT 4: does a larger page_size work? ---
        for ps in (48, 96):
            raw = await graphql.search(
                c, keyword="Milwaukee", nav_param=s.tools_nav_param, store_id=store,
                start_index=0, page_size=ps, storefilter="ALL",
            )
            total, n, err = read(raw)
            if c.is_throttled:
                print("throttled — aborting"); return
            print(f"[P4 page_size={ps}] total={total} returned={n} "
                  f"{'HONORED' if n == ps else 'CAPPED at %d' % n}")
    finally:
        print(f"\nrequests used this probe: {c.request_count}")
        await c.close()


asyncio.run(main())
