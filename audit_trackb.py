#!/usr/bin/env python3
"""Track-B offline audit for the Bahn/Spread level-8 dispatcher (v9).

Read-only analysis of the archived worlds in data/L8 and the run summaries in runs/.
Creates nothing; prints the four Track-B answers (a)-(d) with numbers.

Run from the worktree root:

    cd "/Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb"
    ./.venv/bin/python audit_trackb.py

Sections
  A  price-grid boundary audit on WON requests (winning quote ratio, cap clipping,
     realised margin per won order, grid-bottom / min-margin-bar diagnostics)
  B  decline-quote audit (4x-list quotes: all-4x requests, decline wins, EUR/week)
  C  day-level adaptation stability (OnlineCompetitor replay vs full-information
     pseudo-truth; oscillation / tracking-error / war-false-alarm metrics)
  D  fee_open=0.82 paired evidence (ev panel runs, step-scale variant, the archived
     unseen vb-1 validation week) and the written decision-log lines
"""
import collections
import csv
import glob
import gzip
import json
import math
import os
import re
import statistics
import sys

WT = os.path.dirname(os.path.abspath(__file__))
os.chdir(WT)
sys.path.insert(0, WT)

import dispatch  # noqa: E402  (installs signal handlers; harmless in a read-only audit)
from quote_model import QuoteModel, OnlineCompetitor, DAY  # noqa: E402

DATA = os.path.join(WT, "data", "L8")
FITS = os.path.join(WT, "data", "fits")
RUNS = os.path.join(WT, "runs")

GRID_TOP = 2.03          # _quote_legacy: x = 0.85 + i*0.02 for i in range(60)
GRID_BOTTOM = 0.85
CAP_TEST = 1.99          # the brief's "clipped at the grid cap" test
STRICT_CAP = 2.02        # true archived cap is 2.03, so >=2.02 means only the top grid point
MIN_MARGIN_EUR = 8.0     # Policy.min_margin (decline bar on expected menu EV)


def q(vals, p):
    """Quantile of a list (no interpolation, cheap and reproducible)."""
    vals = sorted(vals)
    if not vals:
        return float("nan")
    return vals[min(len(vals) - 1, max(0, int(p * len(vals))))]


def read_csv(tag, name):
    with open(os.path.join(DATA, tag, name), newline="") as fh:
        return list(csv.DictReader(fh))


def read_gz(tag, name):
    with gzip.open(os.path.join(DATA, tag, name), "rt") as fh:
        return json.load(fh)


def world_id_of(tag):
    p = os.path.join(DATA, tag, "snapshot.json.gz")
    if not os.path.exists(p):
        return None
    try:
        head = gzip.open(p, "rt").read(400)
    except Exception:
        return None
    m = re.search(r'"worldId": "([^"]+)"', head)
    return m.group(1) if m else None


# ---------------------------------------------------------------- inventory
def load_runs():
    """world id -> {file, seed, result, policy} for every quick_test run summary."""
    by_world = {}
    for f in sorted(glob.glob(os.path.join(RUNS, "*.json"))):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        res = d.get("results") or []
        if not res or "marginCents" not in res[0]:
            continue
        for r in res:
            by_world[r["world"]] = {
                "file": os.path.basename(f), "seed": r["seed"],
                "result": r, "policy": d.get("policy") or {},
            }
    return by_world


def build_dirs():
    """All archive directories, grouped by world id (harvested copies deduped later)."""
    by_world = collections.defaultdict(list)
    for d in sorted(os.listdir(DATA)):
        p = os.path.join(DATA, d)
        if not os.path.isdir(p):
            continue
        w = world_id_of(d)
        by_world[w].append(d)
    return by_world


def prefer_dir(dirs, want_csv=False):
    """Pick the richest directory for one world id: CSVs when needed, reveal/events for replay."""
    if want_csv:
        for d in dirs:
            if os.path.exists(os.path.join(DATA, d, "requests.csv")):
                return d
        return None
    for d in dirs:
        if os.path.exists(os.path.join(DATA, d, "reveal.json.gz")):
            return d
    return None


def _norm(v):
    return str(v).replace("(", "[").replace(")", "]").replace(" ", "")


