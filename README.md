# Assembly Item Cost & Inventory Audit ("cost per franchise")

Single-file dashboard (`index.html`) comparing per-location assembly item
cost across Thrive Causemetics' four sellable locations — MS Sellable,
CA Sellable, NV Sellable, Amazon FBA US — with cross-SKU z-score outlier
flagging. Data is embedded directly in the HTML between
`/* DATA:START */` and `/* DATA:END */` markers and refreshed automatically.

## Cost model

- **Population**: active assembly SKUs (`item.itemtype = 'Assembly'`,
  `isinactive = 'F'`, `parent IS NOT NULL`), grouped into families by their
  parent assembly item (hierarchy is flat: family → SKU).
- **Source**: `aggregateItemLocation` per item × location.
  `cost` = `averagecostmli` (NetSuite's per-location moving-average cost,
  includes landed cost); quantities = `quantityonhand` / `quantityavailable`
  / `quantitycommitted`.
- **Snapshot, not a flow metric**: the page shows current state as of the
  run date. Every refresh is a full rebuild from live NetSuite, so there is
  no rolling window, no opening-balance seeding, and nothing to backfill.
- **Stats** (within family × location, across SKU costs): mean (4 dp),
  sample stdev (4 dp, requires ≥ 3 costed SKUs), z = |cost − mean| / stdev
  (2 dp), outlier when z ≥ 2.

These formulas were reverse-engineered from the original embed and verified
with zero mismatches across all 859 SKUs × 4 locations, then cross-checked
against live NetSuite records.

## Refresh pipeline

- `scripts/netsuite_client.py` — read-only SuiteQL client (OAuth1 TBA,
  HMAC-SHA256, 240 s timeout, retry with backoff, limit/offset pagination).
- `scripts/refresh_from_netsuite.py` — fetches, rebuilds the snapshot,
  validates (no negative costs, no schema drift, SKU count sanity vs the
  previous embed), and replaces the marked data block plus the
  "Data as of …" header. **Dry-run by default**: writes `index.sample.html`;
  only `--publish` touches `index.html`.
- `.github/workflows/refresh-data.yml` — daily at 15:07 UTC (8:07am PDT /
  7:07am PST) publishing live; manual `workflow_dispatch` defaults to a
  dry-run that uploads `index.sample.html` as an artifact, with a
  `publish_live` input to override.

## Required repository secrets (Token-Based Auth, read-only role)

| Secret | Meaning |
|---|---|
| `NS_ACCOUNT_ID` | NetSuite account ID |
| `NS_CONSUMER_KEY` / `NS_CONSUMER_SECRET` | Integration record keys |
| `NS_TOKEN_ID` / `NS_TOKEN_SECRET` | Access token for the integration user |

## Local run

```
pip install -r requirements.txt
NS_ACCOUNT_ID=... NS_CONSUMER_KEY=... NS_CONSUMER_SECRET=... \
NS_TOKEN_ID=... NS_TOKEN_SECRET=... \
python scripts/refresh_from_netsuite.py        # dry-run -> index.sample.html
python scripts/refresh_from_netsuite.py --publish   # updates index.html
```

> Methodology note: cost figures are production financial data. Changes to
> the cost model in this repo should be reviewed by Finance.
