"""Realised-margin attribution per won order, from saved practice weeks (data/L8/<tag>-*/: reveal, events, snapshot,
roster, decisions). The server's ledger sums exactly to the score's margin; every line is put on an order:

    revenue, late refunds, undelivered penalties     the order on the line
    driver fees, waiting, release fees, extra drive  the gig's orders, split by typical fee (referenceFee per car)

    python -m bahn.analysis.attribution attr          # tag prefix of the saved weeks

Findings (current defaults, 8 weeks bt-1..6, ho-3, st-25; EUR 14,256/wk, 391 orders/wk, EUR 36.4 per order):
  * No segment loses money on average. Thinnest: last-minute singles (8-16 h slack at the quote, ~4/wk, EUR 4/order:
    realised fee 1.74x typical against 1.35x modelled), short lanes < 100 km (EUR 19/order), quotes < 1.2x list
    (EUR 17). The loss is in single orders: late ones (9/wk, -EUR 22 each, -EUR 203/wk), cancelled/undelivered.
  * The cost model is conservative: modelled fee 1.05x typical vs 0.94x realised (pairs realise 0.80x),
    P(late) 6.4% modelled vs 2.2% realised. Notes: realised fee 1.03x with a note vs 1.04x without (singles), but
    the quote adds 15% for a note, and we win 40% of quoted note requests against 54% without.
  * note_fee=1.0: +EUR 356/wk, t=3.56 on 44 weeks (+25 orders/wk, mean on-time -0.3 pt, worst 91.6% vs 91.4%).
"""
from __future__ import annotations

import glob
import gzip
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from bahn.paths import DATA

DAY = 1440


def _load(folder: str, name: str):
    return json.load(gzip.open(os.path.join(folder, f"{name}.json.gz")))


def supply_index(roster: list, minutes: dict, places: list) -> dict:
    homes: dict = {}
    for d in roster:
        homes[d["homePlaceId"]] = homes.get(d["homePlaceId"], 0) + 1
    return {p: sum(n * math.exp(-minutes[(h, p)] / 60) for h, n in homes.items()) for p in places}


def week(folder: str, road: dict) -> pd.DataFrame:
    rv, ev, snap, roster = (_load(folder, n) for n in ("reveal", "events", "snapshot", "roster"))
    dec = {}
    for d in _load(folder, "decisions"):
        dec[(d["rid"], d["opt"])] = d
    places = sorted({a for a, _ in road})
    sup = supply_index(roster, {k: v["minutes"] for k, v in road.items()}, places)
    req = {r["request"]["id"]: r for r in rv["requests"]}
    orders = {o["id"]: o for o in snap["orders"]}
    # typical fee per car of each order, and the orders in each gig (cars counted per stop pair)
    ref_car = {oid: req[oid]["request"]["referenceFeeCents"] / len(req[oid]["request"]["legs"]) for oid in orders}
    gig_orders: dict = {}
    for g in snap["gigs"]:
        w: dict = {}
        for s in g["stops"]:
            if s["kind"] == "pickup":
                w[s["orderId"]] = w.get(s["orderId"], 0.0) + ref_car.get(s["orderId"], 0.0)
        gig_orders[g["id"]] = w
    acc = {oid: dict(revenue=0.0, fee=0.0, refund=0.0, penalty=0.0, other=0.0) for oid in orders}
    unalloc = 0.0
    for e in ev:
        if e["kind"] != "ledger":
            continue
        l, c = e["line"], e["line"]["cents"] / 100
        col = {"revenue": "revenue", "driver_fee": "fee", "late_refund": "refund",
               "undelivered_penalty": "penalty"}.get(l["kind"], "other")
        if l["gigId"] is not None and gig_orders.get(l["gigId"]):
            w = gig_orders[l["gigId"]]
            tot = sum(w.values()) or 1.0
            for oid, x in w.items():
                acc.setdefault(oid, dict(revenue=0.0, fee=0.0, refund=0.0, penalty=0.0, other=0.0))[col] += c * x / tot
        elif l["orderId"] in acc:
            acc[l["orderId"]][col] += c
        else:
            unalloc += c
    gig_n = {}
    for g, w in gig_orders.items():
        for oid in w:
            gig_n[oid] = max(gig_n.get(oid, 1), len(w))
    rows = []
    world = os.path.basename(folder)
    for oid, o in orders.items():
        r = req[oid]["request"]
        legs = r["legs"]
        km = sum(road[(l["pickup"]["placeId"], l["delivery"]["placeId"])]["km"] for l in legs)
        lst = {x["kind"]: x["listPriceCents"] / 100 for x in r["options"]}[o["option"]]
        d = dec.get((oid, o["option"]), {})
        late = o["status"] != "cancelled" and (any(t is None for t in o["deliveredAt"]) or
                                               any(t is not None and t > o["deadline"] for t in o["deliveredAt"]))
        a = acc[oid]
        rows.append(dict(
            world=world, oid=oid, status=o["status"], option=o["option"], price=o["priceCents"] / 100, list=lst,
            x=o["priceCents"] / 100 / lst, ref=r["referenceFeeCents"] / 100, km=km, nlegs=len(legs),
            private=any(l["pickup"]["privateAddress"] or l["delivery"]["privateAddress"] for l in legs),
            premium=any(l["car"]["premium"] for l in legs), electric=any(l["car"]["electric"] for l in legs),
            note=r["note"] is not None, pickup=legs[0]["pickup"]["placeId"], delivery=legs[0]["delivery"]["placeId"],
            sup_p=sup[legs[0]["pickup"]["placeId"]], sup_d=sup[legs[0]["delivery"]["placeId"]],
            arrive_day=r["arrivedAt"] // DAY, hour=r["arrivedAt"] % DAY // 60, dead_day=o["deadline"] // DAY,
            dead_dow=o["deadline"] // DAY % 7, paired=gig_n.get(oid, 1) > 1, late=late,
            exp_fee=d.get("fee"), exp_pl=d.get("pl"), slack=d.get("slack"),
            **a, net=sum(a.values())))
    df = pd.DataFrame(rows)
    df.attrs["unallocated"] = unalloc
    df.attrs["margin"] = snap["score"]["marginCents"] / 100
    return df


