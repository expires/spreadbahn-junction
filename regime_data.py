"""Loads saved level-8 reveals into one quote-outcome table (offline research for regime.py).

One row per request: our cheapest quoted log ratio (quote / list), whether we quoted the dealer's
fastest option, the outcome, the lane offset, and the dealer-trust count we had earned at decision time.
"""
from __future__ import annotations

import glob
import gzip
import json
import math
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SPREAD_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(ROOT))), "data"))
if not os.path.isdir(DATA):
    DATA = os.path.join(ROOT, "data")
DAY = 1440


def places() -> dict:
    return {p["id"]: (p["lat"], p["lon"]) for p in json.load(open(os.path.join(DATA, "map.json")))["places"]}


def haversine(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def regions(radius_km: float = 80.0) -> dict:
    """center town -> set of places within radius_km straight line (the rules' outage region)."""
    pl = places()
    return {c: {p for p in pl if haversine(pl[c], pl[p]) <= radius_km} for c in pl}


def seed_of(world: str) -> str:
    """'v6-ev-1' -> 'ev-1'; 'ex-3' -> 'ex-3'; 'starter-spread-1' -> 'spread-1'."""
    parts = world.split("-")
    return "-".join(parts[-2:])


def load_world(folder: str, lanes: dict | None) -> pd.DataFrame:
    world = os.path.basename(folder)
    rv = json.load(gzip.open(os.path.join(folder, "reveal.json.gz")))
    # Dealer trust as the rules count it: +1 per order on time (max 25), a late one halves the count.
    deliveries = []
    reqs = {r["request"]["id"]: r for r in rv["requests"]}
    for o in rv["orders"]:
        r = reqs.get(o["orderId"])
        if r is None or r["outcome"] != "won":
            continue
        dl = {x["kind"]: x["deadline"] for x in r["request"]["options"]}.get(r["option"])
        ts = [l.get("deliveredAt") for l in o["legs"]]
        if dl is None or any(t is None for t in ts):
            continue
        nd = [l.get("noteDeadline") for l in o["legs"] if l.get("noteDeadline")]
        dl = min([dl] + nd)
        deliveries.append((max(ts), r["request"]["dealerId"], max(ts) <= dl))
    deliveries.sort()
    rows = []
    trust: dict[str, int] = {}
    k = 0
    for r in sorted(rv["requests"], key=lambda r: r["request"]["respondBy"]):
        req, q = r["request"], r["quote"] or {}
        while k < len(deliveries) and deliveries[k][0] <= req["arrivedAt"]:
            _, d, ok = deliveries[k]
            trust[d] = min(25, trust.get(d, 0) + 1) if ok else trust.get(d, 0) // 2
            k += 1
        leg = req["legs"][0]
        p, d = leg["pickup"]["placeId"], leg["delivery"]["placeId"]
        lrs = [math.log(q[o["kind"]] / o["listPriceCents"]) for o in req["options"]
               if o["kind"] in q and q[o["kind"]] < 3 * o["listPriceCents"]]
        fast = req["options"][0]
        lane = 0.0
        if lanes:
            lane = lanes["pickup"].get(p, 0.0) + lanes["delivery"].get(d, 0.0)
        rows.append(dict(
            world=world, seed=seed_of(world), explore=world.startswith("ex-"), rid=req["id"], dealer=req["dealerId"],
            batch=req.get("batchId"),
            arrived=req["arrivedAt"], respond=req["respondBy"], day=req["arrivedAt"] // DAY,
            minute=req["arrivedAt"] % DAY, pickup=p, delivery=d, nlegs=len(req["legs"]), nopt=len(req["options"]),
            list_fast=fast["listPriceCents"] / 100, ref=req["referenceFeeCents"] / 100,
            quoted=bool(lrs), fast_quoted=fast["kind"] in q and q[fast["kind"]] < 3 * fast["listPriceCents"],
            lr_min=min(lrs) if lrs else np.nan,
            lr_fast=math.log(q[fast["kind"]] / fast["listPriceCents"]) if fast["kind"] in q else np.nan,
            won=r["outcome"] == "won", option=r["option"], trust=trust.get(req["dealerId"], 0), lane=lane,
            price_won=(q.get(r["option"], 0) / 100) if r["outcome"] == "won" else 0.0,
            list_won=({o["kind"]: o["listPriceCents"] for o in req["options"]}.get(r["option"], 0) / 100) if r["outcome"] == "won" else 0.0,
        ))
    df = pd.DataFrame(rows)
    size = df.groupby("batch").rid.transform("size")
    df["bulk"] = df.batch.notna() & (size >= 25)
    return df


def load_all(level: int = 8, pattern: str = "*") -> pd.DataFrame:
    lp = os.path.join(ROOT, "lane_effects.json")
    lanes = json.load(open(lp)) if os.path.exists(lp) else None
    frames = [load_world(f, lanes) for f in sorted(glob.glob(os.path.join(DATA, f"L{level}", pattern)))
              if os.path.exists(os.path.join(f, "reveal.json.gz"))]
    return pd.concat(frames, ignore_index=True)


if __name__ == "__main__":
    df = load_all()
    print(df.groupby("world").agg(n=("rid", "size"), quoted=("quoted", "mean"), fast=("fast_quoted", "mean"),
                                  won=("won", "mean"), lr=("lr_min", "median"), trust=("trust", "mean")).round(3))
