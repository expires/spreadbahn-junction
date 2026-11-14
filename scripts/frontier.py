"""Risk/return frontier: run a branch of candidate policies on a backtest suite (paired weeks, cached) and
print mean margin, paired gain vs the defaults, worst and 2nd-worst on-time, and the 10th-percentile week.
Used to choose how much lateness risk to take in the final.

    python scripts/frontier.py --suite full
"""
import argparse
import statistics as st

from backtest import SUITES, free_slots, parse_set, run
from bahn.simulation.dispatch import Policy

CANDIDATES = [
    [],                                              # the defaults
    ["fee_mult=0.95"], ["fee_mult=0.9"],
    ["late_shadow=60"], ["late_shadow=60", "fee_mult=0.95"],
    ["private_fee=1.3"], ["private_fee=1.3", "fee_mult=0.95"],
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=SUITES, default="full")
    a = ap.parse_args()
    seeds = SUITES[a.suite]
    base = run(Policy(), seeds, free_slots(), "fr-base")
    print(f"{'candidate':38} {'€/week':>8} {'Δ':>7} {'t':>5} {'worst':>6} {'2nd':>6} {'p10 €':>8} {'<92.5%':>6}")
    for cand in CANDIDATES:
        res = run(parse_set(cand), seeds, free_slots(), "fr-cand") if cand else base
        ks = [s for s in seeds if s in res and s in base]
        m = [res[s]["margin"] for s in ks]
        d = [res[s]["margin"] - base[s]["margin"] for s in ks]
        ot = sorted(res[s]["onTime"] for s in ks)
        t = st.mean(d) / (st.stdev(d) / len(d) ** 0.5) if cand and st.stdev(d) > 0 else 0.0
        print(f"{' '.join(cand) or 'defaults':38} {st.mean(m):>8,.0f} {st.mean(d):>+7,.0f} {t:>+5.1f} {ot[0]:>6.1%} {ot[1]:>6.1%} {sorted(m)[len(m) // 10]:>8,.0f} {sum(o < 0.925 for o in ot):>6}", flush=True)


if __name__ == "__main__":
    main()