def policy_class(policy, defaults):
    """Classify a run by its recorded Policy: shipped-v9 exact match / v9 family / other."""
    if not policy:
        return "other"
    diffs = [k for k in policy if k in defaults and _norm(policy[k]) != _norm(defaults[k])]
    if not diffs:
        return "shipped_v9"
    def num(k, default=None):
        v = policy.get(k, default)
        try:
            return float(v) if v is not None else None
        except Exception:
            return default
    if (num("price_mult") == 1.0 and num("comp_from_qm") == 1.0
            and num("hold_hours") == 0.0 and num("use_quote_model") == 0.0):
        return "v9_family"
    return "other"


# ------------------------------------------------------------ per-world parse
def world_econ(tag):
    """Economics of one archived world from its CSVs.

    Returns requests rows, won-order events, driver fee attributed per order (ledger
    driver_fee lines; bundled-gig gig-level fees split by reference-fee share), the
    cancelled set and the ledger-late set (late_refund / undelivered_penalty order ids).
    """
    rows = read_csv(tag, "requests.csv")
    ev = read_csv(tag, "events.csv")
    gigs = {g["id"]: g for g in read_csv(tag, "gigs.csv")}
    refs = {r["id"]: int(r["referenceFeeCents"]) for r in rows}
    won = {}
    cancelled = set()
    late = set()
    fee = collections.Counter()
    for e in ev:
        kind = e["kind"]
        if kind == "order_won":
            won[e["order.id"]] = e
        elif kind == "order_cancelled":
            cancelled.add(e["orderId"])
        elif kind == "ledger":
            k = e["line.kind"]
            oid = e["line.orderId"]
            if k == "driver_fee":
                gid = e["line.gigId"]
                cents = -int(e["line.cents"])
                if oid:
                    fee[oid] += cents
                elif gid in gigs:
                    stops = json.loads(gigs[gid]["stops"])
                    oids = sorted({s["orderId"] for s in stops})
                    tot = sum(refs.get(o, 1) for o in oids) or 1
                    for o in oids:
                        fee[o] += int(round(cents * refs.get(o, 1) / tot))
            elif k in ("late_refund", "undelivered_penalty") and oid:
                late.add(oid)
    return {
        "rows": rows, "won": won, "fee": fee,
        "cancelled": cancelled, "late": late,
    }


def quote_list(row):
    """[(kind, quoteCents, listPriceCents)] for the options quoted in a requests.csv row."""
    out = []
    for k in ("same_day", "next_day", "within_3_days"):
        qv = row.get(k + ".quoteCents") or ""
        if qv:
            out.append((k, int(qv), int(row[k + ".listPriceCents"])))
    return out


def is_decline_quote(row):
    "True if every recorded option quote is 4x list (within 2 cents)."""
    z = quote_list(row)
    return bool(z) and all(abs(q - 4 * l) <= 2 for _, q, l in z)


