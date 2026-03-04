# Reverse-engineering Home Depot's internal API

**Home Depot's web app runs on a federated GraphQL API at `apionline.homedepot.com/federation-gateway/graphql` that requires no API key—just browser-mimicking headers—and returns rich product, pricing, promotion, and real-time store-level inventory data in a single call.** This endpoint powers all product search, category browsing, and inventory lookups on homedepot.com. It accepts a `storeId` parameter that controls both local pricing and exact inventory counts, and its response schema includes dedicated clearance and promotion fields (`savingsCenter`, `specialBuy`, `dollarOff`, `percentageOff`) that can identify sale and clearance items programmatically. Below is a complete technical breakdown based on publicly available community reverse-engineering, browser network analysis documentation, and third-party API service documentation.

---

## The GraphQL federation gateway is the single API that matters

Home Depot uses **Apollo Federation** behind a single GraphQL endpoint. Two host variants have been observed in the wild:

- **`https://apionline.homedepot.com/federation-gateway/graphql?opname=searchModel`** — current primary backend
- **`https://www.homedepot.com/federation-gateway/graphql?opname=searchModel`** — older variant, may still work

Both accept **POST** requests with a JSON body containing `operationName`, `variables`, and `query` fields. The `opname` query parameter identifies the GraphQL operation. The two key operations are:

| Operation | Purpose |
|---|---|
| `searchModel` | Product search results, category browsing, filtering |
| `productClientOnlyProduct` | Individual product detail pages |

**No API key, OAuth token, or session cookie is strictly required.** Authentication relies on browser-like headers. However, after sustained automated access, cookies from a real browser session may become necessary to avoid blocks.

### Required headers

```
Host: apionline.homedepot.com
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0
Accept: */*
Accept-Language: en-US,en;q=0.5
Accept-Encoding: gzip, deflate, br, zstd
Referer: https://www.homedepot.com/
Content-Type: application/json
Origin: https://www.homedepot.com
x-experience-name: general-merchandise
x-hd-dc: origin
x-debug: false
```

The three custom headers—**`x-experience-name`**, **`x-hd-dc`**, and **`x-debug`**—are the key proprietary ones. Omitting them may cause request rejection. There is no `x-api-key` header required.

---

## Complete searchModel request structure with curl example

The `searchModel` operation handles both keyword searches and category browsing. Here is a concrete, copy-pasteable curl command for searching Milwaukee tools with store-specific pricing and inventory:

