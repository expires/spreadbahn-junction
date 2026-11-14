"""Offline replay evaluation of the competitor learners and the outage / price-war detector (regime.py).

    python regime_eval.py events        # identify each saved world's outage and price war (batch fit)
    python regime_eval.py replay        # prequential log-loss / calibration of SGD vs Regime, detection stats
    python regime_eval.py money         # closed-loop pricing simulation on fitted worlds: EUR impact

Replay: every request in time order; at its arrival the learner predicts P(win) at our actual quote,
at respondBy it sees the outcome. Only requests where we quoted the dealer's fastest option count
(the dispatcher learns only from those). No Spread API calls: reads data/L8/*/reveal.json.gz.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

from regime import SGD, Regime
from regime_data import DATA, load_all, regions

DAY = 1440
PLACES = json.load(open(os.path.join(DATA, "map.json")))["places"]

# ---------- 1. events from the batch fit ----------
SLOPE_BATCH = 12.6


def _lvl(x, y, a=0.3, iters=50):
    for _ in range(iters):
        p = 1 / (1 + np.exp(-SLOPE_BATCH * (a + x)))
        a += np.sum(y - p) / (SLOPE_BATCH * np.sum(p * (1 - p)) + 1e-9)
        a = float(np.clip(a, -1, 2))
    return a


def _fit2(x, y, I, iters=40):
    a, dl = 0.3, 0.0
    for _ in range(iters):
        z = SLOPE_BATCH * (a + x + dl * I)
        p = 1 / (1 + np.exp(-z)); w = SLOPE_BATCH ** 2 * p * (1 - p) + 1e-9; g = SLOPE_BATCH * (y - p)
        H = np.array([[w.sum(), (w * I).sum()], [(w * I).sum(), (w * I).sum()]]) + np.eye(2) * 1e-6
        st = np.linalg.solve(H, np.array([g.sum(), (g * I).sum()]))
        a = float(np.clip(a + st[0], -0.5, 1.5)); dl = float(np.clip(dl + st[1], -1, 1))
    z = SLOPE_BATCH * (a + x + dl * I)
    return a, dl, float(np.sum(y * z - np.logaddexp(0, z)))


def frame() -> pd.DataFrame:
    df = load_all()
    df = df[~df.world.str.startswith("starter")]
    df["x"] = df.lane + np.log1p(0.01 * df.trust) - df.lr_min
    return df


def find_events(df: pd.DataFrame) -> pd.DataFrame:
    """Per seed (pooling the policy runs of one seed): price-war day = lowest day level; outage =
    (day, center) with the largest pooled likelihood-ratio of a pickup-region shift 08:00-18:00."""
    R = regions()
    uniq = {}
    for c, reg in sorted(R.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        uniq.setdefault(frozenset(reg), c)
    q = df[df.quoted]
    full = q.groupby("world").day.nunique()
    q = q[q.world.isin(full[full == 5].index)]
    rows = []
    for seed, gs in q.groupby("seed"):
        lv = {d: np.median([_lvl(g.x.values, g.won.values.astype(float)) for _, g in gd.groupby("world")])
              for d, gd in gs.groupby("day")}
        med = np.median(list(lv.values()))
        srt = sorted(lv, key=lv.get)
        lr_tab = defaultdict(float)
        for (w, d), g in gs.groupby(["world", "day"]):
            x, y = g.x.values, g.won.values.astype(float)
            ll0 = float(np.sum(y * SLOPE_BATCH * (_lvl(x, y) + x) - np.logaddexp(0, SLOPE_BATCH * (_lvl(x, y) + x))))
            t = g.arrived.values % DAY
            win = (t >= 480) & (t < 1080)
            for reg, c in uniq.items():
                I = (g.pickup.isin(reg).values & win).astype(float)
                if I.sum() < 4:
                    continue
                _, dl, ll1 = _fit2(x, y, I)
                if dl > 0:
                    lr_tab[(d, c)] += 2 * (ll1 - ll0)
        (od, oc), LR = max(lr_tab.items(), key=lambda kv: kv[1])
        second = max((v for (d, c), v in lr_tab.items() if d != od), default=0.0)
        rows.append(dict(seed=seed, runs=gs.world.nunique(), pw_day=srt[0], pw_ratio=round(math.exp(lv[srt[0]] - med), 3),
                         next_ratio=round(math.exp(lv[srt[1]] - med), 3), outage_day=od, outage_center=oc,
                         outage_LR=round(LR, 1), other_day_LR=round(second, 1),
                         levels=" ".join(f"{math.exp(lv[d]):.2f}" for d in sorted(lv))))
    return pd.DataFrame(rows)


# Best reading of each saved seed (find_events, checked by hand per pickup town; ex-4 and ev-6 outages are weak).
EVENTS = {
    "ex-1": dict(pw=0, out=(3, "helsinki")), "ex-2": dict(pw=1, out=(4, "hyvinkaa")),
    "ex-3": dict(pw=1, out=(2, "oulu")), "ex-4": dict(pw=4, out=(1, "mikkeli")),
    "ex-5": dict(pw=0, out=(1, "helsinki")), "ex-6": dict(pw=1, out=(3, "oulu")),
    "ev-1": dict(pw=1, out=(3, "tampere")), "ev-2": dict(pw=0, out=(4, "hyvinkaa")),
    "ev-3": dict(pw=4, out=(3, "tampere")), "ev-4": dict(pw=4, out=(2, "helsinki")),
    "ev-5": dict(pw=0, out=(4, "helsinki")), "ev-6": dict(pw=3, out=(4, "vaasa")),
    "ho-3": dict(pw=0, out=(1, "kuopio")), "ho-5": dict(pw=2, out=(4, "hameenlinna")),
}
TUNE = set(EVENTS)   # the regime parameters were tuned on these seeds; any other seed is held out


def add_auto_events(df: pd.DataFrame, min_lr: float = 25.0) -> None:
    """Seeds saved after tuning: price war = lowest day; outage only when the batch fit is clear."""
    for r in find_events(df[~df.seed.isin(list(EVENTS))]).itertuples():
        clear = r.outage_LR >= min_lr and r.outage_LR >= 1.5 * r.other_day_LR
        EVENTS[r.seed] = dict(pw=int(r.pw_day), out=(int(r.outage_day), r.outage_center) if clear else None)


# ---------- 2. prequential replay ----------
def req_of(r) -> dict:
    return {"legs": [{"pickup": {"placeId": r.pickup}, "delivery": {"placeId": r.delivery}}], "arrivedAt": int(r.arrived), "respondBy": int(r.respond),
            "dealerId": r.dealer}


def replay(g: pd.DataFrame, model) -> pd.DataFrame:
    """Predict at arrival, learn at respondBy. Returns per-request predictions and model states."""
    g = g[g.quoted & g.fast_quoted].sort_values("arrived")
    evs = []
    for r in g.itertuples():
        net = r.lr_min - r.lane - math.log1p(0.01 * r.trust)
        evs.append((int(r.arrived), 0, r, net))
        evs.append((int(r.respond), 1, r, net))
    evs.sort(key=lambda e: (e[0], e[1]))
    out = []
    for t, kind, r, net in evs:
        req = req_of(r)
        if kind == 0:
            p = model.predict(req, net, t)
            st = dict(p_pw=model.p_price_war(t), p_down=model.p_down(r.pickup)) if isinstance(model, Regime) else {}
            out.append(dict(rid=r.rid, t=t, day=r.day, pickup=r.pickup, p=p, y=float(r.won), lr=r.lr_min,
                            level=model.level(t), adj=model.adjust(req, t), **st))
        else:
            model.observe(req, net, bool(r.won), t)
    P = pd.DataFrame(out)
    if isinstance(model, Regime):
        model._roll(int(g.arrived.max()) // DAY * DAY + DAY)   # close the last day
        P.attrs["history"] = model.history
    return P


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def truth_cols(P: pd.DataFrame, seed: str) -> pd.DataFrame:
    R = regions()
    ev = EVENTS.get(seed)
    if ev is None:
        P["is_pw"] = False; P["is_out"] = False
        return P
    t = P.t % DAY
    win = (t >= 480) & (t < 1080)
    P["is_pw"] = (P.day == ev["pw"]) & win
    P["is_out"] = ((P.day == ev["out"][0]) & P.pickup.isin(R[ev["out"][1]]) & win) if ev["out"] else False
    return P


MODELS = {
    "sgd(current)": lambda: SGD(),
    "regime": lambda: Regime(PLACES),
    "regime-no-outage": lambda: Regime(PLACES, outage_shift=0.0),
    "regime-no-pw": lambda: Regime(PLACES, pw_shift=0.0),
}


HIST: dict = {}


def run_replay(df: pd.DataFrame, models: dict) -> dict:
    res = {}
    for name, mk in models.items():
        parts = []
        for w, g in df.groupby("world"):
            P = replay(g, mk())
            P["world"], P["seed"] = w, g.seed.iloc[0]
            hist = P.attrs.get("history")
            parts.append(truth_cols(P, g.seed.iloc[0]))
            if hist:
                HIST[(name, w)] = hist
        res[name] = pd.concat(parts, ignore_index=True)
    return res


def report_replay(res: dict) -> None:
    for label, keep in (("ALL worlds", None), ("TUNING seeds", True), ("HELD-OUT seeds", False)):
        print(f"== {label}")
        _report_replay({k: (P if keep is None else P[P.seed.isin(list(TUNE)) == keep]) for k, P in res.items()})


def _report_replay(res: dict) -> None:
    print(f"{'model':22s} {'n':>6s} {'logloss':>8s} {'brier':>7s} {'LL pw-day':>9s} {'LL outage':>9s} {'LL other':>9s} {'LL next-day':>11s}")
    for name, P in res.items():
        nd = np.zeros(len(P), bool)
        for s, ev in EVENTS.items():
            nd |= (P.seed == s).values & (P.day == ev["pw"] + 1).values
        oth = ~P.is_pw & ~P.is_out & ~nd
        print(f"{name:22s} {len(P):6d} {logloss(P.p, P.y):8.4f} {np.mean((P.p - P.y) ** 2):7.4f} "
              f"{logloss(P.p[P.is_pw], P.y[P.is_pw]):9.4f} {logloss(P.p[P.is_out], P.y[P.is_out]):9.4f} "
              f"{logloss(P.p[oth], P.y[oth]):9.4f} {logloss(P.p[nd], P.y[nd]):11.4f}")
    for name, P in res.items():
        b = pd.cut(P.p, [0, .2, .4, .6, .8, 1.0])
        cal = P.groupby(b, observed=True).agg(pred=("p", "mean"), obs=("y", "mean"), n=("y", "size")).round(3)
        print(name, "calibration:", "; ".join(f"{r.pred:.2f}->{r.obs:.2f} ({r.n})" for r in cal.itertuples()))


def detection(P: pd.DataFrame, model: str = "regime", thr: float = 0.5) -> pd.DataFrame:
    """Per full-week run: when the alarms rise on the event day (minutes after 08:00, and requests of that
    day / of the region seen before), the end-of-day price-war call, and alarms outside the true events."""
    R = regions()
    rows = []
    for w, g in P.groupby("world"):
        seed = g.seed.iloc[0]
        ev = EVENTS.get(seed)
        if ev is None or g.day.nunique() < 5:
            continue
        od, oc = ev["out"] or (-1, None)
        g = g.sort_values("t")
        tod = g.t % DAY
        pw = g[(g.day == ev["pw"]) & (tod < 1080)]
        hit = pw[pw.p_pw > thr]
        hist = HIST.get((model, w), [])
        eod = {h["day"]: h["p_pw"] for h in hist}
        o = g[(g.day == od) & g.pickup.isin(R[oc]) & (tod < 1080) & (tod >= 480)] if oc else g.iloc[:0]
        ho = o[o.p_down > thr]
        fa = g[(g.p_down > thr) & ~g.is_out] if oc else g.iloc[:0]
        rows.append(dict(
            world=w, set="tune" if seed in TUNE else "held-out", pw_day=ev["pw"],
            pw_alarm_min=(hit.t.iloc[0] % DAY - 480) if len(hit) else None,
            pw_alarm_reqs=int((pw.t < hit.t.iloc[0]).sum()) if len(hit) else None,
            pw_eod=" ".join(f"{eod.get(d, float('nan')):.2f}" for d in range(5)),
            pw_eod_ok=bool(eod) and max(eod, key=eod.get) == ev["pw"],
            pw_eod_false=sum(1 for d, p in eod.items() if d != ev["pw"] and p > thr),
            out=f"d{od} {oc}" if oc else "unclear", out_n=len(o),
            out_alarm_min=(ho.t.iloc[0] % DAY - 480) if len(ho) else None,
            out_alarm_reqs=int((o.t < ho.t.iloc[0]).sum()) if len(ho) else None,
            out_covered=round(float((o.p_down > thr).mean()), 2) if len(o) else float("nan"),
            out_eod=" ".join(f"{h['p_outage']:.2f}" for h in hist),
            false_alarm_reqs=len(fa), n_reqs=len(g),
            false_alarm_where=sorted(set(zip(fa.day, fa.pickup)))[:5]))
    return pd.DataFrame(rows)


def report_detection(d: pd.DataFrame) -> None:
    print(d.drop(columns=["false_alarm_where"]).to_string(index=False))
    for label, x in (("TUNING", d[d.set == "tune"]), ("HELD-OUT", d[d.set != "tune"]), ("ALL", d)):
        print(f"== {label} ({len(x)} full-week runs)")
        _report_detection(x)


def _report_detection(d: pd.DataFrame) -> None:
    big = d[d.out_n >= 30]
    small = d[(d.out_n > 0) & (d.out_n < 30)]
    for name, x in (("big-region outages (>=30 requests)", big), ("small-region outages", small)):
        if len(x):
            hit = x.dropna(subset=["out_alarm_min"])
            print(f"{name}: {len(x)} runs, alarmed in {len(hit)}, median first alarm {hit.out_alarm_min.median():.0f} min after "
                  f"08:00 / after {hit.out_alarm_reqs.median():.0f} region requests; request-weighted coverage "
                  f"{(x.out_covered * x.out_n).sum() / x.out_n.sum():.0%}")
    k = d[d.out != "unclear"]
    print(f"outage false alarms (runs with a known outage): {k.false_alarm_reqs.sum()} of {k.n_reqs.sum()} requests "
          f"({k.false_alarm_reqs.sum() / max(1, k.n_reqs.sum()):.2%}), {k.false_alarm_reqs.mean():.1f} per week")
    hit = d.dropna(subset=["pw_alarm_min"])
    print(f"price war: alarm (p>0.5) in {len(hit)}/{len(d)} runs, median {hit.pw_alarm_min.median():.0f} min after 08:00 "
          f"/ {hit.pw_alarm_reqs.median():.0f} requests into the day; end-of-day argmax right in {d.pw_eod_ok.sum()}/{len(d)}; "
          f"other days ending with p>0.5: {d.pw_eod_false.sum()} in {len(d)} weeks")


# ---------- 3. money: closed-loop pricing on fitted worlds ----------
SLOPE_TRUE = float(os.environ.get("SLOPE_TRUE", 12.6))   # dealer slope given the day level (batch fit)
FEE_X = 1.15       # expected driver fee, x the typical fee (the dispatcher's 16-30 h slack band)
MIN_MARGIN = 8.0
GRID = np.array([0.85 + 0.02 * i for i in range(60)])
SOURCE = {"ev-1": "v6-ev-1", "ev-2": "v6-ev-2", "ev-3": "v6-ev-3", "ev-4": "v6-ev-4", "ev-5": "v6-ev-5",
          "ev-6": "v6-ev-6", "ho-3": "v7-ho-3", "ho-5": "v8off-ho-5"}   # else: the seed's first full-week run


def truth_worlds(df: pd.DataFrame) -> dict:
    """Per seed: its request stream and the fitted truth (day levels with the price war in them, the outage)."""
    R = regions()
    q = df[df.quoted]
    out = {}
    for seed, ev in EVENTS.items():
        od, oc = ev["out"] or (-1, None)
        gs = q[q.seed == seed]
        full = gs.groupby("world").day.nunique()
        gs = gs[gs.world.isin(full[full == 5].index)]
        t = gs.arrived % DAY
        o = ((gs.day == od) & gs.pickup.isin(R[oc]) & (t >= 480) & (t < 1080)) if oc else np.zeros(len(gs), bool)
        x = gs.x + np.where(o, math.log(1.3), 0.0)
        lv = {d: float(np.median([_lvl(x[(gs.world == w) & (gs.day == d)].values,
                                       gs.won[(gs.world == w) & (gs.day == d)].values.astype(float))
                                  for w in gs.world.unique()])) for d in range(5)}
        src = SOURCE.get(seed) or sorted(gs.world.unique())[-1]
        reqs = df[df.world == src].sort_values("arrived")
        out[seed] = dict(levels=lv, pw=ev["pw"], out_day=od, out_region=R[oc] if oc else set(), reqs=reqs)
    return out


def true_log_comp(W: dict, r, eff: dict) -> float:
    t = r.arrived % DAY
    inwin = 480 <= t < 1080
    L = W["levels"][r.day]
    if r.day == W["pw"] and not inwin:
        L -= math.log(0.8)                       # the war is 08:00-18:00 only
    if r.day == W["out_day"] and inwin and r.pickup in W["out_region"]:
        L += math.log(1.3)
    return L + eff.get(("pickup", r.pickup), 0.0) + eff.get(("delivery", r.delivery), 0.0)


def simulate(W: dict, pol: dict, rng_seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(rng_seed)
    places = sorted(set(W["reqs"].pickup) | set(W["reqs"].delivery))
    eff = {(k, p): rng.normal(0, 0.03) for k in ("pickup", "delivery") for p in places}
    u = rng.random(len(W["reqs"]))
    lev, reg = pol["make"]()
    evs = []
    for i, r in enumerate(W["reqs"].itertuples()):
        evs.append((int(r.arrived), 0, i, r))
        evs.append((int(r.respond), 1, i, r))
    evs.sort(key=lambda e: (e[0], e[1]))
    quoted, rows = {}, []
    for t, kind, i, r in evs:
        req = req_of(r)
        if kind == 1:
            if i in quoted:
                lx, won = quoted.pop(i)
                lev.observe(req, lx, won, t)
                if reg is not None and reg is not lev:
                    reg.observe(req, lx, won, t)
            continue
        Lt = true_log_comp(W, r, eff)
        lst, fee = r.list_fast, FEE_X * r.ref
        if pol.get("oracle"):
            L, s = Lt, SLOPE_TRUE
        else:
            L = lev.level(t) + (reg.adjust(req, t) if reg is not None else 0.0)
            s = pol["slope"] if pol.get("slope") else lev.pricing_slope(req, t)
        m = GRID * lst - fee
        ev_ = m / (1 + np.exp(s * (np.log(GRID) - L)))
        k = int(np.argmax(ev_))
        sit = pol.get("sitout") and isinstance(lev, Regime) and lev.p_price_war(t) > 0.5 and Regime.in_window(t)
        if m[k] < MIN_MARGIN or sit:
            rows.append(dict(day=r.day, t=t, pickup=r.pickup, quoted=False, x=np.nan, p=0.0, em=0.0, won=False, L=L, Lt=Lt))
            continue
        x = GRID[k]
        p = 1 / (1 + math.exp(SLOPE_TRUE * (math.log(x) - Lt)))
        won = bool(u[i] < p)
        quoted[i] = (math.log(x), won)
        rows.append(dict(day=r.day, t=t, pickup=r.pickup, quoted=True, x=x, p=p, em=(x * lst - fee) * p, won=won, L=L, Lt=Lt))
    return pd.DataFrame(rows)


POLICIES = {
    "current (SGD, slope 8.7)": dict(make=lambda: (SGD(), None), slope=8.7),
    "SGD, slope 12": dict(make=lambda: (SGD(slope=12.0), None), slope=12.0),
    "SGD + outage adjust": dict(make=lambda: (SGD(), Regime(PLACES, effects={})), slope=8.7),
    "Regime, slope 8.7": dict(make=lambda: ((r := Regime(PLACES)), r), slope=8.7),
    "Regime, slope 10.5": dict(make=lambda: ((r := Regime(PLACES)), r), slope=10.5),
    "Regime no outage": dict(make=lambda: ((r := Regime(PLACES, outage_shift=0.0)), r)),
    "Regime": dict(make=lambda: ((r := Regime(PLACES)), r)),
    "Regime switch-mode outage": dict(make=lambda: ((r := Regime(PLACES, outage_mode="switch")), r)),
    "Regime + sit out war": dict(make=lambda: ((r := Regime(PLACES)), r), sitout=True),
    "oracle (true level)": dict(make=lambda: (SGD(), None), oracle=True),
}


def money(df: pd.DataFrame, reps: int = 6, policies: dict = POLICIES) -> pd.DataFrame:
    worlds = truth_worlds(df)
    rows = []
    for seed, W in worlds.items():
        for name, pol in policies.items():
            for k in range(reps):
                S = simulate(W, pol, 1000 + k)
                tod = S.t % DAY
                inw = (tod >= 480) & (tod < 1080)
                seg = np.where((S.day == W["out_day"]) & S.pickup.isin(W["out_region"]) & inw, "outage",
                               np.where(S.day == W["pw"], "pw_day", np.where(S.day == W["pw"] + 1, "pw_next", "other")))
                for sg in ("outage", "pw_day", "pw_next", "other"):
                    m = seg == sg
                    rows.append(dict(seed=seed, policy=name, rep=k, seg=sg, n=int(m.sum()), em=S.em[m].sum(),
                                     wins=S.p[m].sum(), quoted=int(S.quoted[m].sum()),
                                     bias=float((S.L[m] - S.Lt[m]).mean()) if m.any() else np.nan))
    return pd.DataFrame(rows)


def report_money(M: pd.DataFrame) -> None:
    for label, keep in (("TUNING seeds", True), ("HELD-OUT seeds", False)):
        print(f"== {label}")
        _report_money(M[M.seed.isin(list(TUNE)) == keep], brief=True)
    print("== ALL seeds")
    _report_money(M)


def _report_money(M: pd.DataFrame, brief: bool = False) -> None:
    per = M.groupby(["policy", "seed", "seg"]).agg(em=("em", "mean"), wins=("wins", "mean"), n=("n", "mean"),
                                                   bias=("bias", "mean")).reset_index()
    order = list(dict.fromkeys(M.policy))
    tot = per.groupby(["policy", "seg"]).agg(em=("em", "sum"), wins=("wins", "sum"), n=("n", "sum"), bias=("bias", "mean")).reset_index()
    ns = M.seed.nunique()
    print(f"Expected margin per week (EUR, mean of {ns} fitted worlds x {M.rep.nunique()} draws), by segment; "
          f"bias = mean(believed - true log level)")
    piv = tot.pivot(index="policy", columns="seg", values="em").loc[order] / ns
    piv["total"] = piv.sum(axis=1)
    base = piv.loc[order[0], "total"]
    piv["vs current"] = piv.total - base
    print(piv.round(0).to_string())
    if brief:
        return
    b = tot.pivot(index="policy", columns="seg", values="bias").loc[order]
    print("level bias (log):"); print(b.round(3).to_string())
    w = tot.pivot(index="policy", columns="seg", values="wins").loc[order] / ns
    print("expected wins per week:"); print(w.round(1).to_string())
    n = tot.pivot(index="policy", columns="seg", values="n").loc[order].iloc[0] / ns
    print("requests per week by segment:", n.round(0).to_dict())
    tw = per.groupby(["policy", "seed"]).em.sum().unstack(0)[order]
    print("per world total:"); print(tw.round(0).to_string())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "replay"
    pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 80)
    df = frame()
    add_auto_events(df)
    if cmd == "events":
        print(find_events(df).to_string(index=False))
    elif cmd == "replay":
        res = run_replay(df, MODELS)
        report_replay(res)
        report_detection(detection(res["regime"]))
    elif cmd == "money":
        report_money(money(df, reps=int(sys.argv[2]) if len(sys.argv) > 2 else 6))
