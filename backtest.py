"""One-command backtest: a candidate policy against the current defaults, on a fixed bank of level-8 weeks,
paired by week (same seeds), with a ship / don't-ship verdict. The baseline is cached by a hash of the
default policy, so only the candidate runs.

    python backtest.py --suite quick --set fee_mult=0.95            # 8 weeks, ~3 min
    python backtest.py --suite full  --set late_shadow=60           # 24 weeks
    python backtest.py --suite full                                 # (re)build the baseline cache only

Verdict SHIP needs: mean gain > 0 with t ≥ 1.5, worst week ≥ 92.5% on time, and no week more than 1.5 points
below its own baseline on-time when that baseline week is already under 95% (the fat tail is what eliminates).
"""
import argparse
import hashlib
import json
import os
import statistics as st
from dataclasses import asdict, replace

import dispatch
from dispatch import Policy

ROOT = os.path.dirname(os.path.abspath(__file__))
STRESS = ["st-21", "st-38", "st-32", "st-25", "st-6", "st-1", "ho-3", "ho-4"]  # worst of 40 scanned + flu weeks
SUITES = {
    "quick": [f"bt-{i}" for i in range(1, 7)] + ["ho-3", "st-25"],
    "full": [f"bt-{i}" for i in range(1, 17)] + STRESS,
    "stress": STRESS,
}


def parse_set(pairs):
    pol = Policy()
    for kv in pairs:
        k, v = kv.split("=", 1)
        cur, val = getattr(pol, k), json.loads(v)
        val = tuple(tuple(x) if isinstance(x, list) else x for x in val) if isinstance(cur, tuple) else type(cur)(val)
        pol = replace(pol, **{k: val})
    return pol


def key(pol: Policy) -> str:
    return hashlib.sha1(json.dumps(asdict(pol), sort_keys=True, default=str).encode()).hexdigest()[:10]


def run(pol: Policy, seeds, parallel, tag) -> dict:
    cache = os.path.join(ROOT, "runs", f"bt-{key(pol)}.json")
    have = json.load(open(cache)) if os.path.exists(cache) else {}
    todo = [s for s in seeds if s not in have]
    if todo:
        for r in dispatch.sweep(todo, 8, pol, tag, parallel, False):
            have[r["seed"]] = {"margin": r["marginCents"] / 100, "onTime": r["onTimeShare"], "orders": r["wonOrders"]}
        json.dump(have, open(cache, "w"), indent=1)
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=SUITES, default="quick")
    ap.add_argument("--set", nargs="*", default=[])
    ap.add_argument("--parallel", type=int, default=4)
    a = ap.parse_args()
    seeds = SUITES[a.suite]
    base = run(Policy(), seeds, a.parallel, "bt-base")
    if not a.set:
        print(f"baseline cached for {len(base)} weeks: mean €{st.mean(base[s]['margin'] for s in seeds):,.0f}")
        return
    cand = run(parse_set(a.set), seeds, a.parallel, "bt-cand")
    rows, d = [], []
    for s in seeds:
        if s not in cand or s not in base:
            continue
        b, c = base[s], cand[s]
        d.append(c["margin"] - b["margin"])
        flag = " ⚠" if c["onTime"] < 0.925 or (b["onTime"] < 0.95 and c["onTime"] < b["onTime"] - 0.015) else ""
        rows.append(f"  {s:6} €{c['margin']:>8,.0f} {c['onTime']:6.1%}  vs  €{b['margin']:>8,.0f} {b['onTime']:6.1%}   Δ €{c['margin'] - b['margin']:>+7,.0f}{flag}")
    print(f"candidate {a.set} on suite '{a.suite}' ({len(d)} weeks), level 8:")
    print("\n".join(rows))
    mean = st.mean(d)
    t = mean / (st.stdev(d) / len(d) ** 0.5) if len(d) > 1 and st.stdev(d) > 0 else 0.0
    worst = min(cand[s]["onTime"] for s in seeds if s in cand)
    tail = sum(1 for r in rows if "⚠" in r)
    ok = mean > 0 and t >= 1.5 and worst >= 0.925 and tail == 0
    print(f"mean Δ €{mean:+,.0f}/week  t={t:+.2f}  worst on-time {worst:.1%}  tail flags {tail}  ->  {'SHIP' if ok else 'DO NOT SHIP'}")


if __name__ == "__main__":
    main()
