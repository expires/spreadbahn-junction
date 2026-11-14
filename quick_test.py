"""One-off deterministic A/B runner for dispatcher candidates, trading two practice slots at a time.

    python quick_test.py --tag q_pair --seeds ev-1,ev-2 --parallel 1 --set pair_enabled=1

Waits for a free practice slot (the partner sweeps on the same key) before creating each world,
plays it with dispatch_next.play_practice, prints the score and keeps the reveal in data/L8/.
A world is deleted if the run raises, so no slot is leaked.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

import importlib
import os as _os
D = importlib.import_module(_os.environ.get("DISPATCH_MODULE", "dispatch"))
from spread.client import Client, SpreadError

LIVE: set[str] = set()


def free_slot(wait_s: float = 1800.0) -> None:
    deadline = time.time() + wait_s
    started = time.time()
    while True:
        live = [w for w in Client().worlds() if not w["ended"]]
        if len(live) < 8:
            print(f"  slot free after {time.time() - started:.0f}s ({len(live)} live)", flush=True)
            return
        if time.time() > deadline:
            raise SystemExit("no free practice slot")
        time.sleep(2)


def one(seed: str, level: int, policy: D.Policy, tag: str, keep: bool) -> dict:
    attempts = 3
    for attempt in range(attempts):
        try:
            return _one(seed, level, policy, tag, keep)
        except (SpreadError, OSError) as error:
            if attempt == attempts - 1:
                raise
            print(f"  {seed}: attempt {attempt + 1} failed ({error!r}); retrying", file=sys.stderr, flush=True)
            time.sleep(8)
    raise RuntimeError("unreachable")


def _one(seed: str, level: int, policy: D.Policy, tag: str, keep: bool) -> dict:
    free_slot()
    client = Client()
    road = D.Road(client.map())
    world = client.create(level=level, seed=seed)
    LIVE.add(world.id)
    t0 = time.time()
    try:
        desk = D.play_practice(world, level, policy, road)
    except BaseException:
        try:
            world.delete()
        except SpreadError:
            pass
        raise
    LIVE.discard(world.id)
    if keep:
        import explore
        explore.save(client, world, f"{tag}-{seed}", level, [])
    score = world.score()
    econ = {}
    try:  # the euro decomposition: where the margin actually went
        ledger: dict[str, int] = {}
        counts = {"gig_booked": 0, "gig_dropped": 0, "no_show": 0, "visit_failed": 0,
                  "order_cancelled": 0, "deadline_close": 0, "gig_expired": 0}
        drivers: set = set()
        for event in world.events(-1):
            kind = event["kind"]
            if kind == "ledger":
                line = event["line"]
                ledger[line["kind"]] = ledger.get(line["kind"], 0) + line["cents"]
            elif kind in counts:
                counts[kind] += 1
                if kind == "gig_booked":
                    drivers.add(event.get("driverId"))
        positive_costs = ("revenue",)
        items = {}
        for kind, cents in ledger.items():
            items[kind] = cents / 100.0 if kind in positive_costs else -cents / 100.0
        scores = score or {}
        won = int(scores.get("wonOrders") or 0)
        on_time = float(scores.get("onTimeShare") or 0.0)
        econ = {"ledger": items, **counts,
                "drivers_booked": len(drivers),
                "late_orders": round(won * (1.0 - on_time)) if won else 0,
                "undelivered_orders": round(won * float(scores.get("undeliveredShare") or 0.0)) if won else 0}
    except Exception:  # noqa: BLE001 - diagnostics must never fail a run
        econ = {}
    return {"seed": seed, "world": world.id, "secs": round(time.time() - t0), "comp": round(math.exp(desk.log_comp), 3),
            "pairs": desk.stats.get("pairs", 0), "singles": desk.stats.get("singles", 0),
            "econ": econ, **desk.stats, **score}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--level", type=int, default=8)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    policy = D.Policy()
    for kv in a.set:
        k, v = kv.split("=", 1)
        cur = getattr(policy, k)
        policy = replace(policy, **{k: tuple(map(tuple, json.loads(v))) if isinstance(cur, tuple) else type(cur)(json.loads(v))})
    seeds = a.seeds.split(",")
    print(f"{a.tag}: {len(seeds)} seeds, policy overrides {a.set}", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
        futures = {pool.submit(one, s, a.level, policy, a.tag, a.keep): s for s in seeds}
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as error:  # noqa: BLE001
                print(f"{futures[fut]}: FAILED {error!r}", file=sys.stderr, flush=True)
                continue
            results.append(r)
            print(f"  {a.tag} {r['seed']:>6}  margin EUR{r['marginCents']/100:>9,.0f}  {r['wonOrders']:>4} orders  "
                  f"{r['onTimeShare']:6.1%} on time  pairs={r['pairs']}/{r['singles']} refused={r.get('refused',0)} exp={r.get('expired',0)} urg={r.get('urgent',0)}  comp~{r['comp']}  {r['secs']}s", flush=True)
    if results:
        mean = sum(r["marginCents"] for r in results) / len(results) / 100
        print(f"  {a.tag} MEAN EUR{mean:,.0f}  worst on time {min(r['onTimeShare'] for r in results):.1%}  "
              f"ranked {sum(r['ranked'] for r in results)}/{len(results)}", flush=True)
        os.makedirs("runs", exist_ok=True)
        with open(os.path.join("runs", f"{a.tag}.json"), "w") as fh:
            json.dump({"policy": {k: v for k, v in policy.__dict__.items()}, "results": results}, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