# ------------------------------------------------------------ A. price grid
def audit_a(worlds, label):
    agg = {
        "worlds": 0, "won": 0, "margin_sum": 0.0, "margins": [],
        "bins": collections.defaultdict(lambda: [0, 0.0]),
        "cap": 0, "cap_prof": 0, "cap_margin": 0.0,
        "strict_cap": 0, "strict_prof": 0,
        "min_ratio": 9.9, "max_ratio": 0.0,
        "bottom_090": 0, "bottom_086": 0,
        "genuine_quotes": 0, "gen_quotes_cap": 0, "gen_quotes_strict": 0,
        "reqs": 0, "decline_reqs": 0, "thin8": 0, "thin0": 0,
        "ontime": 0, "won_not_cancelled": 0,
    }
    for tag in worlds:
        w = world_econ(tag)
        rows, won, fee = w["rows"], w["won"], w["fee"]
        agg["worlds"] += 1
        for r in rows:
            agg["reqs"] += 1
            qs = quote_list(r)
            decl = bool(qs) and all(abs(q - 4 * l) <= 2 for _, q, l in qs)
            if decl:
                agg["decline_reqs"] += 1
            for _, q, l in qs:
                x = q / l
                if x < 3.9:                       # genuine quote, not a 4x decline
                    agg["genuine_quotes"] += 1
                    if x >= CAP_TEST:
                        agg["gen_quotes_cap"] += 1
                    if x >= STRICT_CAP:
                        agg["gen_quotes_strict"] += 1
            if r["outcome"] != "won":
                continue
            kind = r["option"]
            qv = int(r[kind + ".quoteCents"])
            lv = int(r[kind + ".listPriceCents"])
            x = qv / lv
            price = int(won[r["id"]]["order.priceCents"])
            margin = (price - fee.get(r["id"], 0)) / 100.0
            agg["won"] += 1
            agg["margins"].append(margin)
            agg["margin_sum"] += margin
            b = round(int(x * 10) / 10, 1)
            agg["bins"][b][0] += 1
            agg["bins"][b][1] += margin
            agg["min_ratio"] = min(agg["min_ratio"], x)
            agg["max_ratio"] = max(agg["max_ratio"], x)
            if x <= 0.90:
                agg["bottom_090"] += 1
            if x <= 0.86:
                agg["bottom_086"] += 1
            if x >= CAP_TEST:
                agg["cap"] += 1
                agg["cap_margin"] += margin
                if margin > 0:
                    agg["cap_prof"] += 1
            if x >= STRICT_CAP:
                agg["strict_cap"] += 1
                if margin > 0:
                    agg["strict_prof"] += 1
            if margin <= MIN_MARGIN_EUR:
                agg["thin8"] += 1
            if margin <= 0:
                agg["thin0"] += 1
            if r["id"] not in w["cancelled"]:
                agg["won_not_cancelled"] += 1
                if r["id"] not in w["late"]:
                    agg["ontime"] += 1
    n = max(1, agg["won"])
    print("--- (a) price-grid boundary audit :: %s" % label)
    print("  worlds=%d  requests=%d  won=%d  declined_requests=%d (%.1f%%)  mean_margin/win=EUR%.2f median=EUR%.2f"
          % (agg["worlds"], agg["reqs"], agg["won"], agg["decline_reqs"],
             100.0 * agg["decline_reqs"] / max(1, agg["reqs"]),
             agg["margin_sum"] / n, statistics.median(agg["margins"])))
    print("  winning ratio: min=%.4f max=%.4f | quote at cap(>=%.2f)=%d (%.2f%% of wins) profitable=%d (%.2f%%)"
          % (agg["min_ratio"], agg["max_ratio"], CAP_TEST, agg["cap"], 100.0 * agg["cap"] / n,
             agg["cap_prof"], 100.0 * agg["cap_prof"] / n))
    print("  winning ratio at true archived cap (>=%.2f)=%d (%.2f%% of wins) profitable=%d (%.2f%%)"
          % (STRICT_CAP, agg["strict_cap"], 100.0 * agg["strict_cap"] / n,
             agg["strict_prof"], 100.0 * agg["strict_prof"] / n))
    print("  margin earned by cap-clipped wins: EUR%.0f of EUR%.0f total (%.1f%%)"
          % (agg["cap_margin"], agg["margin_sum"], 100.0 * agg["cap_margin"] / max(1e-9, agg["margin_sum"])))
    print("  grid-bottom: wins with ratio<=0.90 = %d (%.2f%%), at literal grid floor 0.85 = %d"
          % (agg["bottom_090"], 100.0 * agg["bottom_090"] / n, agg["bottom_086"]))
    print("  realised revenue-minus-fee margin: <=EUR8 = %d (%.2f%%), <=EUR0 = %d (%.2f%%)"
          % (agg["thin8"], 100.0 * agg["thin8"] / n, agg["thin0"], 100.0 * agg["thin0"] / n))
    print("  genuine quotes (all requests, ratio<3.9): %d | at cap %.2f: %d (%.2f%%) | at >=%.2f: %d (%.2f%%)"
          % (agg["genuine_quotes"], CAP_TEST, agg["gen_quotes_cap"],
             100.0 * agg["gen_quotes_cap"] / max(1, agg["genuine_quotes"]), STRICT_CAP,
             agg["gen_quotes_strict"], 100.0 * agg["gen_quotes_strict"] / max(1, agg["genuine_quotes"])))
    print("  ledger on-time share of won (non-cancelled): %.2f%%  (n=%d)"
          % (100.0 * agg["ontime"] / max(1, agg["won_not_cancelled"]), agg["won_not_cancelled"]))
    print("     ratio-bin   n      share   mean realised margin/win (EUR)   margin sum (EUR)")
    for b in sorted(agg["bins"]):
        c, m = agg["bins"][b]
        print("     %.1f-%.1f  %6d  %6.2f%%   %8.1f                    %10.0f"
              % (b, b + 0.1, c, 100.0 * c / n, m / max(1, c), m))
    return agg


