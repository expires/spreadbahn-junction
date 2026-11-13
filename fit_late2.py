"""Refit P(late) on the staff supply features (supply.order_features) with world-held-out CV.

The shipped data/fits/supply.json lateness block predicts ~0 for every real order (the
standardisation used in its fit was not saved), so this refits the same 24 features properly
and writes data/fits/late2.json. dispatch_next wraps it and substitutes it for the broken block.

    python fit_late2.py
"""
from __future__ import annotations

import glob
import gzip
import json
import os

import numpy as np

import supply

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "fits", "late2.json")


def dataset():
    road = {(r["from"], r["to"]): r for r in json.load(open(os.path.join(DATA, "fits", "map.json")))["roads"]}

    class Road:
        def minutes(self, a, b):
            return road[(a, b)]["minutes"]

        def km(self, a, b):
            return road[(a, b)]["km"]

    r = Road()
    rows = []
    worlds = []
    for folder in sorted(glob.glob(os.path.join(DATA, "L8", "w*"))):
        world = os.path.basename(folder)
        try:
            reveal = json.load(gzip.open(os.path.join(folder, "reveal.json.gz")))
            events = json.load(gzip.open(os.path.join(folder, "events.json.gz")))
            roster = json.load(gzip.open(os.path.join(folder, "roster.json.gz")))
        except FileNotFoundError:
            continue
        structure = supply.build_supply(roster, r)
        won = {e["order"]["id"]: e for e in events if e["kind"] == "order_won"}
        if not won:
            continue
        delivered = [e["at"] for e in events if e["kind"] == "delivered"]
        cancelled = {e["orderId"] for e in events if e["kind"] == "order_cancelled"}
        truth = {o["orderId"]: o for o in reveal["orders"]}
        # outstanding backlog at each minute: count orders won whose last delivery is after that minute
        order_times = []
        for oid, event in won.items():
            end = truth.get(oid)
            done = None
            if end is not None and all(l["deliveredAt"] is not None for l in end["legs"]):
                done = max(l["deliveredAt"] for l in end["legs"])
            order_times.append((event["at"], done))
        order_times.sort(key=lambda item: item[0])
        for record in reveal["requests"]:
            oid = record["request"]["id"]
            if record["outcome"] != "won" or not record["option"] or oid not in won or oid in cancelled:
                continue
            end = truth.get(oid)
            if end is None:
                continue
            request = record["request"]
            option = next((o for o in request["options"] if o["kind"] == record["option"]), None)
            if option is None:
                continue
            at = request["respondBy"]
            outstanding = sum(1 for (start, done) in order_times if start <= at and (done is None or done > at))
            legs = end["legs"]
            late = 0.0 if all(l["deliveredAt"] is not None and l["deliveredAt"] <= won[oid]["order"]["deadline"] for l in legs) else 1.0
            try:
                feats = supply.order_features(request, option, r, structure, outstanding=outstanding)
            except Exception:  # noqa: BLE001
                continue
            rows.append((feats, late))
            worlds.append(world)
    return rows, worlds


def main() -> None:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score

    rows, worlds = dataset()
    names = supply.load_models().lateness.features
    X = np.array([[float(f.get(n, 0.0)) for n in names] for f, _ in rows])
    y = np.array([label for _, label in rows])
    print(f"rows {len(rows)}  worlds {len(set(worlds))}  late {y.mean():.3f}")
    world_arr = np.array(worlds)
    unique = sorted(set(worlds))
    rng = np.random.default_rng(11)
    rng.shuffle(unique)
    folds = np.array_split(np.array(unique), 5)
    oof = np.zeros(len(y))
    for fold in folds:
        test = np.isin(world_arr, fold)
        train = ~test
        mean, scale = X[train].mean(0), X[train].std(0)
        scale[scale == 0] = 1.0
        model = LogisticRegression(C=0.3, max_iter=3000)
        model.fit((X[train] - mean) / scale, y[train])
        oof[test] = model.predict_proba((X[test] - mean) / scale)[:, 1]
    print(f"OOF AUC {roc_auc_score(y, oof):.4f}  Brier {brier_score_loss(y, oof):.4f}  base {y.mean():.4f}")
    for i in range(10):
        sel = np.argsort(oof)[i * len(oof) // 10:(i + 1) * len(oof) // 10]
        print(f"  decile {i}: pred {oof[sel].mean():.3f} actual {y[sel].mean():.3f}")
    mean, scale = X.mean(0), X.std(0)
    scale[scale == 0] = 1.0
    model = LogisticRegression(C=0.3, max_iter=3000)
    model.fit((X - mean) / scale, y)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"features": names, "mean": mean.tolist(), "scale": scale.tolist(),
               "coef": model.coef_[0].tolist(), "intercept": float(model.intercept_[0]),
               "oof_auc": float(roc_auc_score(y, oof)), "base_rate": float(y.mean())}, open(OUT, "w"), indent=1)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
