"""Diagnostic: what category does the target item live in?

Read-only, single request. Swaps the client's cached query for the taxonomy
variant in queries/itemTaxonomy.graphql (no inlined GraphQL).
"""

import asyncio
import json
from pathlib import Path

from hd.config import Settings
from hd.hd_api import graphql
from hd.http.client import HDClient

TARGET = "337128401"
MODEL_KW = "0970-20"


async def main():
    settings = Settings()
    client = HDClient(settings)
    # Swap in the taxonomy query for this probe only.
    client._query_cache = (Path("..") / "queries" / "itemTaxonomy.graphql").read_text().strip()
    try:
        raw = await graphql.search(
            client, keyword=MODEL_KW, nav_param=None,
            store_id=settings.store_list[0],
            start_index=0, page_size=24, storefilter="ALL",
        )
        sm = raw.get("data", {}).get("searchModel") or {}
        for p in sm.get("products") or []:
            if str(p.get("itemId")) != TARGET:
                continue
            ids = p.get("identifiers") or {}
            info = p.get("info") or {}
            print(f"itemId       {p.get('itemId')}")
            print(f"productType  {ids.get('productType')}")
            print(f"canonicalUrl {ids.get('canonicalUrl')}")
            print(f"department   {info.get('productDepartment')}")
            print(f"categoryHierarchy {json.dumps(info.get('categoryHierarchy'), indent=2)}")
            return
        print("target not in response")
    finally:
        await client.close()


asyncio.run(main())
