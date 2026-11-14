"""Offline evaluator for a dispatcher policy over the archived level-8 request streams.

No worlds are created: it replays each archived world's requests through the policy's own
quote() decision, then scores the decision with the fitted models instead of the real world:

  P(win request | menu)  = quote_model.OnlineCompetitor   (held-out AUC 0.889)
  which option the dealer takes  = quote_model choice model (conditional logit)
  expected driver fee  = supply.booking.expected_fee_cents (MAE 0.17 x ref)
  P(late)  = the refit supply-feature lateness model (OOF AUC 0.789)
  money   = price - expected fee - P(late) * (refund share * price + late penalty)

The competitor filter and the dealer trust are advanced with sampled outcomes, so a whole
world replay is deterministic per seed. The real driver market (capacity, drops, chains) is not
simulated, so the evaluator is a pricing/selection instrument; operational changes (posting
earlier, bundling, direct offers) must still be A/B tested live.

    python evaluate.py --tag ev7 --seeds ev-1,ev-2 --set price_mult=1.0
    python evaluate.py --tag calib --runs      # calibrate against the 50 recorded v5-v7 worlds
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
import random
import statistics
import sys
from dataclasses import replace

import importlib

import supply
from quote_model import OnlineCompetitor, QuoteModel

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")


class StubWorld:
    """Only the attributes quote() touches: it never asks the world anything at quote time."""

    def __init__(self, wid: str = "offline"):
        self.id = wid


class Road:
    def __init__(self, wmap):
        self.r = {(x["from"], x["to"]): x for x in wmap["roads"]}

    def minutes(self, a: str, b: str) -> int:
        return self.r[(a, b)]["minutes"]

    def km(self, a: str, b: str) -> float:
        return self.r[(a, b)]["km"]


def load_models():
    qm = QuoteModel.load()
    smodels = supply.load_models()
    try:
        from late_model import SupplyLateModel
        smodels.lateness = SupplyLateModel.load()
    except Exception:  # noqa: BLE001
        pass
    return qm, smodels


def archive_worlds(prefix: str = "ev"):
    """The archived worlds grouped by their request signature; ev-N names the panel seeds."""
    known = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "runs", "v[0-9]*.json"))):
        for result in json.load(open(path)).get("results", []):
            known[result["seed"]] = result["world"]  # later files (v7) win
    out = []
    for folder in sorted(glob.glob(os.path.join(DATA, "L8", "*"))):
        name = os.path.basename(folder)
        try:
            reveal = json.load(gzip.open(os.path.join(folder, "reveal.json.gz")))
            snapshot = json.load(gzip.open(os.path.join(folder, "snapshot.json.gz")))
            roster = json.load(gzip.open(os.path.join(folder, "roster.json.gz")))
        except FileNotFoundError:
            continue
        out.append(dict(world=name, reveal=reveal, score=snapshot["score"], roster=roster))
    return out, known


def replay(module, policy, world, road, qm, smodels, rng, mapjson):
    """Score one world's request stream under one policy. Returns (margin, orders, on_time)."""
    desk = module.Dispatcher(StubWorld(world["world"]), 8, 0, policy, road)
    desk.init_supply(world["roster"])
    competitor = OnlineCompetitor(qm)
    trust: dict[str, float] = {}
    margin = 0.0
    orders = 0.0
    on_time = 0.0
    requests = sorted(world["reveal"]["requests"], key=lambda record: (record["request"]["arrivedAt"], record["request"]["id"]))
    for record in requests:
        request = record["request"]
        desk.now = request["arrivedAt"]
        desk.outbox = []
        # The policy's own online competitor estimate lags/mismatches the menu model when it is fed
        # sampled outcomes, so seed it with the fitted filter: the v7 SGD agreed with the real
        # world level to ~0.06 log. The fitted "level" is not the same scale as v7's comp multiplier,
        # so translate through the model's own 50/50 threshold T (level + A + lane + trust).
        try:
            ctx0 = qm.context(request, road)
            eff = competitor.log_comp_for(ctx0, int(trust.get(request["dealerId"], 0)))                 + qm.A_value(ctx0, len(request["options"]))
            desk.log_comp = eff
        except Exception:  # noqa: BLE001
            pass
        dealer = request["dealerId"]
        desk.trust[dealer] = int(trust.get(dealer, 0))
        try:
            desk.quote(request)
        except Exception:  # noqa: BLE001 - a policy bug must not kill the replay
            continue
        quote = next((action for action in desk.outbox if action.get("type") == "quote"), None)
        prices = (quote or {}).get("prices") or {}
        options = {option["kind"]: option for option in request["options"]}
        genuine = {kind: value for kind, value in prices.items()
                   if kind in options and value < 3 * options[kind]["listPriceCents"]}
        if not genuine:
            continue
        ctx = qm.context(request, road)
        trust_count = int(trust.get(dealer, 0))
        takes = competitor.p_take(ctx, genuine, trust_count)
        if not takes:
            continue
        try:
            structure = desk.supply_struct or supply.build_supply(world["roster"], road)
        except Exception:  # noqa: BLE001
            structure = None
        won = 0.0
        for kind, probability in takes.items():
            if probability <= 0:
                continue
            option = options[kind]
            price = genuine[kind] / 100.0
            fee, p_late = 0.6 * request["referenceFeeCents"] / 100.0, 0.05
            if structure is not None:
                try:
                    feats = supply.order_features(request, option, road, structure, outstanding=0)
                    fee = smodels.booking.expected_fee_cents(feats) / 100.0
                    p_late = float(smodels.lateness.p_late(feats))
                except Exception:  # noqa: BLE001
                    pass
            net = price - fee - p_late * (0.20 * price + 25.0)
            margin += probability * net
            orders += probability
            on_time += probability * (1.0 - p_late)
            won += probability
        if won > 0:
            outcome = 1.0 if rng.random() < min(1.0, won) else 0.0
            try:
                competitor.observe(ctx, genuine, outcome > 0, request["respondBy"], trust_count)
            except Exception:  # noqa: BLE001
                pass
            try:  # keep the policy's own online state realistic (its SGD, its dealer trust)
                desk.learn(request["id"], outcome > 0)
            except Exception:  # noqa: BLE001
                pass
            # dealer trust: on time by the option the dealer took, in proportion to the take probabilities
            for kind, probability in takes.items():
                if rng.random() < probability:
                    p_late = 0.05
                    if structure is not None:
                        try:
                            feats = supply.order_features(request, options[kind], road, structure)
                            p_late = float(smodels.lateness.p_late(feats))
                        except Exception:  # noqa: BLE001
                            pass
                    if rng.random() >= p_late:
                        trust[dealer] = min(25, trust.get(dealer, 0) + 1)
                    else:
                        trust[dealer] = trust.get(dealer, 0) // 2
                    desk.trust[dealer] = int(trust[dealer])
                    break
    share = on_time / orders if orders else 0.0
    return margin, orders, share


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--module", default="dispatch_next")
    ap.add_argument("--seeds", default="")
    ap.add_argument("--set", nargs="*", default=[])
    ap.add_argument("--known", action="store_true", help="replay only the worlds with a recorded v1-v7 run")
    args = ap.parse_args()
    module = importlib.import_module(args.module)
    policy = module.Policy()
    for kv in args.set:
        key, value = kv.split("=", 1)
        current = getattr(policy, key)
        policy = replace(policy, **{key: tuple(map(tuple, json.loads(value))) if isinstance(current, tuple) else type(current)(json.loads(value))})
    qm, smodels = load_models()
    wmap = json.load(open(os.path.join(DATA, "fits", "map.json")))
    road = Road(wmap)
    worlds, known = archive_worlds()
    # a fixed panel: the six archived seeds ev-1..ev-6 are the worlds the recorded runs used
    panel = []
    by_signature = {}
    for world in worlds:
        signature = tuple(sorted(record["request"]["id"] for record in world["reveal"]["requests"]))[:1]
        by_signature.setdefault(len(world["reveal"]["requests"]), []).append(world)
    selected = worlds
    if args.known:
        recorded_ids = {result["world"] for path in sorted(glob.glob(os.path.join(ROOT, "runs", "v[0-9]*.json")))
                        for result in json.load(open(path)).get("results", [])}
        selected = [world for world in worlds if world["world"] in recorded_ids]
    if args.seeds:
        wanted = {name.strip() for name in args.seeds.split(",")}
        selected = [world for world in worlds if any(world["world"].startswith(known.get(name, "??")) for name in wanted)]
    results = []
    for index, world in enumerate(selected):
        rng = random.Random(f"{args.tag}:{world['world']}")
        margin, orders, share = replay(module, policy, world, road, qm, smodels, rng, wmap)
        results.append(dict(world=world["world"], margin=margin, orders=orders, on_time=share,
                            recorded=world["score"]["marginCents"] / 100.0, recorded_orders=world["score"]["wonOrders"],
                            recorded_on_time=world["score"]["onTimeShare"]))
        if (index + 1) % 20 == 0:
            print(f"  {index + 1}/{len(selected)}", flush=True)
    print(f"{args.tag}: {len(results)} worlds  mean margin {statistics.mean(r['margin'] for r in results):,.0f}  "
          f"mean orders {statistics.mean(r['orders'] for r in results):.0f}  on time {statistics.mean(r['on_time'] for r in results):.1%}")
    os.makedirs(os.path.join(ROOT, "runs"), exist_ok=True)
    json.dump(dict(policy={k: str(v) for k, v in policy.__dict__.items()}, results=results),
              open(os.path.join(ROOT, "runs", f"eval-{args.tag}.json"), "w"), indent=1)
    recorded = [r for r in results if r["recorded_orders"]]
    if len(recorded) >= 5:
        xs = [r["recorded"] for r in recorded]
        ys = [r["margin"] for r in recorded]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        r = cov / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
        print(f"  calibration vs recorded on {len(recorded)} worlds: corr {r:+.3f}, bias {my - mx:+,.0f}/world")


if __name__ == "__main__":
    main()
