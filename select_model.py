"""Expected net per won order from quote-time features, fitted on the whole L8 archive.

The missing lever in the leaderboard gap is SELECTION: the leaders win ~300 orders at ~EUR45
net/order while v8 wins ~400 at ~EUR34. The hand cost model in dispatch.py cannot rank requests
well enough for that, so this fits the realised net per won order (ledger sum) on features a
dispatcher knows when it quotes: the persistent competitor lane factor, distance, reference fee
and list price, supply at both ends relative to the world's mean, slack, option kind, private
addresses, note, car flags, hour/day and leg count.

    python select_model.py fit     # writes data/fits/select.pkl + prints world-grouped CV
    from select_model import SelectModel
    model = SelectModel.load()
    model.net(request, option, road, supply, mean_supply)  # expected net EUR for that option
"""
from __future__ import annotations

import glob
import gzip
import json
import math
import os
import pickle
import statistics

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "fits", "select.pkl")
DAY, HOUR = 1440, 60

FEATURES = [
    "lane_factor", "log_km", "log_mins", "log_ref", "log_list", "ref_over_list",
    "log_sp", "log_sd", "log_smin", "rel_sp", "rel_sd", "rel_smin", "log_drivers",
    "slack_h", "lead_h", "opt_same", "opt_next", "opt_within", "n_legs", "private_any",
    "note", "premium", "electric", "hour", "day", "is_weekend",
]


def _map():
    path = os.path.join(DATA, "fits", "map.json")
    if not os.path.exists(path):
        path = os.path.join(DATA, "map.json")
    return json.load(open(path))


def _road():
    roads = {(r["from"], r["to"]): r for r in _map()["roads"]}

    class Road:
        def minutes(self, a, b):
            return roads[(a, b)]["minutes"]

        def km(self, a, b):
            return roads[(a, b)]["km"]

    return Road()


