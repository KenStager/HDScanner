"""Diagnostic: at what page depth is the target reachable without the navParam?

Read-only. Bounded page walk so we stay polite to the API.
"""

import asyncio

from hd.config import Settings
from hd.hd_api import graphql
from hd.http.client import HDClient

TARGET = "337128401"
MAX_PROBE_PAGES = 20


async def walk(client, settings, keyword, nav_param, storefilter):
    store = settings.store_list[0]
    total = None
    for page in range(MAX_PROBE_PAGES):
        raw = await graphql.search(
            client, keyword=keyword, nav_param=nav_param, store_id=store,
            start_index=page * settings.page_size, page_size=settings.page_size,
            storefilter=storefilter,
        )
        sm = raw.get("data", {}).get("searchModel") or {}
        if total is None:
            total = (sm.get("searchReport") or {}).get("totalProducts")
        products = sm.get("products") or []
        for i, p in enumerate(products):
            if str(p.get("itemId")) == TARGET:
                return total, page, page * settings.page_size + i
        if len(products) < settings.page_size:
            break
    return total, None, None


async def main():
    settings = Settings()
    client = HDClient(settings)
    try:
        for kw in settings.scan_keyword_list:
            total, page, rank = await walk(client, settings, kw, None, "ALL")
            if page is None:
                print(f"[{kw!r} nav=None sf=ALL] total={total} -> NOT FOUND in first {MAX_PROBE_PAGES} pages")
            else:
                print(f"[{kw!r} nav=None sf=ALL] total={total} -> FOUND page={page} rank={rank}")
    finally:
        await client.close()


asyncio.run(main())