# ------------------------------------------------------ B. decline-quote audit
def audit_b_csv(worlds):
    tot = {"worlds": 0, "reqs": 0, "decline_reqs": 0, "decline_wins": 0,
           "decline_margin": 0.0, "decline_ontime": 0, "mixed_reqs": 0,
           "won_at_4x_any": 0, "won": 0}
    for tag in worlds:
        w = world_econ(tag)
        rows, won, fee = w["rows"], w["won"], w["fee"]
        tot["worlds"] += 1
        for r in rows:
            tot["reqs"] += 1
            qs = quote_list(r)
            if not qs:
                continue
            decl = all(abs(q - 4 * l) <= 2 for _, q, l in qs)
            mixed = (not decl) and any(abs(q - 4 * l) <= 2 for _, q, l in qs)
            if decl:
                tot["decline_reqs"] += 1
            if mixed:
                tot["mixed_reqs"] += 1
            if r["outcome"] != "won":
                continue
            tot["won"] += 1
            kind = r["option"]
            qv = int(r[kind + ".quoteCents"])
            lv = int(r[kind + ".listPriceCents"])
            if qv / lv >= 3.9:
                tot["won_at_4x_any"] += 1
                if decl:
                    tot["decline_wins"] += 1
                    tot["decline_margin"] += (int(won[r["id"]]["order.priceCents"]) - fee.get(r["id"], 0)) / 100.0
                    if r["id"] not in w["cancelled"] and r["id"] not in w["late"]:
                        tot["decline_ontime"] += 1
    print("--- (b) decline-quote audit :: %d archived CSV worlds" % tot["worlds"])
    print("  requests=%d all-4x decline requests=%d (%.2f%%) mixed (some 4x, some genuine)=%d"
          % (tot["reqs"], tot["decline_reqs"], 100.0 * tot["decline_reqs"] / max(1, tot["reqs"]),
             tot["mixed_reqs"]))
    print("  won requests=%d | won on a 4x quote=%d | decline wins (all-4x request AND won on the 4x option)=%d"
          % (tot["won"], tot["won_at_4x_any"], tot["decline_wins"]))
    print("  decline-win margin total=EUR%.2f  avg/order=%s  on-time=%s vs portfolio"
          % (tot["decline_margin"], "n/a" if not tot["decline_wins"] else "%.2f" % (tot["decline_margin"] / tot["decline_wins"]), "n/a"))
    print("  EUR/week added by decline wins = EUR%.2f/world-week (3 dp)" % (tot["decline_margin"] / max(1, tot["worlds"])))
    return tot


def audit_b_reveal(by_world):
    tot = {"worlds": 0, "reqs": 0, "decline_reqs": 0, "won_at_4x": 0, "decline_wins": 0}
    for w, dirs in by_world.items():
        tag = prefer_dir(dirs)
        if tag is None or not os.path.exists(os.path.join(DATA, tag, "reveal.json.gz")):
            continue
        rv = read_gz(tag, "reveal.json.gz")
        tot["worlds"] += 1
        for item in rv.get("requests", []):
            req = item["request"]
            quote = item.get("quote") or {}
            lists = {o["kind"]: o["listPriceCents"] for o in req["options"]}
            z = [(k, v, lists[k]) for k, v in quote.items() if k in lists]
            if not z:
                continue
            tot["reqs"] += 1
            decl = all(abs(v - 4 * l) <= 2 for _, v, l in z)
            if decl:
                tot["decline_reqs"] += 1
            if item.get("outcome") == "won" and item.get("option"):
                k = item["option"]
                if k in lists and quote.get(k) and quote[k] / lists[k] >= 3.9:
                    tot["won_at_4x"] += 1
                    if decl:
                        tot["decline_wins"] += 1
    print("--- (b) decline-quote audit :: ALL %d archived reveal worlds (deduped, incl. other teams' worlds)"
          % tot["worlds"])
    print("  requests=%d all-4x decline requests=%d (%.2f%%) | won on a 4x quote=%d | decline wins=%d"
          % (tot["reqs"], tot["decline_reqs"], 100.0 * tot["decline_reqs"] / max(1, tot["reqs"]),
             tot["won_at_4x"], tot["decline_wins"]))
    return tot


