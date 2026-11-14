"""Ablation table for the jury: the full system, and the full system minus one idea at a time, on the same
weeks (level 8). Plus the kit's starter on the same weeks. Writes runs/ablation.json and prints a table.

    python ablate.py --seeds 20 --prefix jury --parallel 5
"""
import argparse
import json
import os
import statistics as st
from dataclasses import replace

from bahn.simulation import dispatch
from bahn.simulation.dispatch import Policy

from bahn.paths import ROOT
# (label shown to the jury, what is switched off)
ABLATIONS = [
    ("Full system", {}),
    ("− persistent lane prices", {"lanes": False}),
    ("− day-level / price-war / outage learner", {"regime": False}),
    ("− archive-fitted lateness model (our 6-week model instead)", {"late_v9": False}),
    ("− two-car chains (return pairs only)", {"pair_chains": False}),
    ("− all two-car gigs", {"combine": False}),
    ("− backhaul-aware quotes", {"pair_quote": 0.0}),
    ("− note reader (kit's keyword reader instead)", {"notes_v2": False}),
    ("− day capacity & edge-day risk", {"day_cap": 0.0, "edge_late": (0.0, 0.0, 0.0)}),
    ("− holding gigs until 36 h before pickup", {"hold_hours": 1000.0}),
    ("− late-order shadow price λ", {"late_shadow": 0.0}),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--prefix", default="jury")
    ap.add_argument("--parallel", type=int, default=5)
    ap.add_argument("--only", default="", help="comma-separated ablation indexes to (re)run")
    a = ap.parse_args()
    seeds = [f"{a.prefix}-{i}" for i in range(1, a.seeds + 1)]
    path = os.path.join(ROOT, "runs", "ablation.json")
    out = json.load(open(path)) if os.path.exists(path) else {}
    todo = [int(x) for x in a.only.split(",")] if a.only else range(len(ABLATIONS))
    for i in todo:
        label, change = ABLATIONS[i]
        res = dispatch.sweep(seeds, 8, replace(Policy(), **change), f"abl{i}", a.parallel, False)
        out[label] = {r["seed"]: {"margin": r["marginCents"] / 100, "onTime": r["onTimeShare"], "orders": r["wonOrders"], "ranked": r["ranked"]} for r in res}
        json.dump(out, open(path, "w"), indent=1)
    full = out.get("Full system", {})
    print(f"{'idea':48} {'€/week':>9} {'Δ vs full':>10} {'t':>6} {'on-time mean/min':>17} {'ranked':>7}")
    for label, _ in ABLATIONS:
        r = out.get(label)
        if not r:
            continue
        common = sorted(set(r) & set(full))
        m = st.mean(r[s]["margin"] for s in common)
        d = [r[s]["margin"] - full[s]["margin"] for s in common]
        t = st.mean(d) / (st.stdev(d) / len(d) ** 0.5) if len(d) > 1 and st.stdev(d) > 0 else float("nan")
        ot = [r[s]["onTime"] for s in common]
        print(f"{label:48} {m:>9,.0f} {st.mean(d):>+10,.0f} {t:>6.1f} {st.mean(ot):>8.1%} / {min(ot):.1%} {sum(r[s]['ranked'] for s in common):>4}/{len(common)}")


if __name__ == "__main__":
    main()
