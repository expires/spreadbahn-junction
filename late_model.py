"""P(order is late | what is knowable when the quote is made), fitted on the archive.

The v7 dispatcher guesses lateness from a hand-set slack table plus a logit in two supply
numbers. The archive has 74,731 won orders with revealed delivery times, so the hazard can
be fitted directly: a per-option logistic on supply at both ends, distance, slack, option
kind, private addresses, notes, hour/day and the level-8 town shortage relative to the
world's own mean. dispatch v8 uses it twice: as p_late in the expected-margin computation
and as a hard gate on options that are very likely late.

    python late_model.py fit          # fit + write data/fits/late.json (world-held-out AUC)
    from late_model import LateModel  # model = LateModel.load(); model.p_late(...)
"""
from __future__ import annotations

import glob
import gzip
import json
import math
import os
from dataclasses import dataclass

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
FIT = os.path.join(DATA, "fits", "late.json")
DAY, HOUR = 1440, 60
OPTIONS = ("same_day", "next_day", "within_3_days")
FEATURES = (
    "slack_h", "lead_h", "log_km", "log_mins", "log_sp", "log_sd", "log_smin", "rel_sp", "rel_sd", "rel_smin",
    "log_drivers", "private_p", "private_d", "note", "n_legs", "premium", "electric", "ref_over_list",
    "hour", "dow", "opt_same_day", "opt_next_day", "opt_within",
)


def _roads() -> dict:
    path = os.path.join(DATA, "fits", "map.json")
    if not os.path.exists(path):
        path = os.path.join(DATA, "map.json")
    return {(r["from"], r["to"]): r for r in json.load(open(path))["roads"]}


def supply_of(roster: list[dict], road: dict) -> dict[str, float]:
    """Expected drivers within reach of each place: sum over towns of residents * exp(-minutes/60)."""
    homes: dict[str, int] = {}
    for driver in roster:
        home = driver["homePlaceId"]
        homes[home] = homes.get(home, 0) + 1
    places = {a for a, _ in road}
    return {p: sum(n * math.exp(-road[(h, p)]["minutes"] / 60.0) for h, n in homes.items()) for p in places}


