"""Offline probe: compare the fitted-menu quotes against the legacy quotes on archived requests.

No world is created: a stub world is enough because quote() only needs the local bookkeeping.
For every request we compute the model's expected margin of the two menus (same competitor state)
and print aggregates. This is a code-path check, not a result claim.
"""
from __future__ import annotations

import gzip
import json
import sys
import math

import dispatch
import supply
from quote_model import QuoteModel, OnlineCompetitor
from late_model import SupplyLateModel


class StubWorld:
    id = "stub"


def load_world(folder: str):
    g = lambda name: json.load(gzip.open(f"{folder}/{name}.json.gz"))
    return g("reveal"), g("roster"), g("snapshot")


def build(policy):
    road = dispatch.Road(json.load(open("data/fits/map.json")))
    desk = dispatch.Dispatcher(StubWorld(), 8, 0, policy, road)
    return desk


def menu_ev(desk, request, prices):
    ctx = desk.qm.context(request, desk.road)
    genuine = {o["kind"]: prices[o["kind"]] for o in request["options"]
               if prices.get(o["kind"], 0) < 3 * o["listPriceCents"]}
    if not genuine:
        return None
    options = {o["kind"]: o for o in request["options"]}
    takes = desk.oc.p_take(ctx, genuine, desk.trust.get(request["dealerId"], 0))
    value = 0.0
    for kind, probability in takes.items():
        option = options[kind]
        deadline = desk.effective_deadline(request, option["deadline"])
        terms = desk._supply_terms(request, option, deadline)
        pl, fee = terms if terms else (0.1, request["referenceFeeCents"] / 100 * 0.9)
        price = genuine[kind] / 100.0
        value += probability * (price - fee - pl * (desk.p.late_cost * price + desk.p.late_shadow))
    return value


def run(folder: str):
    reveal, roster, snapshot = load_world(folder)
    requests = sorted(reveal["requests"], key=lambda r: r["request"]["arrivedAt"])
    road = dispatch.Road(json.load(open("data/fits/map.json")))
    menu = build(dispatch.Policy(use_quote_model=1))
    legacy = build(dispatch.Policy())
    for desk in (menu, legacy):
        desk.now = 0
        desk.init_supply(roster)
    legacy.qm = menu.qm = QuoteModel.load()
    legacy.oc = menu.oc = OnlineCompetitor(menu.qm)
    totals = {"menu": 0.0, "legacy": 0.0}
    accepted = {"menu": 0, "legacy": 0}
    n = 0
    for record in requests:
        request = record["request"]
        for desk, key in ((menu, "menu"), (legacy, "legacy")):
            desk.now = request["arrivedAt"]
            desk.outbox.clear()
            try:
                desk.quote(request)
            except Exception as error:  # noqa: BLE001
                print("quote failed", key, type(error).__name__, error, file=sys.stderr)
                return
            prices = {}
            for action in desk.outbox:
                if action.get("type") == "quote" and action["requestId"] == request["id"]:
                    prices = action["prices"]
            if not prices:
                continue
            value = menu_ev(menu, request, prices)
            if value is None:
                continue
            totals[key] += value
            accepted[key] += 1
        n += 1
    print(f"{folder.split('/')[-1]}: {n} requests | model-menu EV {totals['menu']:.0f} | legacy EV {totals['legacy']:.0f} | delta {totals['menu'] - totals['legacy']:+.0f}")


if __name__ == "__main__":
    for folder in sys.argv[1:] or ["data/L8/q_hf4-ev-4", "data/L8/q6_m30-ev-1"]:
        run(folder)
