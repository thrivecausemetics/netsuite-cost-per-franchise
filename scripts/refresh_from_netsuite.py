#!/usr/bin/env python3
"""Refresh the Assembly Item Cost & Inventory Audit dashboard from NetSuite.

The dashboard (index.html) embeds a single JSON snapshot (`const D = {...}`)
of per-location assembly item cost and inventory. This script regenerates
that snapshot from live NetSuite and replaces ONLY the marked data block,
leaving the rest of the page untouched.

Cost model (validated against live NetSuite and the original embed):
  - Population: item.itemtype = 'Assembly', isinactive = 'F', parent IS NOT NULL.
    Families are the (top-level) parent assembly items; hierarchy is flat.
  - Source of truth: aggregateItemLocation per item x location for the four
    sellable locations. cost = averagecostmli (NetSuite's own per-location
    moving average, includes landed cost), quantities = quantityonhand /
    quantityavailable / quantitycommitted (missing row or NULL -> 0; cost NULL
    stays null, cost 0 stays 0).
  - This is a POINT-IN-TIME snapshot, not a flow metric: there is no
    transaction window. Every run is a full rebuild from current NetSuite
    state, so the refresh can never degrade or need backfill seeding.
  - Stats, computed within family x location across its SKUs' costs:
      avg_cost    = round(mean, 4)
      cost_stdev  = round(sample stdev, 4), only when >= 3 costed SKUs, else null
      z_score     = round(abs(cost - avg) / stdev, 2), null when stdev null/zero
      is_outlier  = z_score >= 2.0
    SKU-level: outlier_locs = flagged locations in display order;
    is_outlier = any location flagged. z/outlier keys omitted when cost null.
  - Families sorted by name, SKUs by code. Integral cost/avg/qty values
    serialize as ints (matches the original generator byte-for-byte).

Safety: dry-run by default — writes index.sample.html and never touches
index.html unless --publish is passed. All queries are read-only SuiteQL.
"""

import argparse
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "index.html"
SAMPLE_OUT = REPO_ROOT / "index.sample.html"

# Display order is load-bearing: the page's columns and outlier_locs follow it.
LOCATION_NAMES = ["MS Sellable", "CA Sellable", "NV Sellable", "Amazon FBA US"]

Z_OUTLIER_THRESHOLD = 2.0
MIN_COSTED_SKUS_FOR_STATS = 3

DATA_START = "/* DATA:START"
DATA_END = "/* DATA:END */"

ITEM_FILTER = "itemtype = 'Assembly' AND isinactive = 'F' AND parent IS NOT NULL"


# ---------------------------------------------------------------- fetch

def fetch_all(client):
    """Pull locations, assembly SKUs, their parents, and per-location
    aggregate inventory/cost rows. Chunked per location; no server-side
    ORDER BY (sorting happens in Python)."""
    quoted = ", ".join(f"'{name}'" for name in LOCATION_NAMES)
    loc_rows = client.suiteql(
        f"SELECT id, name FROM location WHERE name IN ({quoted})"
    )
    locations = {row["name"]: int(row["id"]) for row in loc_rows}
    missing = [name for name in LOCATION_NAMES if name not in locations]
    if missing:
        raise SystemExit(f"FATAL: locations not found in NetSuite: {missing}")

    items = client.suiteql(
        f"SELECT id, itemid, fullname, parent FROM item WHERE {ITEM_FILTER}"
    )
    if not items:
        raise SystemExit("FATAL: assembly item query returned no rows")

    parent_ids = sorted({int(row["parent"]) for row in items})
    parents = []
    for i in range(0, len(parent_ids), 500):
        chunk = ", ".join(str(pid) for pid in parent_ids[i : i + 500])
        parents.extend(
            client.suiteql(f"SELECT id, itemid FROM item WHERE id IN ({chunk})")
        )

    agg_by_loc = {}
    for name in LOCATION_NAMES:
        agg_by_loc[name] = client.suiteql(
            "SELECT item, quantityonhand, quantityavailable, quantitycommitted,"
            " averagecostmli FROM aggregateItemLocation"
            f" WHERE location = {locations[name]}"
            f" AND item IN (SELECT id FROM item WHERE {ITEM_FILTER})"
        )
    return locations, items, parents, agg_by_loc