def features_for(request: dict, option: dict, order: dict | None, supply: dict, road: dict,
                 drivers: int, world_mean_supply: float) -> dict:
    """The feature row for one option of one request. Uses only what a dispatcher can see."""
    leg = request["legs"][0]
    p, d = leg["pickup"]["placeId"], leg["delivery"]["placeId"]
    km = sum(road[(l["pickup"]["placeId"], l["delivery"]["placeId"])]["km"] for l in request["legs"])
    mins = sum(road[(l["pickup"]["placeId"], l["delivery"]["placeId"])]["minutes"] for l in request["legs"])
    sp, sd = supply.get(p, 0.0), supply.get(d, 0.0)
    smin = min(sp, sd)
    mean = world_mean_supply or 1.0
    return {
        "slack_h": (option["deadline"] - request["respondBy"]) / 60.0,
        "lead_h": (option["deadline"] - mins - 30 - request["respondBy"]) / 60.0,
        "log_km": math.log1p(km), "log_mins": math.log1p(mins),
        "log_sp": math.log1p(sp), "log_sd": math.log1p(sd), "log_smin": math.log1p(smin),
        "rel_sp": sp / mean, "rel_sd": sd / mean, "rel_smin": smin / mean,
        "log_drivers": math.log1p(drivers),
        "private_p": 1.0 if leg["pickup"]["privateAddress"] else 0.0,
        "private_d": 1.0 if leg["delivery"]["privateAddress"] else 0.0,
        "note": 1.0 if request.get("note") else 0.0,
        "n_legs": len(request["legs"]),
        "premium": 1.0 if leg["car"]["premium"] else 0.0,
        "electric": 1.0 if leg["car"]["electric"] else 0.0,
        "ref_over_list": math.log(max(1e-6, request["referenceFeeCents"] / max(1, option["listPriceCents"]))),
        "hour": (request["arrivedAt"] % DAY) / 60.0,
        "dow": (request["arrivedAt"] // DAY) % 7,
        "opt_same_day": 1.0 if option["kind"] == "same_day" else 0.0,
        "opt_next_day": 1.0 if option["kind"] == "next_day" else 0.0,
        "opt_within": 1.0 if option["kind"] == "within_3_days" else 0.0,
    }


def _matrix(rows: list[dict]) -> np.ndarray:
    return np.array([[row[f] for f in FEATURES] for row in rows], dtype=float)


class SupplyLateModel:
    """P(late) on supply.order_features() rows (the 24 features of the staff fit).

    The shipped data/fits/supply.json lateness block predicts ~0 because its standardisation was
    not saved; data/fits/late2.json is the same feature set refit with the mean/scale kept
    (OOF AUC 0.789, calibrated deciles). This wrapper is drop-in for supply.LatenessModel.
    """

    def __init__(self, blob: dict):
        self.features = list(blob["features"])
        self.mean = np.array(blob["mean"], dtype=float)
        self.scale = np.array(blob["scale"], dtype=float)
        self.scale[self.scale == 0] = 1.0
        self.coef = np.array(blob["coef"], dtype=float)
        self.intercept = float(blob["intercept"])

    @classmethod
    def load(cls, path: str = os.path.join(DATA, "fits", "late2.json")) -> "SupplyLateModel":
        return cls(json.load(open(path)))

    def p_late(self, feats: dict) -> float:
        x = np.array([float(feats.get(name, 0.0)) for name in self.features])
        z = self.intercept + float(((x - self.mean) / self.scale) @ self.coef)
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


@dataclass
class LateModel:
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray
    intercept: float

    @classmethod
    def load(cls) -> "LateModel":
        payload = json.load(open(FIT))
        return cls(np.array(payload["mean"]), np.array(payload["scale"]), np.array(payload["coef"]), payload["intercept"])

    def p_late_matrix(self, rows: list[dict]) -> np.ndarray:
        z = ((_matrix(rows) - self.mean) / self.scale) @ self.coef + self.intercept
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def p_late(self, row: dict) -> float:
        z = float(((np.array([row[f] for f in FEATURES]) - self.mean) / self.scale) @ self.coef + self.intercept)
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def save(self, path: str = FIT) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump({"features": list(FEATURES), "mean": self.mean.tolist(), "scale": self.scale.tolist(),
                   "coef": self.coef.tolist(), "intercept": float(self.intercept)}, open(path, "w"), indent=1)


def archive_rows(limit_worlds: int | None = None):
    """One row per won order-option in the archive: features + late label + world id."""
    road = _roads()
    folders = sorted(glob.glob(os.path.join(DATA, "L8", "w*")))
    if limit_worlds:
        folders = folders[:limit_worlds]
    for folder in folders:
        try:
            reveal = json.load(gzip.open(os.path.join(folder, "reveal.json.gz")))
            snapshot = json.load(gzip.open(os.path.join(folder, "snapshot.json.gz")))
            roster = json.load(gzip.open(os.path.join(folder, "roster.json.gz")))
        except FileNotFoundError:
            continue
        world = os.path.basename(folder)
        supply = supply_of(roster, road)
        mean = sum(supply.values()) / max(1, len(supply))
        dealer_of = {r["request"]["id"]: r["request"]["dealerId"] for r in reveal["requests"]}
        outcomes = {r["request"]["id"]: r for r in reveal["requests"]}
        for order in reveal["orders"]:
            oid = order["orderId"]
            record = outcomes.get(oid)
            if record is None or record["outcome"] != "won" or record["option"] is None:
                continue
            request = record["request"]
            option = next((o for o in request["options"] if o["kind"] == record["option"]), None)
            if option is None:
                continue
            deadline = option["deadline"]
            delivered = [leg["deliveredAt"] for leg in order["legs"]]
            late = 1.0 if any(t is None or t > deadline for t in delivered) else 0.0
            row = features_for(request, option, order, supply, road, len(roster), mean)
            row["late"] = late
            row["world"] = world
            yield row


def fit() -> None:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    rows = list(archive_rows())
    print(f"rows {len(rows)}  late share {np.mean([r['late'] for r in rows]):.3f}")
    X, y = _matrix(rows), np.array([r["late"] for r in rows])
    worlds = sorted({r["world"] for r in rows})
    rng = np.random.default_rng(7)
    rng.shuffle(worlds)
    hold = set(worlds[: max(1, len(worlds) // 5)])
    train = np.array([r["world"] not in hold for r in rows])
    mean, scale = X[train].mean(0), X[train].std(0)
    scale[scale == 0] = 1.0
    model = LogisticRegression(C=0.5, max_iter=2000)
    model.fit((X[train] - mean) / scale, y[train])
    p = model.predict_proba((X[~train] - mean) / scale)[:, 1]
    print(f"held-out worlds {len(hold)}  AUC {roc_auc_score(y[~train], p):.3f}")
    # calibration deciles
    order = np.argsort(p)
    for k in range(10):
        sel = order[k * len(order) // 10:(k + 1) * len(order) // 10]
        print(f"  decile {k}: predicted {p[sel].mean():.3f} actual {y[~train][sel].mean():.3f}")
    fitted = LateModel(mean, scale, model.coef_[0], float(model.intercept_[0]))
    fitted.save()
    print(f"wrote {FIT}")
    print("coef:", dict(zip(FEATURES, np.round(model.coef_[0], 3))))


if __name__ == "__main__":
    import sys
    if "fit" in sys.argv:
        fit()
