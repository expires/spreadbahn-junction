"""Edge probe: play a practice week with the bot up to a cut minute, fork it, and finish each fork with one
lever nudged. Forks share the history and the future draws, so margin differences are the local gradient
of margin with respect to each lever (common random numbers, no week-to-week noise).

    python edge_probe.py --seeds pr-1,pr-2 --cut 2880
"""
import argparse
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from bahn.simulation import dispatch
from bahn.simulation.dispatch import DAY, Policy, Road, play_practice
from spread.client import Client

from bahn.paths import ROOT
LEVERS = [
    ("base", {}),
    ("price +5%", {"price_mult": 1.05}), ("price −5%", {"price_mult": 0.95}),
    ("fees +10%", {"fee_mult": 1.10}), ("fees −10%", {"fee_mult": 0.90}),
    ("λ ×0.5 (take more risk)", {"late_shadow": 40.0}), ("λ ×2 (take less risk)", {"late_shadow": 160.0}),
]


def step_to(world, desk, until):
    from dispatch import WAKE_ON
    while desk.now < until:
        actions, desk.outbox = desk.outbox, []
        st = world.advance(until=WAKE_ON + ["order_lost", "offer_answered"], max=min(until, (desk.now // DAY + 1) * DAY, max(desk.now + 1, desk.next_timer())), actions=actions)
        desk.read_results(actions, st["results"])
        desk.now = st["now"]
        for e in st["events"]:
            desk.handle(e)
        desk.fire_timers()


def probe(seed: str, cut: int) -> dict:
    client = Client()
    road = Road(client.map())
    world = client.create(level=8, seed=seed)
    dispatch.LIVE.add(world.id)
    snap = world.snapshot()
    desk = dispatch.Dispatcher(world, 8, snap["now"], Policy(), road)
    desk.init_supply(world.roster())
    step_to(world, desk, cut)
    s0 = world.score()
    out = {"seed": seed, "cut": cut, "at_cut": s0["marginCents"] / 100}

    def finish(i):
        label, change = LEVERS[i]
        for attempt in range(5):               # same history, same future draws
            try:
                f = Client().world(world.id).fork()   # own client per thread: the kit's connection is not thread-safe
                break
            except Exception:
                if attempt == 4:
                    raise
                import time; time.sleep(3)
        dispatch.LIVE.add(f.id)
        d = copy.copy(desk)
        for k, v in vars(desk).items():  # deep-copy the books, share the clients
            if k not in ("world", "p", "road", "regime", "outbox"):
                setattr(d, k, copy.deepcopy(v))
        d.outbox = []
        d.regime = copy.deepcopy(desk.regime)
        d.world, d.p = f, replace(Policy(), **change)
        play_practice(f, 8, d.p, road, d)
        sc = f.score()
        dispatch.LIVE.discard(f.id)
        return label, sc["marginCents"] / 100, sc["onTimeShare"], sc["wonOrders"]

    with ThreadPoolExecutor(1) as pool:  # base + 1 fork at a time (team slots are shared)
        for label, m, ot, n in pool.map(finish, range(len(LEVERS))):
            out[label] = {"margin": m, "onTime": ot, "orders": n}
    world.delete()
    dispatch.LIVE.discard(world.id)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--cut", type=int, default=2 * DAY)
    a = ap.parse_args()
    res = []
    for seed in a.seeds.split(","):
        r = probe(seed, a.cut)
        res.append(r)
        base = r["base"]["margin"]
        print(f"{seed} (forked at minute {a.cut}, margin so far €{r['at_cut']:,.0f}):")
        for label, _ in LEVERS:
            x = r[label]
            print(f"   {label:26} €{x['margin']:>9,.0f}  Δ {x['margin'] - base:>+7,.0f}  on-time {x['onTime']:.1%}  orders {x['orders']}")
        json.dump(res, open(os.path.join(ROOT, "runs", "edge_probe.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
