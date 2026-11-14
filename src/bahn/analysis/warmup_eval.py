"""Offline check of the competitor learner's start-of-session behaviour ("learn fast early, then settle").

Replays every saved level-8 week (data/L8/*/reveal.json.gz) through a learner in time order: at each request's
arrival the learner predicts P(win) at the price we actually quoted, at respondBy it sees the outcome (only
requests where we quoted the dealer's fastest option, as the dispatcher learns). Scores are prequential
log-loss by how many outcomes the session has seen so far.

Sessions: a full week from Monday, and a cold start on Thursday (the test finals start mid-week; the learner is
built at Thursday 00:00 with first_day=3). A cheap or dear world is emulated by moving the prior instead of the
truth: `shift` 1.14 means the prior is 14% too high (the 14:00 test final: competitor ~1.2x, prior 1.37x).

    SPREAD_DATA=<repo>/data python -m bahn.analysis.warmup_eval [replay|money|testfinal]

Findings (2026-11-14, 50 full weeks replayed, 28 fitted worlds closed loop):
  * The current learner already gets the level within ~20 decided quotes: in the emulated test final (Thursday
    cold start, competitor 1.17x vs prior 1.37x) its level error is +0.05 over the first 20 outcomes and ~0 after.
    Knowing the level exactly from the first quote is worth only +EUR 33 per 2-day session (prior right: +15);
    the full oracle's +98 is mostly per-lane effects, not the start of the week.
  * Wider priors (sd0 .12, PriorMix tight+wide, decaying warm-up variance) improve first-20 log-loss only when the
    prior is far off, and lose on a price-war Thursday (the war goes into the world level: -EUR 4..11, t -3.4).
  * Practice backtest: regime_mix=0.25 -EUR 326/wk (t -2.9, quick suite). Keep the learner as it is.
"""
from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

from bahn.analysis.regime_eval import PLACES, frame, logloss, req_of
from bahn.models.regime import PriorMix, Regime

DAY = 1440
BUCKETS = [(0, 20), (20, 60), (60, 10**9)]


def replay(g: pd.DataFrame, model, start_day: int = 0) -> pd.DataFrame:
    g = g[g.quoted & g.fast_quoted & (g.day >= start_day)].sort_values("arrived")
    evs = []
    for r in g.itertuples():
        net = r.lr_min - r.lane - math.log1p(0.01 * r.trust)
        evs.append((int(r.arrived), 0, r, net))
        evs.append((int(r.respond), 1, r, net))
    evs.sort(key=lambda e: (e[0], e[1]))
    out, n = [], 0
    for t, kind, r, net in evs:
        req = req_of(r)
        if kind == 0:
            out.append(dict(rid=r.rid, t=t, day=r.day, n=n, p=model.predict(req, net, t), y=float(r.won),
                            level=model.level(t)))
        else:
            model.observe(req, net, bool(r.won), t)
            n += 1
    return pd.DataFrame(out)


def evaluate(df: pd.DataFrame, make, start_day: int, shift: float) -> pd.DataFrame:
    parts = []
    for w, g in df.groupby("world"):
        if g.day.nunique() < 5:
            continue
        m = make(first_day=start_day, level0=math.log(1.37 * shift))
        P = replay(g, m, start_day)
        if P.empty:
            continue
        P["world"] = w
        P["first_day"] = P.day == start_day
        parts.append(P)
    return pd.concat(parts, ignore_index=True)


def summary(P: pd.DataFrame) -> dict:
    out = {}
    for a, b in BUCKETS:
        m = (P.n >= a) & (P.n < b) & P.first_day
        out[f"LL n{a}-{b if b < 10**9 else 'end'} d1"] = logloss(P.p[m], P.y[m])
    out["LL day1"] = logloss(P.p[P.first_day], P.y[P.first_day])
    out["LL rest"] = logloss(P.p[~P.first_day], P.y[~P.first_day])
    out["LL all"] = logloss(P.p, P.y)
    out["bias d1"] = float((P.p[P.first_day] - P.y[P.first_day]).mean())
    out["bias rest"] = float((P.p[~P.first_day] - P.y[~P.first_day]).mean())
    return out


