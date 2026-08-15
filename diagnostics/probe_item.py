"""Diagnostic: why is a given itemId absent from the captured set?

Read-only probe. Uses the app's own rate-limited client and query.
"""

import asyncio
import sys

from hd.config import Settings
from hd.hd_api import graphql
from hd.http.client import HDClient

TARGET = "337128401"


def find_target(raw, target=TARGET):
    products = (raw.get("data", {}).get("searchModel") or {}).get("products") or []
    for i, p in enumerate(products):
        if str(p.get("itemId")) == target:
            return i, p
    return None, None


def total(raw):
    sm = raw.get("data", {}).get("searchModel") or {}
    return (sm.get("searchReport") or {}).get("totalProducts")


def describe(p, store_id):
    ids = p.get("identifiers") or {}
    pricing = p.get("pricing") or {}
    promo = pricing.get("promotion") or {}
    avail = p.get("availabilityType") or {}
    print(f"      brand={ids.get('brandName')} model={ids.get('modelNumber')}")
    print(f"      title={(ids.get('productLabel') or '')[:70]}")
    print(f"      price={pricing.get('value')} original={pricing.get('original')}")
    print(f"      promoTag={promo.get('promotionTag')} savingsCenter={promo.get('savingsCenter')} pctOff={promo.get('percentageOff')}")
    print(f"      availability={avail.get('type')} buyable={avail.get('buyable')} discontinued={avail.get('discontinued')}")
    # store inventory
    for fo in (p.get("fulfillment") or {}).get("fulfillmentOptions") or []:
        for svc in fo.get("services") or []:
            for loc in svc.get("locations") or []:
                if str(loc.get("locationId")) == str(store_id):
                    inv = loc.get("inventory") or {}
                    print(f"      @store {store_id}: qty={inv.get('quantity')} inStock={inv.get('isInStock')} type={fo.get('type')}/{svc.get('type')}")


async def main():
    settings = Settings()
    stores = [s.strip() for s in settings.stores.split(",") if s.strip()]
    print(f"config stores={stores} storefilter={settings.snapshot_storefilter} max_pages={settings.max_pages}")
    print(f"scan keywords={settings.scan_keywords}\n")

    client = HDClient(settings)
    try:
        # Test 1: direct model-number search, both storefilters, store 2619
        for sf in ("IN_STORE", "ALL"):
            raw = await graphql.search(
                client, keyword="0970-20", store_id="2619",
                start_index=0, page_size=24, storefilter=sf,
            )
            idx, p = find_target(raw)
            print(f"[model '0970-20' @2619 storefilter={sf}] total={total(raw)} found={'YES idx=%d' % idx if p else 'NO'}")
            if p:
                describe(p, "2619")
        print()

        # Test 2: how deep does 'Milwaukee PACKOUT' go, and is the item in range?
        for store in stores:
            raw = await graphql.search(
                client, keyword="Milwaukee PACKOUT", store_id=store,
                start_index=0, page_size=24,
                storefilter=settings.snapshot_storefilter,
            )
            t = total(raw)
            pages = (t + 23) // 24 if t else 0
            capped = min(pages, settings.max_pages)
            print(f"[keyword 'Milwaukee PACKOUT' @{store} sf={settings.snapshot_storefilter}] "
                  f"total={t} pages={pages} scanned={capped} "
                  f"{'TRUNCATED -> ' + str((pages - capped) * 24) + ' products never seen' if pages > capped else 'full coverage'}")
        print()

        # Test 3: is it visible at each store under ALL?
        for store in stores:
            raw = await graphql.search(
                client, keyword="Milwaukee PACKOUT wet dry vacuum impact driver",
                store_id=store, start_index=0, page_size=24, storefilter="ALL",
            )
            idx, p = find_target(raw)
            print(f"[descriptive keyword @{store} sf=ALL] total={total(raw)} found={'YES idx=%d' % idx if p else 'NO'}")
            if p:
                describe(p, store)
    finally:
        await client.close()


asyncio.run(main())