def load(tag: str) -> pd.DataFrame:
    wmap = json.load(open(os.path.join(DATA, "map.json")))
    road = {(x["from"], x["to"]): x for x in wmap["roads"]}
    parts = []
    for f in sorted(glob.glob(os.path.join(DATA, "L8", f"{tag}-*"))):
        w = week(f, road)
        chk = w.net.sum() + w.attrs["unallocated"]
        assert abs(chk - w.attrs["margin"]) < 1.0, (f, chk, w.attrs["margin"])
        parts.append(w)
    df = pd.concat(parts, ignore_index=True)
    df["km_band"] = pd.cut(df.km, [0, 100, 200, 300, 400, 1e4], labels=["<100", "100-200", "200-300", "300-400", "400+"])
    df["sup_p_band"] = pd.qcut(df.sup_p, 4, labels=["p-supply Q1 (low)", "Q2", "Q3", "Q4 (high)"])
    df["sup_d_band"] = pd.qcut(df.sup_d, 4, labels=["d-supply Q1 (low)", "Q2", "Q3", "Q4 (high)"])
    df["hour_band"] = pd.cut(df.hour, [-1, 8, 11, 14, 17, 24], labels=["<09", "09-11", "12-14", "15-17", "18+"])
    df["x_band"] = pd.cut(df.x, [0, 1.2, 1.3, 1.4, 1.5, 9], labels=["<1.2", "1.2-1.3", "1.3-1.4", "1.4-1.5", "1.5+"])
    df["fee_ratio"] = -df.fee / df.ref
    return df


SEGMENTS = ["option", "km_band", "private", "nlegs", "paired", "premium", "electric", "note", "sup_p_band",
            "sup_d_band", "dead_dow", "hour_band", "x_band", "late"]


def table(df: pd.DataFrame, weeks: int) -> pd.DataFrame:
    out = []
    for s in SEGMENTS:
        for v, g in df.groupby(s, observed=True):
            out.append(dict(segment=s, value=str(v), orders_wk=len(g) / weeks, net_per_order=g.net.mean(),
                             net_wk=g.net.sum() / weeks, revenue=g.revenue.mean(), fee=g.fee.mean(),
                             refund=(g.refund + g.penalty).mean(), other=g.other.mean(), late=g.late.mean(),
                             fee_ratio=g.fee_ratio.mean(), exp_fee_ratio=(g.exp_fee / g.ref).mean(),
                             share_neg=(g.net < 0).mean()))
    return pd.DataFrame(out)


def main(argv):
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)
    df = load(argv[1] if len(argv) > 1 else "attr")
    weeks = df.world.nunique()
    print(f"{weeks} weeks, {len(df)} orders, margin €{df.net.sum() / weeks:,.0f}/week, net/order €{df.net.mean():.1f}, "
          f"orders with net<0: {(df.net < 0).mean():.1%}")
    print(table(df, weeks).round(2).to_string(index=False))
    return df


if __name__ == "__main__":
    main(sys.argv)
