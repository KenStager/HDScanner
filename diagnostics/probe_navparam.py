"""Diagnostic: does the Tools navParam exclude the target item?

Read-only. Uses the model number as keyword so each config is a 1-request test.
"""

import asyncio

from hd.config import Settings
from hd.hd_api import graphql
from hd.http.client import HDClient

TARGET = "337128401"
MODEL_KW = "0970-20"


def probe_result(raw):
    sm = raw.get("data", {}).get("searchModel") or {}
    total = (sm.get("searchReport") or {}).get("totalProducts")
    products = sm.get("products") or []
    idx = next((i for i, p in enumerate(products) if str(p.get("itemId")) == TARGET), None)
    return total, idx, len(products)


async def main():
    settings = Settings()
    nav = settings.tools_nav_param
    store = settings.store_list[0]
    print(f"tools_nav_param={nav}  store={store}\n")

    client = HDClient(settings)
    try:
        # Decisive: same keyword, ALL filter, with vs without the Tools navParam
        for label, nav_param in (("WITH tools navParam", nav), ("WITHOUT navParam", None)):
            raw = await graphql.search(
                client, keyword=MODEL_KW, nav_param=nav_param, store_id=store,
                start_index=0, page_size=24, storefilter="ALL",
            )
            total, idx, n = probe_result(raw)
            print(f"[{label}] total={total} returned={n} target={'FOUND idx=%d' % idx if idx is not None else 'ABSENT'}")

        print()
        # How deep is the item under the real scan keywords (nav + ALL)?
        for kw in settings.scan_keyword_list:
            raw = await graphql.search(
                client, keyword=kw, nav_param=nav, store_id=store,
                start_index=0, page_size=24, storefilter="ALL",
            )
            total, idx, n = probe_result(raw)
            pages = (total + 23) // 24 if total else 0
            print(f"[discovery-shaped '{kw}' nav+ALL] total={total} pages={pages} "
                  f"cap=10 {'TRUNCATED' if pages > 10 else 'full'}")
    finally:
        await client.close()


asyncio.run(main())