# ------------------------------------------- C. day-level adaptation stability
def replay_filter(tag, qm, road):
    """Replay OnlineCompetitor on one world's archived quotes/outcomes (same update rule as
    dispatch.learn -> oc.observe).  Returns observations [(at, day, mu, day_mu, war_active)]."""
    rv = read_gz(tag, "reveal.json.gz")
    ev = read_gz(tag, "events.json.gz")
    dealer_of = {}
    late = set()
    for e in ev:
        if e["kind"] == "order_won":
            dealer_of[e["order"]["id"]] = e["order"]["dealerId"]
        elif e["kind"] == "ledger":
            ln = e["line"]
            if ln["kind"] in ("late_refund", "undelivered_penalty") and ln.get("orderId"):
                late.add(ln["orderId"])
    settles = sorted(
        (e["at"], e["seq"], dealer_of[e["line"]["orderId"]], e["line"]["orderId"] not in late)
        for e in ev
        if e["kind"] == "ledger" and e["line"]["kind"] == "revenue"
        and e["line"]["orderId"] in dealer_of
    )
    trust = {}
    si = 0
    oc = OnlineCompetitor(qm)
    recs = []
    for item in sorted(rv["requests"], key=lambda x: x["request"]["arrivedAt"]):
        req = item["request"]
        at = req["arrivedAt"]
        while si < len(settles) and settles[si][0] <= at:
            _, _, d, ok = settles[si]
            trust[d] = min(25, trust.get(d, 0) + 1) if ok else trust.get(d, 0) // 2
            si += 1
        lists = {o["kind"]: o["listPriceCents"] for o in req["options"]}
        genuine = {k: v for k, v in (item.get("quote") or {}).items()
                   if k in lists and v < 3.0 * lists[k]}
        if not genuine:                      # exactly as dispatch: decline quotes are not observed
            continue
        ctx = qm.context(req, road)
        oc.observe(ctx, genuine, item.get("outcome") == "won", at, trust.get(req["dealerId"], 0))
        recs.append((at, at // DAY, oc.mu, oc.day_mu, oc.war_active))
    return recs


def oscillation_metrics(recs):
    """Day-end estimates from one replay: total log-comp (mu+day_mu), persistent mu, day_mu."""
    by_day = collections.defaultdict(list)
    for at, d, mu, dm, war in recs:
        by_day[d].append((at, mu, dm, war))
    days = sorted(by_day)
    end = {d: by_day[d][-1] for d in days if len(by_day[d]) >= 5}
    ds = sorted(end)
    tot = {d: end[d][1] + end[d][2] for d in ds}
    return {
        "days": ds, "tot": tot,
        "day_mu": {d: end[d][2] for d in ds},
        "mu": {d: end[d][1] for d in ds},
        "war_days": {d: any(x[3] for x in by_day[d]) for d in ds},
    }


def audit_c_fit(by_world, qm, road):
    truth = json.load(open(os.path.join(FITS, "quotes.json")))
    world_level = truth["world_log_levels"]
    day_effect = truth["day_effect"]
    war_day = truth["world_war_day"]
    war_shift = qm.war_log_shift
    fit_worlds = [w for w in world_level if prefer_dir(by_world.get(w, [])) is not None]
    final_err, err10, err20 = [], [], []
    day_err, total_err = [], []
    dd_est, dd_true, dd_mu = [], [], []
    sign_pairs = sign_flips = 0
    var_ratios = []
    war_detected = war_days = 0
    war_false = nonwar_days = 0
    for w in fit_worlds:
        tag = prefer_dir(by_world[w])
        recs = replay_filter(tag, qm, road)
        if len(recs) < 20:
            continue
        lvl = world_level[w]
        if len(recs) >= 10:
            err10.append(abs(recs[9][2] - lvl))
        if len(recs) >= 20:
            err20.append(abs(recs[19][2] - lvl))
        final_err.append(abs(recs[-1][2] - lvl))
        m = oscillation_metrics(recs)
        ds = m["days"]
        true_day = {d: float(day_effect[w].get(str(d), 0.0)) for d in ds}
        true_tot = {d: lvl + true_day[d] for d in ds}
        eff_day = {}
        for d in ds:
            byd = m["war_days"]
            eff_day[d] = m["day_mu"][d] + (war_shift if m["war_days"][d] else 0.0)
        for d in ds:
            day_err.append(abs(eff_day[d] - true_day[d]))
            total_err.append(abs(m["tot"][d] - true_tot[d]))
        for a, b in zip(ds, ds[1:]):
            if b == a + 1:
                dd_est.append(m["tot"][b] - m["tot"][a])
                dd_true.append(true_tot[b] - true_tot[a])
                dd_mu.append(m["mu"][b] - m["mu"][a])
        signs = [1 if m["tot"][b] - m["tot"][a] > 0 else -1
                 for a, b in zip(ds, ds[1:]) if b == a + 1]
        sign_flips += sum(1 for x, y in zip(signs, signs[1:]) if x != y)
        sign_pairs += max(0, len(signs) - 1)
        if len(ds) >= 3:
            se = statistics.pstdev(list(m["tot"].values()))
            st_true = statistics.pstdev(list(true_tot.values()))
            if st_true > 1e-9:
                var_ratios.append(se / st_true)
        for d in ds:
            if d == war_day[w]:
                war_days += 1
                if m["war_days"][d]:
                    war_detected += 1
            else:
                nonwar_days += 1
                if m["war_days"][d]:
                    war_false += 1
    print("--- (c) day-level adaptation :: OnlineCompetitor replay on %d archived worlds with"
          " full-information pseudo-truth (data/fits/quotes.json world_levels/day_effect)" % len(fit_worlds))
    print("  persistent level error |mu - true_level|: @10 obs median %.3f, @20 obs median %.3f, end median %.3f (p90 %.3f)"
          % (q(err10, .5), q(err20, .5), q(final_err, .5), q(final_err, .9)))
    print("  day-factor error (>=5 obs/day): median %.3f, p90 %.3f (prior day sd = %.3f)"
          % (q(day_err, .5), q(day_err, .9), truth["day"]["sd"]))
    print("  total log-comp error (level+day, end of day): median %.3f, p90 %.3f"
          % (q(total_err, .5), q(total_err, .9)))
    print("  day-over-day |change in fitted total|: median %.4f mean %.4f  vs |true change| median %.4f mean %.4f"
          % (q([abs(v) for v in dd_est], .5), sum(abs(v) for v in dd_est) / len(dd_est),
             q([abs(v) for v in dd_true], .5), sum(abs(v) for v in dd_true) / len(dd_true)))
    print("  day-over-day |change in persistent level mu|: median %.4f p90 %.4f"
          % (q([abs(v) for v in dd_mu], .5), q([abs(v) for v in dd_mu], .9)))
    print("  sign-flip rate of day-over-day fitted changes: %.3f (n pairs=%d; chance for independent days ~0.67)"
          % (sign_flips / max(1, sign_pairs), sign_pairs))
    print("  sd(fitted total per world)/sd(true total per world): median %.2f (n=%d)"
          % (q(var_ratios, .5), len(var_ratios)))
    print("  war detector: war days detected %d/%d (%.1f%%), false alarms %d/%d non-war days (%.2f%%)"
          % (war_detected, war_days, 100.0 * war_detected / max(1, war_days),
             war_false, nonwar_days, 100.0 * war_false / max(1, nonwar_days)))
    return {
        "worlds": len(fit_worlds), "level_err@10": q(err10, .5), "level_err@20": q(err20, .5),
        "level_err_final": q(final_err, .5), "day_err": q(day_err, .5), "day_err_p90": q(day_err, .9),
        "total_err": q(total_err, .5), "dd_est": q([abs(v) for v in dd_est], .5),
        "dd_true": q([abs(v) for v in dd_true], .5), "dd_mu": q([abs(v) for v in dd_mu], .5),
        "sign_flip": sign_flips / max(1, sign_pairs), "var_ratio": q(var_ratios, .5),
        "war_fa": 100.0 * war_false / max(1, nonwar_days),
    }


def audit_c_own(worlds, qm, road):
    """Same filter stability metrics on OUR archived v9-family weeks (no truth: estimate side only)."""
    tot_ch, mu_ch, sign_pairs, sign_flips = [], [], 0, 0
    war_triggers = days_total = 0
    world_sd = []
    for tag in worlds:
        recs = replay_filter(tag, qm, road)
        if len(recs) < 20:
            continue
        m = oscillation_metrics(recs)
        ds = m["days"]
        if len(ds) < 3:
            continue
        tot_ch += [m["tot"][b] - m["tot"][a] for a, b in zip(ds, ds[1:]) if b == a + 1]
        mu_ch += [m["mu"][b] - m["mu"][a] for a, b in zip(ds, ds[1:]) if b == a + 1]
        signs = [1 if m["tot"][b] - m["tot"][a] > 0 else -1 for a, b in zip(ds, ds[1:]) if b == a + 1]
        sign_flips += sum(1 for x, y in zip(signs, signs[1:]) if x != y)
        sign_pairs += max(0, len(signs) - 1)
        world_sd.append(statistics.pstdev(list(m["tot"].values())))
        for d in ds:
            days_total += 1
            if m["war_days"][d]:
                war_triggers += 1
    print("--- (c) same metrics on OUR %d archived v9-family CSV worlds (no truth: estimate-side only)"
          % len(worlds))
    print("  day-over-day |change in fitted total|: median %.4f mean %.4f (n=%d)"
          % (q([abs(v) for v in tot_ch], .5), sum(abs(v) for v in tot_ch) / max(1, len(tot_ch)), len(tot_ch)))
    print("  day-over-day |change in persistent level mu|: median %.4f p90 %.4f"
          % (q([abs(v) for v in mu_ch], .5), q([abs(v) for v in mu_ch], .9)))
    print("  sign-flip rate: %.3f (pairs=%d) | within-world sd of fitted total: median %.4f"
          % (sign_flips / max(1, sign_pairs), sign_pairs, q(world_sd, .5)))
    print("  days with war detector active: %d/%d (%.1f%%)"
          % (war_triggers, days_total, 100.0 * war_triggers / max(1, days_total)))
    return {"tot_ch": q([abs(v) for v in tot_ch], .5), "mu_ch": q([abs(v) for v in mu_ch], .5),
            "sign_flip": sign_flips / max(1, sign_pairs), "war_days": 100.0 * war_triggers / max(1, days_total)}


# ----------------------------------------------------------- D. fee_open=0.82
def run_results(name, seed=None):
    d = json.load(open(os.path.join(RUNS, name)))
    res = d["results"]
    if seed is not None:
        return [r for r in res if r["seed"] == seed][0]
    return res


def audit_d(worlds_csv):
    print("--- (d) fee_open=0.82 evidence")
    base = run_results("q6_m30.json")         # frozen v9 shipped baseline at experiment time
    cand = run_results("q_exp_fee82.json")    # fee_open=0.82, otherwise identical policy
    b = {r["seed"]: r for r in base}
    c = {r["seed"]: r for r in cand}
    print("  paired ev panel (baseline runs/q6_m30.json vs runs/q_exp_fee82.json; only policy diff fee_open 0.9->0.82)")
    print("  seed   base_margin  fee82_margin  dMargin  base_ot  fee82_ot  dOnTime  dRev/o dFee/o")
    tot_d = 0.0
    for s in sorted(b):
        dm = (c[s]["marginCents"] - b[s]["marginCents"]) / 100.0
        tot_d += dm
        dr = (c[s]["revenueCents"] - b[s]["revenueCents"]) / 100.0 / max(1, c[s]["wonOrders"]) - \
             (c[s]["revenueCents"] - b[s]["revenueCents"]) / 100.0 / max(1, b[s]["wonOrders"]) if False else float("nan")
        do = (c[s]["onTimeShare"] - b[s]["onTimeShare"]) * 100.0
        print("  %-5s %11.0f %13.0f %8.0f %8.2f %9.2f %8.2f" % (s, b[s]["marginCents"] / 100.0,
              c[s]["marginCents"] / 100.0, dm, b[s]["onTimeShare"] * 100.0,
              c[s]["onTimeShare"] * 100.0, do))
    print("  mean dMargin = EUR%.0f/wk (4 up, 2 down); mean dOnTime = %+.2f pt"
          % (tot_d / len(b), sum((c[s]["onTimeShare"] - b[s]["onTimeShare"]) * 100.0 for s in b) / len(b)))
    # step_scale variant
    f82 = {r["seed"]: r for r in run_results("q_fee82.json")}
    f82s = {r["seed"]: r for r in run_results("q_fee82s.json")}
    print("  fee82 + fee_step_scale=0.9 (runs/q_fee82s.json vs runs/q_fee82.json): "
          + ", ".join("%s dMargin=%+.0f dOnTime=%+.2f pt" % (
              s, (f82s[s]["marginCents"] - f82[s]["marginCents"]) / 100.0,
              (f82s[s]["onTimeShare"] - f82[s]["onTimeShare"]) * 100.0) for s in sorted(f82)))
    # unseen-panel partial validation: only vb-1 was archived, no runs json
    def econ(tag):
        w = world_econ(tag)
        margin = sum(int(e["line.cents"]) for e in read_csv(tag, "events.csv") if e["kind"] == "ledger") / 100.0
        n = len(w["won"])
        canc = w["cancelled"]
        on = sum(1 for oid in w["won"] if oid not in canc and oid not in w["late"])
        return margin, n, on / max(1, len([o for o in w["won"] if o not in canc])), len([o for o in w["won"] if o not in canc])
    vb82 = econ("vb_fee82-vb-1")
    vbbase = econ("vb_m30-vb-1")
    print("  archived unseen-panel week vb_fee82-vb-1 (complete: snapshot now==endsAt; policy from"
          " q_exp_fee82_gen.sh, no runs json):")
    print("    vb_m30-vb-1 baseline   : margin EUR%.0f  won=%d  on-time %.2f%%  (n=%d)"
          % (vbbase[0], vbbase[1], 100.0 * vbbase[2], vbbase[3]))
    print("    vb_fee82-vb-1 fee 0.82 : margin EUR%.0f  won=%d  on-time %.2f%%  (n=%d)  -> dMargin EUR%+.0f, dOnTime %+.2f pt"
          % (vb82[0], vb82[1], 100.0 * vb82[2], vb82[3], vb82[0] - vbbase[0], (vb82[2] - vbbase[2]) * 100.0))
    print("  archive availability: fee82 worlds on disk = %d (ev panel 6 + ev-1/ev-4 fee82, ev-1/ev-4 step-scale, vb-1);"
          " vb_fee82/vc_fee82 full panel runs/*.json absent -> the planned unseen-panel validation never completed."
          % len([d for d in os.listdir(DATA) if "fee82" in d]))
    print("  written decision log (NOTES-decisions.md D8):")
    for line in open(os.path.join(WT, "NOTES-decisions.md")):
        if "fee_open=0.82" in line or line.strip().startswith("### D8") or "Never validated on vb/vc" in line:
            print("    " + line.rstrip())
    print("  written run log (NOTES-run.md, Experiment 1/1b):")
    for i, line in enumerate(open(os.path.join(WT, "NOTES-run.md")), 1):
        if 308 <= i <= 316 or 356 <= i <= 369:
            print("    " + line.rstrip())


# ------------------------------------------------------------------- main
def main():
    print("# Track-B offline audit - Bahn/Spread level-8 v9 (read-only)")
    print("# command: cd '%s' && ./.venv/bin/python audit_trackb.py" % WT)
    print()
    defaults = {k: _norm(v) for k, v in vars(dispatch.Policy()).items()}
    by_world = build_dirs()
    runs = load_runs()
    csv_worlds = []
    cls_of = {}
    for w, dirs in by_world.items():
        tag = prefer_dir(dirs, want_csv=True)
        if tag is None:
            continue
        rec = runs.get(w)
        cls = policy_class(rec["policy"], defaults) if rec else "unmapped"
        cls_of[tag] = cls
        csv_worlds.append(tag)
    shipped = [t for t in csv_worlds if cls_of[t] == "shipped_v9"]
    v9fam = [t for t in csv_worlds if cls_of[t] in ("shipped_v9", "v9_family")]
    print("archive: %d dirs, %d unique worlds, %d CSV worlds (shipped-v9=%d, v9-family=%d, other/unmapped=%d)"
          % (sum(len(v) for v in by_world.values()), len(by_world), len(csv_worlds), len(shipped), len(v9fam),
             len(csv_worlds) - len(v9fam)))
    print()
    for label, worlds in (("shipped v9 exact-default policy", shipped),
                          ("v9 grid family (pmult=1, comp_from_qm=1, hold=0)", v9fam),
                          ("ALL archived CSV worlds", csv_worlds)):
        audit_a(worlds, label)
        print()
    audit_b_csv(csv_worlds)
    print()
    audit_b_reveal(by_world)
    print()
    qm = QuoteModel.load()
    road = dispatch.Road(json.load(open(os.path.join(FITS, "map.json"))))
    audit_c_fit(by_world, qm, road)
    print()
    audit_c_own(v9fam, qm, road)
    print()
    audit_d(csv_worlds)
    print()
    print("# END - read-only audit; no file written.")


if __name__ == "__main__":
    main()
