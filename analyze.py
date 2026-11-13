"""Archive analysis: per-order P&L, price/win curves, lateness anatomy, world spread.

Reads only data/L8/*.json.gz. No practice tokens.

    python analyze.py            # all sections
    python analyze.py orders     # one section
"""
from __future__ import annotations

import collections
import glob
import gzip
import json
import math
import os
import statistics as st

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
FOLDERS = sorted(glob.glob(os.path.join(DATA, "L8", "*")))
HOUR, DAY = 60, 1440


def get(wid: str, name: str):
    with gzip.open(os.path.join(DATA, "L8", wid, f"{name}.json.gz")) as fh:
        return json.load(fh)


def worlds():
    for folder in FOLDERS:
        yield os.path.basename(folder)


def load(wid: str) -> dict:
    return {
        "reveal": get(wid, "reveal"),
        "events": get(wid, "events"),
        "snapshot": get(wid, "snapshot"),
        "roster": get(wid, "roster"),
    }


def order_econ(w: dict) -> dict[str, dict]:
    """orderId -> money lines and flags, from the ledger + reveal truth."""
    lines: dict[str, dict] = collections.defaultdict(lambda: collections.Counter())
    for e in w["events"]:
        if e["kind"] != "ledger":
            continue
        line = e["line"]
        oid = line.get("orderId")
        if oid:
            lines[oid][line["kind"]] += line["cents"]
    truth = {o["orderId"]: o for o in w["reveal"]["orders"]}
    reqs = {r["request"]["id"]: r for r in w["reveal"]["requests"]}
    out = {}
    for oid, c in lines.items():
        o = truth.get(oid)
        if o is None:
            continue
        req = reqs[oid]
        opt = next((x for x in req["request"]["options"] if x["kind"] == req["option"]), None)
        legs = o["legs"]
        delivered = [l["deliveredAt"] for l in legs]
        deadline = opt["deadline"] if opt else None
        late = any(d is None or (deadline is not None and d > deadline) for d in delivered)
        out[oid] = {
            "order_id": oid, "dealer": req["request"]["dealerId"], "option": req["option"],
            "price": c["revenue"], "driver": -c["driver_fee"], "refund": -c["late_refund"],
            "penalty": -c["undelivered_penalty"], "failed": -c["failed_visit"], "release": -c["release_fee"],
            "waiting": -c["waiting"], "fine": -c["fine"], "net": sum(c.values()),
            "n_legs": len(legs), "delivered_at": delivered,
            "deadline": deadline, "won_at": req["request"]["arrivedAt"],
            "list": opt["listPriceCents"] if opt else None, "reply": req["quote"],
            "slack_h": (opt["deadline"] - req["request"]["respondBy"]) / 60 if opt else None,
            "km0": None,  # filled by lane if wanted
            "late": late,
        }
    return out