def features_for(request: dict, option: dict, road, supply: dict, mean_supply: float,
                 lane_factor: float) -> dict:
    legs = request["legs"]
    km = sum(road.km(leg["pickup"]["placeId"], leg["delivery"]["placeId"]) for leg in legs)
    mins = sum(road.minutes(leg["pickup"]["placeId"], leg["delivery"]["placeId"]) for leg in legs)
    p0 = legs[0]["pickup"]["placeId"]
    d0 = legs[-1]["delivery"]["placeId"]
    sp = supply.get(p0, 0.0)
    sd = supply.get(d0, 0.0)
    mean = mean_supply or 1.0
    premium = 1.0 if any(leg["car"].get("premium") for leg in legs) else 0.0
    electric = 1.0 if any(leg["car"].get("electric") for leg in legs) else 0.0
    private = 1.0 if any(leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"] for leg in legs) else 0.0
    at = request["arrivedAt"]
    return {
        "lane_factor": lane_factor,
        "log_km": math.log1p(km), "log_mins": math.log1p(mins),
        "log_ref": math.log1p(request["referenceFeeCents"] / 100.0),
        "log_list": math.log1p(option["listPriceCents"] / 100.0),
        "ref_over_list": math.log(max(1e-6, request["referenceFeeCents"] / max(1, option["listPriceCents"]))),
        "log_sp": math.log1p(sp), "log_sd": math.log1p(sd), "log_smin": math.log1p(min(sp, sd)),
        "rel_sp": sp / mean, "rel_sd": sd / mean, "rel_smin": min(sp, sd) / mean,
        "log_drivers": math.log1p(len(supply)),
        "slack_h": (option["deadline"] - request["respondBy"]) / 60.0,
        "lead_h": (option["deadline"] - mins - 30 - request["respondBy"]) / 60.0,
        "opt_same": 1.0 if option["kind"] == "same_day" else 0.0,
        "opt_next": 1.0 if option["kind"] == "next_day" else 0.0,
        "opt_within": 1.0 if option["kind"] == "within_3_days" else 0.0,
        "n_legs": len(legs), "private_any": private, "note": 1.0 if request.get("note") else 0.0,
        "premium": premium, "electric": electric,
        "hour": (at % DAY) / 60.0, "day": at // DAY, "is_weekend": 1.0 if at // DAY >= 5 else 0.0,
    }


def dataset(min_worlds: int | None = None):
    import supply
    from quote_model import QuoteModel
    qm = QuoteModel.load()
    road = _road()
    rows = []
    worlds = []
    folders = sorted(glob.glob(os.path.join(DATA, "L8", "w*")))
    for folder in folders:
        world = os.path.basename(folder)
        try:
            reveal = json.load(gzip.open(os.path.join(folder, "reveal.json.gz")))
            events = json.load(gzip.open(os.path.join(folder, "events.json.gz")))
            roster = json.load(gzip.open(os.path.join(folder, "roster.json.gz")))
        except FileNotFoundError:
            continue
        structure = supply.build_supply(roster, road)
        kernels = {place: (value.get("kernel", 0.0) if isinstance(value, dict) else value)
                   for place, value in structure.items()}
        mean = sum(kernels.values()) / max(1, len(kernels)) if kernels else 1.0
        money = {}
        for event in events:
            if event["kind"] != "ledger":
                continue
            line = event["line"]
            oid = line.get("orderId")
            if oid is None:
                continue
            if line["kind"] in ("driver_fee", "revenue", "late_refund", "undelivered_penalty",
                                "failed_visit", "release_fee", "waiting", "fine"):
                money[oid] = money.get(oid, 0.0) + line["cents"] / 100.0
        requests = {r["request"]["id"]: r for r in reveal["requests"]}
        for record in reveal["requests"]:
            oid = record["request"]["id"]
            if record["outcome"] != "won" or not record["option"] or oid not in money:
                continue
            request = record["request"]
            option = next((o for o in request["options"] if o["kind"] == record["option"]), None)
            if option is None:
                continue
            lane = qm.lane_log_factor(request["legs"][0]["pickup"]["placeId"],
                                      request["legs"][0]["delivery"]["placeId"])
            rows.append(features_for(request, option, road, kernels, mean, lane) | {"net": money[oid]})
            worlds.append(world)
    return rows, worlds


def fit() -> None:
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.model_selection import GroupKFold
    from sklearn.metrics import r2_score

    rows, world_of = dataset()
    X = np.array([[row[name] for name in FEATURES] for row in rows])
    y = np.array([row["net"] for row in rows])
    groups = np.array(world_of)
    print(f"rows {len(rows)} worlds {len(set(world_of))} net mean {y.mean():.2f} sd {y.std():.2f}")
    model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.06, max_leaf_nodes=31,
                                          min_samples_leaf=40, l2_regularization=1.0, random_state=7)
    ridge = Ridge(alpha=1.0)
    oof = np.zeros(len(y))
    for train, test in GroupKFold(n_splits=5).split(X, y, groups):
        model.fit(X[train], y[train])
        oof[test] = model.predict(X[test])
    print(f"OOF R2 {r2_score(y, oof):.3f}  MAE {np.mean(np.abs(y - oof)):.1f}  base MAE {np.mean(np.abs(y - y.mean())):.1f}")
    order = np.argsort(oof)
    for k in range(10):
        sel = order[k * len(order) // 10:(k + 1) * len(order) // 10]
        print(f"  decile {k}: predicted {np.mean(oof[sel]):7.1f}  realised {np.mean(y[sel]):7.1f}")
    # keep a full-data model for production
    model.fit(X, y)
    ridge.fit(X, y)
    with open(OUT, "wb") as fh:
        pickle.dump({"model": model, "ridge": ridge, "features": FEATURES}, fh)
    print("wrote", OUT)


class SelectModel:
    def __init__(self, model, features):
        self.model = model
        self.features = features

    @classmethod
    def load(cls, path: str = OUT) -> "SelectModel":
        blob = pickle.load(open(path, "rb"))
        return cls(blob["model"], blob["features"])

    def net(self, request: dict, option: dict, road, supply: dict, mean_supply: float,
            lane_factor: float = 0.0) -> float:
        row = features_for(request, option, road, supply, mean_supply, lane_factor)
        return float(self.model.predict(np.array([[row[name] for name in self.features]]))[0])


if __name__ == "__main__":
    import sys
    if "fit" in sys.argv:
        fit()
