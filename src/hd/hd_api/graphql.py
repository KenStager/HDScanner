"""GraphQL request builder and sender."""

from __future__ import annotations

from typing import Any

from hd.config import Settings
from hd.http.client import HDClient


def is_valid_search_response(raw: dict) -> bool:
    """Check if response contains a valid searchModel payload."""
    if not isinstance(raw, dict):
        return False
    if "error" in raw or "errors" in raw:
        return False
    data = raw.get("data")
    if not isinstance(data, dict):
        return False
    search_model = data.get("searchModel")
    return search_model is not None


async def search(
    client: HDClient,
    *,
    keyword: str | None = None,
    nav_param: str | None = None,
    store_id: str,
    start_index: int = 0,
    page_size: int = 24,
) -> dict[str, Any]:
    """Execute a searchModel GraphQL query and return raw JSON response."""
    variables: dict[str, Any] = {
        "keyword": keyword,
        "navParam": nav_param,
        "storeId": store_id,
        "storefilter": "ALL",
        "channel": "DESKTOP",
        "isBrandPricingPolicyCompliant": False,
        "skipInstallServices": True,
        "skipFavoriteCount": True,
        "skipDiscoveryZones": True,
        "skipBuyitagain": True,
        "additionalSearchParams": {
            "deliveryZip": "",
            "multiStoreIds": [],
        },
        "filter": {},
        "orderBy": {"field": "BEST_MATCH", "order": "ASC"},
        "pageSize": page_size,
        "startIndex": start_index,
    }

    return await client.post_graphql(variables)