def _mix(sd_wide: float, w: float):
    return lambda **k: PriorMix([Regime(PLACES, **k), Regime(PLACES, sd_level0=sd_wide, **k)], [1 - w, w])


VARIANTS = {
    "today (sd0 .07)": lambda **k: Regime(PLACES, **k),
    "sd0 .12": lambda **k: Regime(PLACES, sd_level0=0.12, **k),
    "mix .20 w.25": _mix(0.20, 0.25),
    "mix .20 w.50": _mix(0.20, 0.50),
    "warm q.03 n40": lambda **k: Regime(PLACES, warm_n=40, warm_q=0.03, **k),
}


def run(variants=None, shifts=(1.0, 1.14, 0.88), starts=(0, 3)) -> pd.DataFrame:
    df = frame()
    rows = []
    for start in starts:
        for shift in shifts:
            for name, mk in (variants or VARIANTS).items():
                rows.append(dict(start="Mon" if start == 0 else "Thu", prior=f"x{shift:.2f}", variant=name,
                                 **summary(evaluate(df, mk, start, shift))))
    return pd.DataFrame(rows)


# ---------- closed loop: what the start-of-session behaviour is worth in euros ----------
def simulate(W: dict, make, start_day: int, truth_mult: float, rng_seed: int, slope: float = 10.5) -> pd.DataFrame:
    """regime_eval.simulate with a session that starts on start_day and a world whose competitor is truth_mult x
    the fitted one (0.88 = a cheap week like the 14:00 test final). Prices at the dispatcher's slope (10.5)."""
    from bahn.analysis.regime_eval import FEE_X, GRID, MIN_MARGIN, SLOPE_TRUE, true_log_comp
    rng = np.random.default_rng(rng_seed)
    reqs = W["reqs"][W["reqs"].day >= start_day]
    places = sorted(set(W["reqs"].pickup) | set(W["reqs"].delivery))
    eff = {(k, p): rng.normal(0, 0.03) for k in ("pickup", "delivery") for p in places}
    u = rng.random(len(reqs))
    lev = make(first_day=start_day)
    evs = []
    for i, r in enumerate(reqs.itertuples()):
        evs.append((int(r.arrived), 0, i, r))
        evs.append((int(r.respond), 1, i, r))
    evs.sort(key=lambda e: (e[0], e[1]))
    quoted, rows, n = {}, [], 0
    for t, kind, i, r in evs:
        req = req_of(r)
        if kind == 1:
            if i in quoted:
                lx, won = quoted.pop(i)
                lev.observe(req, lx, won, t)
                n += 1
            continue
        Lt = true_log_comp(W, r, eff) + math.log(truth_mult)
        lst, fee = r.list_fast, FEE_X * r.ref
        L = Lt if getattr(lev, "oracle", False) else lev.level(t) + lev.adjust(req, t)
        m = GRID * lst - fee
        k = int(np.argmax(m / (1 + np.exp(slope * (np.log(GRID) - L)))))
        if m[k] < MIN_MARGIN:
            rows.append(dict(day=r.day, n=n, quoted=False, p=0.0, em=0.0, err=L - Lt))
            continue
        p = 1 / (1 + math.exp(SLOPE_TRUE * (math.log(GRID[k]) - Lt)))
        quoted[i] = (math.log(GRID[k]), bool(u[i] < p))
        rows.append(dict(day=r.day, n=n, quoted=True, p=p, em=(GRID[k] * lst - fee) * p, err=L - Lt))
    return pd.DataFrame(rows)


_WORLDS: dict = {}