# ---------------------------------------------------------------- build

def _num(value):
    """SuiteQL over REST returns numbers as strings; integral values must
    serialize as ints to match the original embed."""
    if value is None:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def build_data(locations, items, parents, agg_by_loc, as_of):
    # The original embed orders each SKU's locations dict by NetSuite
    # location id (summaries use display order); preserved for clean diffs.
    id_order = sorted(LOCATION_NAMES, key=lambda name: locations[name])
    parent_names = {int(row["id"]): row["itemid"] for row in parents}

    per_loc = {}
    for loc_name, rows in agg_by_loc.items():
        per_loc[loc_name] = {int(row["item"]): row for row in rows}

    families = {}
    for row in items:
        item_id = int(row["id"])
        parent_id = int(row["parent"])
        family_name = parent_names.get(parent_id)
        if family_name is None:
            raise SystemExit(f"FATAL: parent {parent_id} of item {item_id} not found")
        sku_locations = {}
        for loc_name in id_order:
            agg = per_loc[loc_name].get(item_id)
            cost = _num(agg.get("averagecostmli")) if agg else None
            entry = {
                "cost": cost,
                "on_hand": _num(agg.get("quantityonhand")) or 0 if agg else 0,
                "available": _num(agg.get("quantityavailable")) or 0 if agg else 0,
                "committed": _num(agg.get("quantitycommitted")) or 0 if agg else 0,
            }
            if cost is not None:
                entry["z_score"] = None
                entry["is_outlier"] = False
            sku_locations[loc_name] = entry
        sku = {
            "item_id": item_id,
            "sku": row["itemid"],
            "full_name": row["fullname"],
            "parent_id": parent_id,
            "family": family_name,
            "locations": sku_locations,
            "outlier_locs": [],
            "is_outlier": False,
        }
        families.setdefault(family_name, []).append(sku)

    out_families = []
    for family_name in sorted(families):
        skus = sorted(families[family_name], key=lambda s: s["sku"])
        summary = {}
        for loc_name in LOCATION_NAMES:
            entries = [s["locations"][loc_name] for s in skus]
            costs = [e["cost"] for e in entries if e["cost"] is not None]
            avg = sum(costs) / len(costs) if costs else None
            stdev = (
                statistics.stdev(costs)
                if len(costs) >= MIN_COSTED_SKUS_FOR_STATS
                else None
            )
            if stdev:
                for entry in entries:
                    if entry["cost"] is None:
                        continue
                    z = round(abs(entry["cost"] - avg) / stdev, 2)
                    entry["z_score"] = z
                    entry["is_outlier"] = z >= Z_OUTLIER_THRESHOLD
            summary[loc_name] = {
                "avg_cost": _num(round(avg, 4)) if avg is not None else None,
                "cost_stdev": round(stdev, 4) if stdev is not None else None,
                "total_on_hand": sum(e["on_hand"] for e in entries),
                "total_available": sum(e["available"] for e in entries),
                "total_committed": sum(e["committed"] for e in entries),
            }
        for sku in skus:
            flagged = [
                loc_name
                for loc_name in LOCATION_NAMES
                if sku["locations"][loc_name].get("is_outlier")
            ]
            sku["outlier_locs"] = flagged
            sku["is_outlier"] = bool(flagged)
        out_families.append(
            {"name": family_name, "skus": skus, "summary": summary}
        )

    return {"as_of": as_of, "locations": LOCATION_NAMES, "families": out_families}


# ------------------------------------------------------------- validate

def validate(data, previous):
    """Guardrails before anything is written. Returns a list of problems."""
    problems = []
    skus = [s for f in data["families"] for s in f["skus"]]
    if data["locations"] != LOCATION_NAMES:
        problems.append(f"locations changed: {data['locations']}")
    negative = [
        (s["sku"], loc, e["cost"])
        for s in skus
        for loc, e in s["locations"].items()
        if e["cost"] is not None and e["cost"] < 0
    ]
    if negative:
        problems.append(f"negative costs: {negative[:10]}")
    prev_skus = sum(len(f["skus"]) for f in previous["families"])
    if len(skus) < prev_skus * 0.5:
        problems.append(
            f"SKU count collapsed: {len(skus)} vs previous {prev_skus}"
        )
    for sku in skus:
        for entry in sku["locations"].values():
            costed = entry["cost"] is not None
            if costed != ("z_score" in entry) or costed != ("is_outlier" in entry):
                problems.append(f"schema drift on {sku['sku']}")
                break
    return problems