```bash
curl -X POST 'https://apionline.homedepot.com/federation-gateway/graphql?opname=searchModel' \
  -H 'Host: apionline.homedepot.com' \
  -H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0' \
  -H 'Accept: */*' \
  -H 'Accept-Language: en-US,en;q=0.5' \
  -H 'Accept-Encoding: gzip, deflate, br, zstd' \
  -H 'Referer: https://www.homedepot.com/' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://www.homedepot.com' \
  -H 'x-experience-name: general-merchandise' \
  -H 'x-hd-dc: origin' \
  -H 'x-debug: false' \
  -d '{
    "operationName": "searchModel",
    "variables": {
      "keyword": "Milwaukee",
      "navParam": "N-5yc1vZc1xy",
      "storeId": "6521",
      "storefilter": "ALL",
      "channel": "DESKTOP",
      "isBrandPricingPolicyCompliant": false,
      "skipInstallServices": true,
      "skipFavoriteCount": true,
      "skipDiscoveryZones": true,
      "skipBuyitagain": true,
      "additionalSearchParams": {
        "deliveryZip": "78520",
        "multiStoreIds": []
      },
      "filter": {},
      "orderBy": {
        "field": "BEST_MATCH",
        "order": "ASC"
      },
      "pageSize": 24,
      "startIndex": 0
    },
    "query": "query searchModel($storeId:String,$startIndex:Int,$pageSize:Int,$orderBy:ProductSort,$filter:ProductFilter,$isBrandPricingPolicyCompliant:Boolean,$keyword:String,$navParam:String,$storefilter:StoreFilter=ALL,$channel:Channel=DESKTOP,$additionalSearchParams:AdditionalParams){searchModel(keyword:$keyword,navParam:$navParam,storefilter:$storefilter,isBrandPricingPolicyCompliant:$isBrandPricingPolicyCompliant,storeId:$storeId,channel:$channel,additionalSearchParams:$additionalSearchParams){products(startIndex:$startIndex,pageSize:$pageSize,orderBy:$orderBy,filter:$filter){itemId identifiers{brandName modelNumber canonicalUrl productLabel storeSkuNumber itemId productType parentId isSuperSku}pricing(storeId:$storeId,isBrandPricingPolicyCompliant:$isBrandPricingPolicyCompliant){value original alternatePriceDisplay mapAboveOriginalPrice message preferredPriceFlag promotion{type description dollarOff percentageOff promotionTag savingsCenter savingsCenterPromos specialBuySavings specialBuyDollarOff specialBuyPercentageOff dates}specialBuy unitOfMeasure}reviews{ratingsReviews{averageRating totalReviews}}media{images{url type subType sizes}}fulfillment{anchorStoreStatus anchorStoreStatusType backordered backorderedShipDate fulfillmentOptions{type fulfillable services{deliveryTimeline deliveryDates deliveryCharge dynamicEta hasFreeShipping freeDeliveryThreshold type totalCharge locations{curbsidePickupFlag isBuyInStoreCheckNearBy distance inventory{isOutOfStock isInStock isLimitedQuantity isUnavailable quantity maxAllowedBopisQty minAllowedBopisQty}isAnchor locationId state storeName storePhone type}}}onlineStoreStatus onlineStoreStatusType}info{hidePrice ecoRebate quantityLimit categoryHierarchy sskMin sskMax productSubType isLiveGoodsProduct isSponsored hasSubscription samplesAvailable totalNumberOfOptions classNumber productDepartment}availabilityType{type discontinued buyable status}badges{name label}favoriteDetail{count}keyProductFeatures{keyProductFeaturesItems{features{name refinementId refinementUrl value}}}taxonomy{breadCrumbs}details{installation collection{name url collectionId}highlights}}}}"
  }'
```

### Key variables explained

| Variable | Value | Purpose |
|---|---|---|
| `keyword` | `"Milwaukee"` | Free-text search term; set to `null` when using `navParam` alone |
| `navParam` | `"N-5yc1vZc1xy"` | Category navigation token (Tools = `c1xy`); set to `null` for keyword-only searches |
| `storeId` | `"6521"` | 3-4 digit store number; **critical** for local pricing and inventory |
| `pageSize` | `24` | Home Depot's default page size |
| `startIndex` | `0` | Pagination offset (0, 24, 48, 72…) |
| `orderBy.field` | `"BEST_MATCH"` | Options: `BEST_MATCH`, `PRICE`, `TOP_RATED`, `TOP_SELLERS` |
| `orderBy.order` | `"ASC"` or `"DESC"` | Sort direction; use `ASC` with `PRICE` for low-to-high |
| `filter` | `{}` | Object for facet filtering (brand tokens, price ranges, clearance) |
| `deliveryZip` | `"78520"` | ZIP code for delivery pricing/availability |
| `channel` | `"DESKTOP"` | Also accepts `"MOBILE"` |

**Without a `storeId`, the API defaults to store 2414 (Bangor, Maine)**, which will return irrelevant inventory and potentially different pricing for your area.

---

## Clearance and sale detection relies on multiple response fields

Home Depot does not expose a simple `clearance=true` parameter in the request. Instead, clearance and sale status must be detected from **response data** and **navigation tokens**.

### Request-side clearance filtering via navParam

The most direct approach is to use the **clearance navigation token `1z11adf`**, appended to a category's N-parameter with a `Z` separator. For clearance items in the Tools category:

```
"navParam": "N-5yc1vZc1xyZ1z11adf"
```

This is equivalent to browsing `https://www.homedepot.com/b/Tools/Clearance/N-5yc1vZc1xyZ1z11adf`. Other confirmed filter tokens include:

- **`1z11adf`** — Clearance
- **`1z179pc`** — Recently Added
- **`1z175a5`** — Pick Up Today
- **`1z175cq`** — Next-Day Delivery
- **`bwo5s`** — Hide Unavailable Products

### Response-side clearance detection

Within each product's `pricing` object, these fields indicate discounts and clearance:

