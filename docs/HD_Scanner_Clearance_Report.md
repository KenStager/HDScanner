# HD Price Scanner: In-Store Clearance Detection

**Date:** March 19, 2026
**Trigger:** FORGE 12Ah Starter Kit (48-59-1813GB) found on clearance at $150 (50% off) at the primary store — system missed it entirely.

---

## Root Cause

HD's GraphQL API has a `pricing.clearance{value dollarOff percentageOff}` sub-object that returns store-specific in-store clearance pricing. Our `searchModel` query never requested it. The online price ($299) and all promotion metadata showed nothing — the clearance data was one field away the entire time.

A secondary bug compounded the miss: duplicate snapshots (items appearing on multiple paginated pages) created identical rows per run, causing the diff engine to compare two copies of the same data and detect no change.

## Fix

- Added `clearance{value dollarOff percentageOff}` to the GraphQL query
- New `IN_STORE_CLEARANCE` alert type fires when clearance pricing appears or deepens
- Fixed duplicate snapshot insertion with per-store dedup set
- Fixed diff engine to deduplicate by timestamp before comparing
- Fixed inventory parser to prefer express delivery when BOPIS is OOS (clearance items can't be bought online, so BOPIS falsely reports zero stock)

241 tests pass, zero regressions.

## First Pipeline Results

14 in-store clearance deals detected at the primary store on first run (partial — API rate-limited to 1 page of snapshots):

| Item | Model | Online | Clearance | Off | Stock |
|------|-------|--------|-----------|-----|-------|
| FORGE 12Ah Starter Kit | 48-59-1813GB | $299 | $150 | 50% | 4 in stock |
| M18 FUEL Deep Cut Band Saw | 2929-20 | $399 | $200 | 50% | 4 in stock |
| M18 5-Tool Combo Kit | 2694-25 | $299 | $269 | 55% | 1 in stock |
| M18 5-Tool Combo Kit | 2695-25CX | $299 | $150 | 75% | — |
| M12 5-Tool Combo Kit | 2498-25H | $399 | $100 | 75% | — |
| Battery + PACKOUT Charger Pack | 48-59-1865POC | $499 | $250 | 72% | — |
| M18 6-Tool Combo Kit | 2696-26 | $799 | $303 | 62% | OOS |
| M18 Planer | 2623-20 | $249 | $81 | 67% | 1 in stock |
| M18 FUEL SURGE Impact | 2760-20 | $199 | $100 | 50% | OOS |
| M18 Jobsite Radio/Charger | 2792-20 | $279 | $140 | 50% | OOS |
| M12 FUEL Ratchet | 2557-20 | $199 | $90 | 55% | OOS |
| M12 FUEL 4-in-1 Install Kit | 2505-22 | $229 | $100 | 56% | OOS |
| M12 Polisher/Sander | 2438-20 | $199 | $89 | 55% | OOS |
| SHOCKWAVE Socket Set | 49-66-7021 | $54.97 | $12 | 78% | 2 in stock |

## Caveat: Store Specificity

Cross-store probing of the Planer (2623-20) confirmed the `clearance` field is store-specific:

- Primary store: clearance $81 (67% off)
- Secondary store: no clearance
- Chicopee (2610): no clearance

However, the website didn't show a clearance badge for Greenfield on this item despite the API returning one. The clearance may reflect regional pricing that doesn't always correspond to a physical unit on the clearance shelf. Alerts should be treated as strong leads worth verifying in person, not guaranteed finds.

## Impact

This was a complete blind spot. Zero clearance events were ever detected in the system's history — the `savings_center = 'CLEARANCE'` value never appeared once across all snapshots because HD doesn't use that field for in-store clearance. The `clearance` sub-object is the sole source, and we now capture it.