# ---------------------------------------------------------------- embed

def embed(html, data):
    start = html.find(DATA_START)
    end = html.find(DATA_END)
    if start == -1 or end == -1 or end <= start:
        raise SystemExit("FATAL: DATA:START / DATA:END markers not found in index.html")
    start = html.index("\n", start) + 1  # keep the START marker line itself
    block = f"const D = {json.dumps(data)};\n"
    html = html[:start] + block + html[end:]
    html, n = re.subn(
        r"Data as of \d{4}-\d{2}-\d{2}", f"Data as of {data['as_of']}", html
    )
    if n != 1:
        raise SystemExit(f"FATAL: expected exactly one 'Data as of' header, found {n}")
    return html


def summarize(data, previous):
    skus = [s for f in data["families"] for s in f["skus"]]
    outliers = [s for s in skus if s["is_outlier"]]
    prev_skus = sum(len(f["skus"]) for f in previous["families"])
    top = sorted(
        (
            (e["z_score"], s["sku"], loc)
            for s in skus
            for loc, e in s["locations"].items()
            if e.get("z_score") is not None
        ),
        reverse=True,
    )[:5]
    lines = [
        f"as_of {data['as_of']}: {len(data['families'])} families, {len(skus)} SKUs"
        f" (previous embed: {prev_skus}), {len(outliers)} outlier SKUs",
        "top z-scores: " + ", ".join(f"{sku}@{loc} z={z}" for z, sku, loc in top),
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------- main

def load_embedded(html):
    match = re.search(r"const D = (\{.*?\});\n", html, re.S)
    if not match:
        raise SystemExit("FATAL: could not parse existing embedded data")
    return json.loads(match.group(1))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--publish",
        action="store_true",
        help="write index.html in place (default: dry-run to index.sample.html)",
    )
    parser.add_argument(
        "--fixtures",
        metavar="DIR",
        help="dev/debug: build from previously fetched JSON (items.json,"
        " parents.json, agg_by_loc.json) instead of querying NetSuite",
    )
    args = parser.parse_args()

    html = DASHBOARD.read_text()
    previous = load_embedded(html)

    if args.fixtures:
        fixtures = Path(args.fixtures)
        locations = json.loads((fixtures / "locations.json").read_text())
        items = json.loads((fixtures / "items.json").read_text())
        parents = json.loads((fixtures / "parents.json").read_text())
        agg_by_loc = json.loads((fixtures / "agg_by_loc.json").read_text())
    else:
        import os

        from netsuite_client import NetSuiteClient

        env = {}
        for key in (
            "NS_ACCOUNT_ID",
            "NS_CONSUMER_KEY",
            "NS_CONSUMER_SECRET",
            "NS_TOKEN_ID",
            "NS_TOKEN_SECRET",
        ):
            value = os.environ.get(key)
            if not value:
                raise SystemExit(f"FATAL: missing environment variable {key}")
            env[key] = value
        client = NetSuiteClient(
            env["NS_ACCOUNT_ID"],
            env["NS_CONSUMER_KEY"],
            env["NS_CONSUMER_SECRET"],
            env["NS_TOKEN_ID"],
            env["NS_TOKEN_SECRET"],
        )
        locations, items, parents, agg_by_loc = fetch_all(client)

    as_of = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    data = build_data(locations, items, parents, agg_by_loc, as_of)

    problems = validate(data, previous)
    if problems:
        print("VALIDATION FAILED — nothing written:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(1)

    output = embed(html, data)
    target = DASHBOARD if args.publish else SAMPLE_OUT
    target.write_text(output)
    print(f"wrote {target.name} ({'LIVE' if args.publish else 'dry-run'})")
    print(summarize(data, previous))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