def section_orders() -> None:
    rows = []
    score_mismatch = []
    for wid in worlds():
        w = load(wid)
        econ = order_econ(w)
        snap_margin = w["snapshot"]["score"]["marginCents"]
        led = sum(x["net"] for x in econ.values())
        if abs(led - snap_margin) > 5:
            score_mismatch.append((wid, led, snap_margin))
        for x in econ.values():
            x["world"] = wid
            rows.append(x)
    print(f"worlds {len(FOLDERS)}  orders with ledger {len(rows)}  mismatched worlds {len(score_mismatch)}")
    if score_mismatch[:3]:
        print("  first mismatches", score_mismatch[:3])
    by_opt = collections.defaultdict(list)
    for x in rows:
        by_opt[x["option"]].append(x)
    print("\n== per option: n, mean price, mean driver, mean refund, mean net, late share, mean slack h")
    for k, v in sorted(by_opt.items()):
        n = len(v)
        print(f"  {k:<14} n={n:>6}  price={sum(x['price'] for x in v)/n:>7.0f}  driver={sum(x['driver'] for x in v)/n:>7.0f}"
              f"  refund={sum(x['refund'] for x in v)/n:>6.0f}  net={sum(x['net'] for x in v)/n:>7.0f}"
              f"  late={sum(1 for x in v if x['late'])/n:6.1%}  slack={sum(x['slack_h'] or 0 for x in v)/n:>5.1f}")
    print("\n== money lines, all orders (EUR/order)")
    n = len(rows)
    for key in ("price", "driver", "refund", "penalty", "failed", "release", "waiting", "fine", "net"):
        print(f"  {key:<10} {sum(x[key] for x in rows)/n:>8.2f}")
    print("\n== order net distribution (EUR)")
    nets = sorted(x["net"] / 100 for x in rows)
    for q in (0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99):
        print(f"  p{int(q*100):02d} {nets[int(q*(len(nets)-1))]:>9.0f}")
    print("  share of orders with net<0: %.1f%%   share of total net from worst 5%%: %.1f%%" % (
        100 * sum(1 for x in nets if x < 0) / n, 100 * sum(nets[: len(nets) // 20]) / max(1e-9, sum(nets))))
    # per world: worst leak
    print("\n== worlds where refunds+penalties+fees leak most (EUR/week)")
    leaks = []
    for wid in worlds():
        w = load(wid)
        econ = order_econ(w)
        if not econ:
            continue
        rev = sum(x["price"] for x in econ.values())
        fees = sum(x["driver"] for x in econ.values())
        refs = sum(x["refund"] + x["penalty"] for x in econ.values())
        leaks.append((wid, rev, fees, refs, sum(x["net"] for x in econ.values())))
    leaks.sort(key=lambda t: t[3])
    for wid, rev, fees, refs, net in leaks[:8]:
        print(f"  {wid}  rev={rev/100:>8.0f} fees={fees/100:>8.0f} late-cost={refs/100:>8.0f} net={net/100:>8.0f}")
    leaks.sort(key=lambda t: -t[3])
    print("  ... best late-cost worlds")
    for wid, rev, fees, refs, net in leaks[:5]:
        print(f"  {wid}  rev={rev/100:>8.0f} fees={fees/100:>8.0f} late-cost={refs/100:>8.0f} net={net/100:>8.0f}")
    # loss orders
    print("\n== 10 worst single orders")
    for x in sorted(rows, key=lambda x: x["net"])[:10]:
        print(f"  {x['world']} {x['order_id']} {x['option']:<13} price={x['price']/100:>7.0f} driver={x['driver']/100:>7.0f}"
              f" refund={x['refund']/100:>6.0f} pen={x['penalty']/100:>7.0f} net={x['net']/100:>8.0f} slack={x['slack_h']:.1f}h")


def section_quotes() -> None:
    rows = []
    for wid in worlds():
        rv = get(wid, "reveal")
        for r in rv["requests"]:
            q = r["quote"] or {}
            if not q:
                continue
            req = r["request"]
            fast = req["options"][0]
            if fast["kind"] not in q:
                continue
            rows.append(dict(world=wid, x=q[fast["kind"]] / fast["listPriceCents"], won=r["outcome"] == "won",
                             nopt=len(q), day=req["arrivedAt"] // DAY, hour=req["arrivedAt"] % DAY / 60,
                             legs=len(req["legs"]), opt=fast["kind"], arrived=req["arrivedAt"]))
    print(f"quoted requests {len(rows)}  win share {sum(r['won'] for r in rows)/len(rows):.1%}")
    bins = [(0.7, 0.9), (0.9, 1.0), (1.0, 1.1), (1.1, 1.2), (1.2, 1.3), (1.3, 1.4), (1.4, 1.5), (1.5, 1.7), (1.7, 2.0), (2.0, 3.0)]
    print("\n== win rate by x = price/list on fastest option")
    for lo, hi in bins:
        sel = [r for r in rows if lo <= r["x"] < hi]
        if sel:
            print(f"  {lo:.1f}-{hi:.1f}  n={len(sel):>6}  win={sum(r['won'] for r in sel)/len(sel):6.1%}")
    print("\n== win rate by day (all worlds pooled, all x)")
    for d in range(7):
        sel = [r for r in rows if r["day"] == d]
        if sel:
            print(f"  day {d}  n={len(sel):>6}  win={sum(r['won'] for r in sel)/len(sel):6.1%}")
    # per-world: fit rough 50% threshold via logistic on x
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    mids = {}
    for wid in sorted({r["world"] for r in rows}):
        sel = [r for r in rows if r["world"] == wid]
        if len(sel) < 40 or len({r["won"] for r in sel}) < 2:
            continue
        X = np.array([[r["x"]] for r in sel])
        y = np.array([r["won"] for r in sel])
        m = LogisticRegression(C=10).fit(X, y)
        mids[wid] = (-m.intercept_[0] / m.coef_[0][0], len(sel))
    vals = sorted(v[0] for v in mids.values())
    print(f"\n== implied 50%% competitor level per world (n={len(vals)}): "
          f"p10={vals[len(vals)//10]:.2f} median={vals[len(vals)//2]:.2f} p90={vals[9*len(vals)//10]:.2f}")


def section_worlds() -> None:
    rows = []
    for wid in worlds():
        snap = get(wid, "snapshot")
        roster = get(wid, "roster")
        events = get(wid, "events")
        rv = None
        sc = snap["score"]
        homes = collections.Counter(d["homePlaceId"] for d in roster)
        eff_towns = sum(homes.values()) ** 2 / sum(v * v for v in homes.values()) if homes else 0
        forecasts = [e for e in events if e["kind"] == "forecast"]
        price_war_day = None
        # price-war day: the working day with the most order_lost among quoted requests
        lost_by_day = collections.Counter()
        for e in events:
            if e["kind"] == "order_lost":
                lost_by_day[e["at"] // DAY] += 1
        if lost_by_day:
            price_war_day = lost_by_day.most_common(1)[0][0]
        rows.append(dict(world=wid, margin=sc["marginCents"] / 100, won=sc["wonOrders"], ot=sc["onTimeShare"],
                         drivers=len(roster), eff_towns=eff_towns, snow=len(forecasts),
                         top_town_share=max(homes.values()) / len(roster) if roster else 0,
                         lost_by_day=dict(lost_by_day), war_guess=price_war_day))
    print(f"worlds {len(rows)}")
    print("\n== driver count: p0/p10/median/p90: %.0f %.0f %.0f %.0f" % (
        min(r["drivers"] for r in rows), sorted(r["drivers"] for r in rows)[len(rows)//10],
        sorted(r["drivers"] for r in rows)[len(rows)//2], sorted(r["drivers"] for r in rows)[9*len(rows)//10]))
    for key in ("drivers", "eff_towns", "top_town_share"):
        vals = [r[key] for r in rows]
        print(f"  corr({key}, margin/week) = {_corr(vals, [r['margin'] for r in rows]):+.3f}"
              f"  corr({key}, onTime) = {_corr(vals, [r['ot'] for r in rows]):+.3f}")
    print("  corr(won, margin) = %+.3f" % _corr([r["won"] for r in rows], [r["margin"] for r in rows]))
    print("  corr(won, onTime) = %+.3f" % _corr([r["won"] for r in rows], [r["ot"] for r in rows]))
    print("\n== lost-orders by day histogram (pooled), as share of that day's quoted requests")
    tot = collections.Counter()
    evc = collections.Counter()
    for wid in worlds():
        ev = get(wid, "events")
        quoted = sum(1 for e in ev if e["kind"] == "price_request")
        for e in ev:
            if e["kind"] == "order_lost":
                evc[e["at"] // DAY] += 1
        for d in range(7):
            tot[d] += quoted / 7
    for d in range(7):
        print(f"  day {d}  losses={evc[d]:>6}")


def _corr(xs, ys) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n / (sx * sy) if sx and sy else 0.0


if __name__ == "__main__":
    import sys
    which = set(sys.argv[1:]) or {"orders", "quotes", "worlds"}
    for name in ("orders", "quotes", "worlds"):
        if name in which:
            print(f"\n######## {name} ########")
            globals()[f"section_{name}"]()
