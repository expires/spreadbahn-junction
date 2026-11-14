"""Fits the two first models from exploration runs:

1. Quotes: P(win | quote / list, lane, day, hour, option) by logistic regression.
2. Fees:   P(booked within h hours | fee / typical fee, km, pickup supply, hour) as a discrete-time hazard.

    python fit.py --level 8
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import math
import os

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from bahn.paths import DATA


def load(level: int, prefix: str = "ex-"):
    road = {(r["from"], r["to"]): r for r in json.load(open(os.path.join(DATA, "map.json")))["roads"]}
    for folder in sorted(glob.glob(os.path.join(DATA, f"L{level}", f"{prefix}*"))):
        if not os.path.exists(os.path.join(folder, "reveal.json.gz")):
            continue
        get = lambda n: json.load(gzip.open(os.path.join(folder, f"{n}.json.gz")))  # noqa: E731
        yield os.path.basename(folder), road, get("reveal"), get("events"), get("snapshot"), get("roster"), get("decisions")


def quotes_frame(level: int) -> pd.DataFrame:
    rows = []
    for seed, road, rv, ev, snap, roster, log in load(level):
        drv = collections.Counter(r["homePlaceId"] for r in roster)
        for r in rv["requests"]:
            req, q = r["request"], r["quote"] or {}
            if not q:
                continue
            leg = req["legs"][0]
            p, d = leg["pickup"]["placeId"], leg["delivery"]["placeId"]
            for o in req["options"]:
                if o["kind"] not in q:
                    continue
                rows.append(dict(
                    seed=seed, rid=req["id"], dealer=req["dealerId"], opt=o["kind"], nopt=len(q),
                    ratio=q[o["kind"]] / o["listPriceCents"], lr=math.log(q[o["kind"]] / o["listPriceCents"]),
                    list=o["listPriceCents"] / 100, ref=req["referenceFeeCents"] / 100, km=road[(p, d)]["km"],
                    p=p, d=d, day=req["arrivedAt"] // 1440, hour=req["arrivedAt"] % 1440 // 60,
                    legs=len(req["legs"]), private=leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"],
                    drv_p=drv[p], drv_d=drv[d],
                    won_any=r["outcome"] == "won", took=r["option"] == o["kind"] and r["outcome"] == "won",
                ))
    return pd.DataFrame(rows)


def fees_frame(level: int) -> pd.DataFrame:
    """One row per first posting of an order at a flat exploration fee: hours until booked (or censored)."""
    rows = []
    for seed, road, rv, ev, snap, roster, log in load(level):
        drv = collections.Counter(r["homePlaceId"] for r in roster)
        ratio = {d["orderId"]: d["ratio"] for d in log if d["t"] == "fee"}
        req = {r["request"]["id"]: r["request"] for r in rv["requests"]}
        first_gig: dict[str, dict] = {}
        for g in sorted(snap["gigs"], key=lambda g: g["postedAt"]):
            first_gig.setdefault(g["stops"][0]["orderId"], g)
        booked_at = {}
        ended_at = {}
        for e in ev:
            if e["kind"] == "gig_booked":
                booked_at.setdefault(e["gigId"], e["at"])
            elif e["kind"] in ("gig_expired",):
                ended_at.setdefault(e["gigId"], e["at"])
        for oid, g in first_gig.items():
            if oid not in ratio or len(g["schedule"]) != 1:
                continue
            r = req[oid]
            leg = r["legs"][0]
            p, d = leg["pickup"]["placeId"], leg["delivery"]["placeId"]
            t0 = g["postedAt"]
            if g["id"] in booked_at:
                dur, ev_ = booked_at[g["id"]] - t0, 1
            else:
                # Censored at expiry, at a reprice (urgent steep fees), or at the end.
                dur, ev_ = ended_at.get(g["id"], snap["now"]) - t0, 0
            rows.append(dict(
                seed=seed, oid=oid, ratio=ratio[oid], km=road[(p, d)]["km"], p=p, d=d,
                drv_p=drv[p], drv_d=drv[d], hour=t0 % 1440 // 60, day=t0 // 1440,
                lead=(leg["pickup"]["window"]["from"] - t0) / 60, cars=sum(s["kind"] == "pickup" for s in g["stops"]),
                private=leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"],
                hours=max(dur, 0) / 60, booked=ev_,
            ))
    return pd.DataFrame(rows)


def fit_quotes(df: pd.DataFrame) -> None:
    print(f"\n== quotes: {len(df)} option-quotes, {df.rid.nunique()} requests, {df.seed.nunique()} worlds")
    one = df.sort_values("ratio").groupby(["seed", "rid"]).first().reset_index()  # cheapest option quoted
    one["bin"] = pd.cut(one.ratio, np.arange(0.85, 2.0, 0.1))
    print(one.groupby("bin", observed=True).won_any.agg(["size", "mean"]).round(3).to_string())
    X = pd.get_dummies(one[["lr", "km", "hour", "drv_d", "drv_p"]].assign(day=one.day.astype(str)), drop_first=True).astype(float)
    X["km"] /= 100
    m = LogisticRegression(C=10, max_iter=2000).fit(X, one.won_any)
    print("coef:", dict(zip(X.columns, m.coef_[0].round(3))), "b0", m.intercept_.round(3))
    # The ratio at which P(win) = 50% for an average request: the competitor's implied price level.
    lr_mid = -(m.intercept_[0] + X.drop(columns="lr").mean().values @ m.coef_[0][1:]) / m.coef_[0][0]
    print(f"implied median competitor price ≈ {math.exp(lr_mid):.3f} x list")
    by_seed = one.groupby("seed").apply(lambda s: _mid(s), include_groups=False)
    print("per-world 50% ratio:", by_seed.round(3).to_dict())


def _mid(s: pd.DataFrame) -> float:
    if s.won_any.nunique() < 2:
        return float("nan")
    m = LogisticRegression(C=10).fit(s[["lr"]], s.won_any)
    return math.exp(-m.intercept_[0] / m.coef_[0][0])


def fit_fees(df: pd.DataFrame) -> None:
    print(f"\n== fees: {len(df)} first postings, {df.booked.mean():.1%} booked at their flat fee")
    df["rbin"] = pd.cut(df.ratio, [0.6, 0.9, 1.1, 1.3, 1.5, 1.8, 2.3])
    df["sup"] = pd.cut(df.drv_p, [-1, 15, 40, 120, 400])
    for h in (1, 3, 8):
        df[f"in{h}h"] = (df.booked == 1) & (df.hours <= h)
    print(df.groupby("rbin", observed=True)[["in1h", "in3h", "in8h"]].mean().round(2).assign(n=df.groupby("rbin", observed=True).size()).to_string())
    print(df.groupby(["sup"], observed=True)[["in1h", "in3h", "in8h"]].mean().round(2).to_string())
    df["kmb"] = pd.cut(df.km, [-1, 100, 250, 500, 1200])
    print(df.pivot_table(index="kmb", columns="rbin", values="in3h", aggfunc="mean", observed=True).round(2).to_string())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=8)
    a = ap.parse_args()
    q, f = quotes_frame(a.level), fees_frame(a.level)
    q.to_pickle(os.path.join(DATA, f"L{a.level}-quotes.pkl"))
    f.to_pickle(os.path.join(DATA, f"L{a.level}-fees.pkl"))
    fit_quotes(q)
    fit_fees(f)


if __name__ == "__main__":
    main()