```json
{
  "pricing": {
    "value": 49.97,
    "original": 129.00,
    "promotion": {
      "type": "CLEARANCE",
      "dollarOff": 79.03,
      "percentageOff": 61,
      "promotionTag": "Clearance",
      "savingsCenter": "CLEARANCE",
      "savingsCenterPromos": "...",
      "specialBuySavings": null,
      "specialBuyDollarOff": null,
      "specialBuyPercentageOff": null
    },
    "specialBuy": null
  }
}
```

**The key detection logic for a clearance monitor:**

- **`pricing.value < pricing.original`** → product is discounted
- **`pricing.promotion.savingsCenter`** → contains `"CLEARANCE"` for clearance items
- **`pricing.promotion.promotionTag`** → human-readable label like `"Clearance"`
- **`pricing.promotion.percentageOff`** → discount percentage (filter for deep clearance, e.g., >50%)
- **`pricing.specialBuy`** → non-null for Special Buy promotions
- **`badges[].name`** → can contain `"clearance"` as a badge
- **`availabilityType.discontinued`** → `true` signals discontinued product (often clearance)

The community has also documented **in-store penny pricing patterns**: prices ending in **.06** indicate a second markdown, **.03** a final markdown, and **.01** a "penny item" being fully discontinued—though these patterns apply to physical shelf tags, not the API response directly.

---

## Store-level inventory is embedded in the search response

There is no separate inventory-check endpoint you need to call. **Inventory data comes back inline with every `searchModel` response** under the `fulfillment` object, scoped to the `storeId` you pass in the request.

### Inventory response path

```
data.searchModel.products[].fulfillment.fulfillmentOptions[].services[].locations[]
```

Each location object looks like:

```json
{
  "locationId": "6521",
  "storeName": "Brownsville",
  "state": "TX",
  "storePhone": "(956)544-5466",
  "type": "store",
  "isAnchor": true,
  "inventory": {
    "isOutOfStock": false,
    "isInStock": true,
    "isLimitedQuantity": false,
    "isUnavailable": false,
    "quantity": 47,
    "maxAllowedBopisQty": null,
    "minAllowedBopisQty": null
  }
}
```

**`inventory.quantity` gives the exact count** at the specified store. The `fulfillmentOptions` array contains entries for both `"pickup"` (BOPIS—Buy Online Pick Up In Store) and `"delivery"` types, each with their own service types (`"bopis"`, `"express delivery"`, etc.) and location-level inventory data.

### Store ID format and lookup

Store IDs are **3-4 digit numeric strings** (e.g., `"6521"`, `"2414"`, `"922"`). To find store IDs programmatically:

- **SerpApi** publishes a complete list of all **1,776 US stores** at `https://serpapi.com/home-depot-stores-us`
- **Unwrangle** provides a downloadable JSON file of all store numbers and ZIP codes
- **Home Depot's store directory** lives at `https://www.homedepot.com/l/storeDirectory`
- The store ID appears in the browser's upper-left store selector on homedepot.com

For a clearance monitor, you would pass your target store's ID (or iterate over multiple stores) to get location-specific pricing and stock levels.

---

## Brand filtering uses Endeca navigation tokens

Home Depot's catalog runs on an **Endeca-based navigation system** where every facet—category, brand, price range, promotion type—is encoded as a short alphanumeric token appended to the `N-` parameter with `Z` separators.

### Applying brand filters

**Option 1: Via navParam token chaining.** Append the brand's token to the category navigation string:

```
N-5yc1vZc1xyZ4j2     → Tools + DEWALT (confirmed token: 4j2)
N-5yc1vZc1xyZmki     → Tools + Milwaukee (likely token: mki)
```

Pass this as `"navParam": "N-5yc1vZc1xyZ4j2"` in the variables and set `"keyword": null`.

**Option 2: Via keyword + navParam.** Set `"keyword": "DeWalt"` and `"navParam": "N-5yc1vZc1xy"` to search for DeWalt within the Tools category.

**Option 3: Post-response filtering.** Search the full Tools category and filter results client-side using `identifiers.brandName === "DEWALT"` or `identifiers.brandName === "Milwaukee"`.

