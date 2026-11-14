"""Joint parameter search: stochastic hill-climb with common random numbers (the same seeds for every
candidate, so differences are paired). Objective = mean margin, with a hard penalty when any seed's
on-time share falls below the floor. Stops at --until (Helsinki HH:MM) to keep the server free.

    python tune.py --train pr-1,...,nw-4 --parallel 4 --until 13:45
"""
import argparse
import datetime
import json
import os
import random
from dataclasses import asdict, replace
from zoneinfo import ZoneInfo

import dispatch
from dispatch import Policy

ROOT = os.path.dirname(os.path.abspath(__file__))
# (name, low, high): the search space, around the current defaults.
SPACE = [
    ("fee_open", 0.8, 1.0), ("pair_alpha_return", 0.65, 0.85), ("pair_quote", 0.1, 0.35),
    ("late_shadow", 60.0, 110.0), ("pl_scale", 0.45, 0.8), ("min_margin", 3.0, 14.0),
    ("hold_hours", 30.0, 44.0), ("price_slope", 9.0, 12.5), ("day_cap", 8.5, 12.0), ("day_late", 0.5, 1.0),
]


def score(results: list[dict], floor: float) -> tuple[float, float, float]:
    mean = sum(r["marginCents"] for r in results) / len(results) / 100
    worst = min(r["onTimeShare"] for r in results)
    penalty = 0.0 if worst >= floor else 20000 * (floor - worst) * 100
    return mean - penalty, mean, worst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--floor", type=float, default=0.93)
    ap.add_argument("--until", default="13:45")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    seeds = a.train.split(",")
    stop_h, stop_m = map(int, a.until.split(":"))
    rng = random.Random(a.seed)
    log_path = os.path.join(ROOT, "runs", "tune-log.jsonl")
    best = Policy()
    best_res = dispatch.sweep(seeds, 8, best, "tune-base", a.parallel, False)
    best_s = score(best_res, a.floor)
    print(f"BASE obj {best_s[0]:,.0f} mean {best_s[1]:,.0f} worst {best_s[2]:.3f}", flush=True)
    it = 0
    while True:
        now = datetime.datetime.now(ZoneInfo("Europe/Helsinki"))
        if (now.hour, now.minute) >= (stop_h, stop_m):
            break
        it += 1
        cand = best
        for name, lo, hi in rng.sample(SPACE, rng.choice([2, 3])):
            cur = getattr(best, name)
            step = (hi - lo) * rng.uniform(-0.3, 0.3)
            cand = replace(cand, **{name: round(min(hi, max(lo, cur + step)), 3)})
        res = dispatch.sweep(seeds, 8, cand, f"tune-{it}", a.parallel, False)
        if len(res) < len(seeds):
            continue
        s = score(res, a.floor)
        changed = {k: v for k, v in asdict(cand).items() if asdict(best)[k] != v}
        accepted = s[0] > best_s[0]
        with open(log_path, "a") as fh:
            fh.write(json.dumps({"it": it, "changed": changed, "obj": s[0], "mean": s[1], "worst": s[2], "accepted": accepted}) + "\n")
        print(f"it {it}: {changed} -> obj {s[0]:,.0f} mean {s[1]:,.0f} worst {s[2]:.3f} {'ACCEPT' if accepted else ''}", flush=True)
        if accepted:
            best, best_s = cand, s
            with open(os.path.join(ROOT, "runs", "tune-best.json"), "w") as fh:
                json.dump({"policy": asdict(best), "obj": s[0], "mean": s[1], "worst": s[2], "seeds": seeds}, fh, indent=1)
    print(f"BEST obj {best_s[0]:,.0f} mean {best_s[1]:,.0f} worst {best_s[2]:.3f}", flush=True)


if __name__ == "__main__":
    main()