def money(variants: dict, mults=(0.88, 1.0, 1.14), starts=(0, 3), reps: int = 4) -> pd.DataFrame:
    from bahn.analysis.regime_eval import add_auto_events, truth_worlds
    if not _WORLDS:
        df = frame()
        add_auto_events(df)
        _WORLDS.update(truth_worlds(df))
    worlds = _WORLDS
    rows = []
    for start in starts:
        for mult in mults:
            for name, mk in variants.items():
                em1 = emr = w1 = e1 = er = 0.0
                per = []
                for seed, W in worlds.items():
                    tot = 0.0
                    for k in range(reps):
                        S = simulate(W, mk, start, mult, 1000 + k)
                        d1 = S.day == start
                        em1 += S.em[d1].sum(); emr += S.em[~d1].sum(); w1 += S.p[d1].sum()
                        e1 += float(np.abs(S.err[d1]).mean()); er += float(np.abs(S.err[~d1]).mean())
                        tot += S.em.sum()
                    per.append(tot / reps)
                k = len(worlds) * reps
                rows.append(dict(start="Mon" if start == 0 else "Thu", truth=f"x{mult:.2f}", variant=name,
                                 eur_day1=em1 / k, eur_rest=emr / k, eur_total=(em1 + emr) / k, wins_day1=w1 / k,
                                 abs_err_day1=e1 / k, abs_err_rest=er / k, per_world=per))
    return pd.DataFrame(rows)


def paired(M: pd.DataFrame, base: str) -> pd.DataFrame:
    """Each variant's total against `base`, paired by world: mean euros per week and t."""
    out = []
    for (s, t), g in M.groupby(["start", "truth"], sort=False):
        b = np.array(g[g.variant == base].per_world.iloc[0])
        for r in g.itertuples():
            d = np.array(r.per_world) - b
            se = d.std(ddof=1) / math.sqrt(len(d)) if d.std() > 0 else float("nan")
            out.append(dict(start=s, truth=t, variant=r.variant, eur_total=r.eur_total, eur_day1=r.eur_day1,
                            wins_day1=r.wins_day1, d_eur=d.mean(), t=d.mean() / se if se == se else 0.0,
                            abs_err_day1=r.abs_err_day1, abs_err_rest=r.abs_err_rest))
    return pd.DataFrame(out)


def testfinal(variants: dict, level: float = 1.17, war_thursday: bool = False, reps: int = 2) -> pd.DataFrame:
    """The 14:00 test final, emulated on every saved seed's request stream: cold start Thursday 00:00, competitor
    flat at `level` x list (no outage; optionally a 0.8x price war on Thursday). Total and day-one euros per
    variant, paired against the first one."""
    from bahn.analysis.regime_eval import add_auto_events, truth_worlds
    if not _WORLDS:
        df = frame()
        add_auto_events(df)
        _WORLDS.update(truth_worlds(df))
    rows, base = [], None
    for name, mk in variants.items():
        per, d1 = [], 0.0
        for w in _WORLDS.values():
            lv = {d: math.log(level) + (math.log(0.8) if war_thursday and d == 3 else 0.0) for d in range(5)}
            w2 = dict(w, levels=lv, pw=3 if war_thursday else -1, out_day=-1, out_region=set())
            tot = 0.0
            for k in range(reps):
                S = simulate(w2, mk, 3, 1.0, 1000 + k)
                tot += S.em.sum()
                d1 += S.em[S.day == 3].sum()
            per.append(tot / reps)
        per = np.array(per)
        base = per if base is None else base
        d = per - base
        rows.append(dict(variant=name, eur_total=per.mean(), eur_day1=d1 / len(per) / reps, d_eur=d.mean(),
                         t=d.mean() / (d.std(ddof=1) / math.sqrt(len(d))) if d.std() > 0 else 0.0))
    return pd.DataFrame(rows)


def main(argv):
    pd.set_option("display.width", 250)
    cmd = argv[1] if len(argv) > 1 else "replay"
    if cmd == "replay":
        print(run().round(4).to_string(index=False))
    elif cmd == "testfinal":
        print(testfinal(VARIANTS).round(2).to_string(index=False))
        print(testfinal(VARIANTS, level=1.37, war_thursday=True).round(2).to_string(index=False))
    else:
        M = money(VARIANTS, reps=int(argv[2]) if len(argv) > 2 else 4)
        print(paired(M, next(iter(VARIANTS))).round(3).to_string(index=False))


if __name__ == "__main__":
    main(sys.argv)