Brand tokens are **context-dependent**—the same brand may use different tokens across subcategories. The reliable way to discover the exact token for a given brand-category combination is to inspect the API response's filter/facet data, which returns available brand tokens for the current result set. **DEWALT's confirmed token is `4j2`** (verified in Power Tool sub-categories). Milwaukee's token appears to be `mki` based on URL patterns, though independent verification from response data is recommended.

To combine brand + clearance + category, chain all tokens:

```
N-5yc1vZc1xyZ4j2Z1z11adf   → Tools + DEWALT + Clearance
N-5yc1vZc1xyZmkiZ1z11adf   → Tools + Milwaukee + Clearance
```

---

## Rate limiting and anti-bot countermeasures

Home Depot employs several layers of bot detection, though specific thresholds are not publicly documented:

- **IP-based rate limiting** — sustained automated requests from a single IP trigger blocks (HTTP 403 or 429 responses) or CAPTCHA challenges
- **No fixed published rate limit** — community consensus suggests keeping requests to **≤1 per second** with randomized delays
- **Cookie escalation** — initially no cookies are required, but after detection signals accumulate, a valid browser session cookie may become necessary
- **JavaScript fingerprinting** — the site uses client-side JS for bot detection; requests from `curl` or `requests` libraries lack these signals, making them more likely to be flagged over time
- **GraphQL schema instability** — the schema changes periodically without notice, which passively breaks automated clients

**Practical mitigation strategies** from the scraping community include rotating residential proxies, adding **1-3 second random delays** between requests, rotating User-Agent strings, and periodically refreshing cookies from a real browser session. For a clearance monitor running a few checks per hour, basic header mimicry and modest delays are typically sufficient.

---

## Community tools and GitHub projects already exist

Multiple open-source projects and commercial tools target Home Depot data extraction:

- **`Ken-Watson/home-depot-scraper-pdm`** — Python/Scrapy scraper for product data with database storage
- **`coronel08/HomeDepot_PriceScrape`** — Python price monitor that reads product lists from Excel and tracks price changes; uses Selenium with randomized delays
- **`eneiromatos/the-home-depot-web-scraper`** — TypeScript Apify/Crawlee actor supporting search, category, and product URL scraping; exports to JSON/CSV/Excel
- **`byazici/python-homedepot-scrape`** — BeautifulSoup scraper storing to MongoDB with brand, price, and specs extraction
- **`aclark4life/home-depot-crawl`** — Scrapy-based crawler
- **`dimitryzub/ecommerce-scraper-py`** — Multi-retailer scraper using SerpApi as its backend, with Home Depot price range filtering built in

**Commercial clearance monitoring tools** include **Clearance Scout** (Discord-based alerts with brand/discount/inventory filtering, sold via Whop) and **Endless** (tracks markdown cycles with real-time alerts; notes that HD price changes propagate in the system **24-48 hours before shelf tags update**). **BrickSeek** offers a free inventory checker by SKU and ZIP code.

Third-party API services that normalize Home Depot data include **SerpApi** (`home_depot` engine, 100 free searches/month), **BigBox API** by Traject Data (from $15/month), **Unwrangle** (provides `inventory_quantity`, `discount.percentage`, `aisle_bay` fields), and multiple **Apify actors** including a dedicated store inventory lookup tool.

---

## Conclusion

Building a Milwaukee/DeWalt clearance monitor requires just one API endpoint: the `searchModel` GraphQL operation at `apionline.homedepot.com/federation-gateway/graphql`. The most efficient approach is to query with `navParam` set to the Tools category token (`c1xy`) combined with the clearance token (`1z11adf`) and brand tokens (`4j2` for DEWALT, `mki` for Milwaukee), passing your target `storeId` to get local pricing and inventory. Detect deep clearance by checking `pricing.promotion.percentageOff` for high discount percentages and `pricing.promotion.savingsCenter` for the `"CLEARANCE"` flag. The `fulfillment.fulfillmentOptions` path gives you exact store-level inventory counts without any additional API call.

The primary risk is **endpoint instability**: this is an undocumented internal API that can change without notice. For production reliability, consider using a third-party service like SerpApi or BigBox API as a fallback, or build your monitor to detect schema changes and alert you. Run no more than a few requests per minute with randomized delays and rotated headers to stay under the bot-detection radar.