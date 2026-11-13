"""Measure notes.parse_note against the reveal ground truth (and the kit's keyword baseline).

    .venv/bin/python eval_notes.py [--data DIR] [--worst N] [--field FIELD]

Ground truth: every won order in data/L8/*/reveal.json.gz, deduplicated by (folder, orderId).
Only leg 0 carries note effects (leg 1 of a two-leg request is the trade-in returning).

For each field we compare the EFFECTIVE value (what the dispatcher would plan with) after
combining the request with the parsed note against the truth the world used:

  ready      max(pickup.window.from, ready_from)        vs max(window.from, readyAt, releasedAt)
  p/d hours  max(stated.opens, opens), min(stated.closes, closes)  vs truth pickupHours/deliveryHours
  deadline   min(won option deadline, deadline)         vs min(option deadline, noteDeadline)
  flags      manual / plates / summer_tyres             vs manualGearbox / needsPlates / summerTyres

"danger" = the error that causes a failed visit or a late car:
  ready predicted too early, closing predicted too late, opening predicted too early,
  deadline predicted too late, flag missed.
"waste"  = the safe-side error (we wait / hurry for nothing / flag a car that does not need it).
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notes import DAY, parse_note  # noqa: E402

DEFAULT_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "L8")
if not os.path.isdir(DEFAULT_DATA):
    DEFAULT_DATA = "/Users/manu/Developer/Hackathons/bahn/data/L8"

try:
    from spread.starter import window_from_note as kit_window_from_note
except Exception:  # pragma: no cover
    kit_window_from_note = None

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fmt(m):
    if m is None:
        return "-"
    return f"{WD[(m // DAY) % 7]}{m // DAY}.{(m % DAY) // 60:02d}:{m % 60:02d}"


def hm(m):
    return "-" if m is None else f"{m // 60:02d}:{m % 60:02d}"


def load(data_dir):
    rows, seen = [], set()
    for f in sorted(glob.glob(os.path.join(data_dir, "*", "reveal.json.gz"))):
        folder = os.path.basename(os.path.dirname(f))
        with gzip.open(f) as fh:
            r = json.load(fh)
        reqs = {x["request"]["id"]: x for x in r["requests"]}
        for o in r["orders"]:
            key = (folder, o["orderId"])
            if key in seen or o["orderId"] not in reqs:
                continue
            seen.add(key)
            q = reqs[o["orderId"]]
            rows.append({"folder": folder, "req": q["request"], "option": q.get("option"), "truth": o})
    return rows


def option_deadline(req, option):
    opts = {o["kind"]: o["deadline"] for o in req["options"]}
    if option in opts:
        return opts[option]
    return max(opts.values())


class Stat:
    def __init__(self, name, unit="min"):
        self.name, self.unit = name, unit
        self.n = self.pos = self.exact = self.danger = self.waste = 0
        self.tp = self.fp = self.fn = 0  # "changed vs stated" detection
        self.abs_err = 0

    def add(self, stated, truth, pred, danger_if_pred_less):
        self.n += 1
        t_ch, p_ch = truth != stated, pred != stated
        self.pos += t_ch
        self.tp += t_ch and p_ch and truth == pred
        self.fp += p_ch and pred != truth
        self.fn += t_ch and pred != truth
        if truth == pred:
            self.exact += 1
            return 0
        self.abs_err += abs(truth - pred)
        bad = (pred < truth) if danger_if_pred_less else (pred > truth)
        if bad:
            self.danger += 1
            return abs(truth - pred)
        self.waste += 1
        return -abs(truth - pred)

    def row(self):
        prec = self.tp / max(1, self.tp + self.fp)
        rec = self.tp / max(1, self.pos)
        err = self.n - self.exact
        return (f"{self.name:<16}{self.n:>6}{self.pos:>6}{self.exact / max(1, self.n):>8.2%}{prec:>7.1%}{rec:>7.1%}"
                f"{self.danger:>7}{self.waste:>7}{self.abs_err / max(1, err):>9.0f}")


class Flag:
    def __init__(self, name):
        self.name = name
        self.tp = self.fp = self.fn = self.n = 0

    def add(self, truth, pred):
        self.n += 1
        self.tp += truth and pred
        self.fp += pred and not truth
        self.fn += truth and not pred
        return 1 if truth and not pred else (-1 if pred and not truth else 0)

    def row(self):
        pos = self.tp + self.fn
        exact = self.n - self.fp - self.fn
        return (f"{self.name:<16}{self.n:>6}{pos:>6}{exact / max(1, self.n):>8.2%}{self.tp / max(1, self.tp + self.fp):>7.1%}"
                f"{self.tp / max(1, pos):>7.1%}{self.fn:>7}{self.fp:>7}{'':>9}")


HEADER = f"{'field':<16}{'n':>6}{'chg':>6}{'exact':>8}{'prec':>7}{'rec':>7}{'danger':>7}{'waste':>7}{'MAE-err':>9}"


def baseline_parse(note, req):
    """The kit's keyword reader expressed in our fields (best-effort mapping of its windows)."""
    L = req["legs"][0]
    p, d = L["pickup"], L["delivery"]
    out = {"ready_from": None, "pickup_closes": None, "pickup_opens": None, "delivery_closes": None,
           "delivery_opens": None, "deadline": None, "manual": False, "plates": False, "summer_tyres": False}
    if kit_window_from_note is None or not note:
        return out
    pw = kit_window_from_note(note, p["window"], "pickup")
    if pw["from"] > p["window"]["from"]:
        out["ready_from"] = pw["from"]
    if pw["to"] < p["window"]["to"]:
        out["pickup_closes"] = pw["to"] % DAY
    D = max(o["deadline"] for o in req["options"])
    base = {"from": d["window"]["from"], "to": max(d["window"]["from"], D)}
    dw = kit_window_from_note(note, base, "delivery")
    if dw["to"] < base["to"]:
        out["deadline"] = dw["to"]
        out["delivery_closes"] = dw["to"] % DAY
    return out


def evaluate(rows, parser, label, worst=None):
    S = {k: Stat(k) for k in ("ready", "pickup_opens", "pickup_closes", "delivery_opens", "delivery_closes",
                              "deadline")}
    F = {k: Flag(k) for k in ("manual", "plates", "summer_tyres")}
    misses = []
    t0 = time.perf_counter()
    nparse = 0
    for r in rows:
        req, T = r["req"], r["truth"]["legs"][0]
        note = req["note"]
        L = req["legs"][0]
        p, d = L["pickup"], L["delivery"]
        if note:
            nparse += 1
            P = parser(note, req)
        else:
            P = parser(None, req)
        errs = []
        # ready
        stated = p["window"]["from"]
        truth = max(stated, T["readyAt"], T["releasedAt"] or 0)
        pred = max(stated, P["ready_from"] or 0)
        e = S["ready"].add(stated, truth, pred, danger_if_pred_less=True)
        if e:
            errs.append((e, "ready", fmt(truth), fmt(pred)))
        # hours
        for side, th, sh in (("pickup", T["pickupHours"], p["hours"]), ("delivery", T["deliveryHours"], d["hours"])):
            if sh is None or th is None:
                continue
            po, pc = P[side + "_opens"], P[side + "_closes"]
            pred_o = max(sh["opens"], po) if po is not None else sh["opens"]
            pred_c = min(sh["closes"], pc) if pc is not None else sh["closes"]
            if pred_o >= pred_c:
                pred_o, pred_c = sh["opens"], sh["closes"]
            e = S[side + "_opens"].add(sh["opens"], th["opens"], pred_o, danger_if_pred_less=True)
            if e:
                errs.append((e, side + "_opens", hm(th["opens"]), hm(pred_o)))
            e = S[side + "_closes"].add(sh["closes"], th["closes"], pred_c, danger_if_pred_less=False)
            if e:
                errs.append((e, side + "_closes", hm(th["closes"]), hm(pred_c)))
        # deadline
        D = option_deadline(req, r["option"])
        truth = min(D, T["noteDeadline"]) if T["noteDeadline"] is not None else D
        pred = min(D, P["deadline"]) if P["deadline"] is not None else D
        e = S["deadline"].add(D, truth, pred, danger_if_pred_less=False)
        if e:
            errs.append((e, "deadline", fmt(truth), fmt(pred)))
        for k, tk in (("manual", "manualGearbox"), ("plates", "needsPlates"), ("summer_tyres", "summerTyres")):
            e = F[k].add(bool(T[tk]), bool(P[k]))
            if e:
                errs.append((e * 600, k, str(bool(T[tk])), str(bool(P[k]))))
        if errs and note:
            misses.append((r, errs))
    dt = time.perf_counter() - t0
    print(f"\n== {label}  ({len(rows)} won orders, {nparse} with notes, {1000 * dt / max(1, nparse):.3f} ms/note)")
    print(HEADER)
    for s in S.values():
        print(s.row())
    for f in F.values():
        print(f.row())
    tot_d = sum(s.danger for s in S.values()) + sum(f.fn for f in F.values())
    tot_w = sum(s.waste for s in S.values()) + sum(f.fp for f in F.values())
    print(f"{'TOTAL':<16}{'':>6}{'':>6}{'':>8}{'':>7}{'':>7}{tot_d:>7}{tot_w:>7}")
    if worst:
        uniq = {}
        for r, errs in misses:
            sev = max(abs(e[0]) + (100000 if e[0] > 0 else 0) for e in errs)
            key = r["req"]["note"]["text"]
            if key not in uniq or uniq[key][0] < sev:
                uniq[key] = (sev, r, errs)
        ranked = sorted(uniq.values(), key=lambda x: -x[0])[:worst]
        print(f"\n-- {len(ranked)} worst misses (unique notes; danger first) --")
        for sev, r, errs in ranked:
            req = r["req"]
            print(f"[{req['note']['language']}] arr={fmt(req['arrivedAt'])} pw={fmt(req['legs'][0]['pickup']['window']['from'])}"
                  f"  {req['note']['text']!r}")
            for e, k, tv, pv in errs:
                print(f"      {'DANGER' if e > 0 else 'waste '} {k:<16} truth={tv:<14} pred={pv}")
    return S, F


def typo(text, rng):
    """One typo like the dealers make: swap two adjacent letters, double a letter or drop one,
    in one word of 4+ letters.  The world's facts do not change, so the truth stays the same."""
    import re as _re
    words = [m for m in _re.finditer(r"[^\W\d_]{4,}", text)]
    if not words:
        return text
    m = rng.choice(words)
    w = m.group(0)
    i = rng.randrange(1, len(w) - 1)
    op = rng.choice(("swap", "dup", "drop"))
    if op == "swap":
        w2 = w[:i] + w[i + 1] + w[i] + w[i + 2:]
    elif op == "dup":
        w2 = w[:i] + w[i] + w[i:]
    else:
        w2 = w[:i] + w[i + 1:]
    return text[:m.start()] + w2 + text[m.end():]


def order_from(req, option):
    """The order the world would hand us for a won request (windows end at the won option's deadline)."""
    D = option_deadline(req, option)
    legs = []
    for L in req["legs"]:
        L = json.loads(json.dumps(L))
        for k in ("pickup", "delivery"):
            if not L[k]["privateAddress"]:
                L[k]["window"]["to"] = D
        legs.append(L)
    return {"id": req["id"], "dealerId": req["dealerId"], "option": option, "deadline": D, "legs": legs,
            "status": "waiting", "deliveredAt": [None] * len(legs), "lastPickupAt": D}


def _open_ivs(h, lo, hi):
    from notes import _day_open
    out = []
    for d in range(lo // DAY, hi // DAY + 1):
        iv = _day_open(d, h)
        if iv and iv[1] > lo and iv[0] < hi:
            out.append((max(lo, iv[0]), min(hi, iv[1])))
    return out


def _closed_but_stated_open(stated, real, lo, hi):
    """Minutes in [lo, hi] when the stated hours show the site open but it is really closed."""
    if stated is None or real is None or hi <= lo:
        return 0
    real_iv = _open_ivs(real, lo, hi)
    bad = 0
    for a, b in _open_ivs(stated, lo, hi):
        covered = sum(max(0, min(b, d) - max(a, c)) for c, d in real_iv)
        bad += (b - a) - covered
    return bad


def check_stops(rows, make_stops, make_audience, label):
    """Simulate the post the dispatcher makes right after winning each order and count what would
    fail: visit before ready, pickup allowed while really closed, deadline later than the note's,
    missing plates / manual licence, and windows that the world would refuse."""
    import collections
    V = collections.Counter()
    n = 0
    for r in rows:
        req, T = r["req"], r["truth"]["legs"][0]
        if not req["note"]:
            continue
        n += 1
        order = order_from(req, r["option"])
        now = req["arrivedAt"]
        stops = make_stops(order, [0], req, now)
        aud = make_audience(order, [0], req)
        p, d = order["legs"][0]["pickup"], order["legs"][0]["delivery"]
        ps = next(s for s in stops if s["kind"] == "pickup" and s["leg"] == 0)
        ds = next(s for s in stops if s["kind"] == "delivery" and s["leg"] == 0)
        pw = ps.get("window", p["window"])
        dw = ds.get("window", {"from": d["window"]["from"], "to": order["deadline"]})
        ready = max(p["window"]["from"], T["readyAt"], T["releasedAt"] or 0)
        if pw["from"] < ready:
            V["pickup_before_ready"] += 1
        if pw["from"] < p["window"]["from"]:
            V["REFUSED_starts_before_order"] += 1
        if p["privateAddress"] and (pw["from"] < p["window"]["from"] or pw["to"] > p["window"]["to"]):
            V["REFUSED_private_outside_slot"] += 1
        if _closed_but_stated_open(p["hours"], T["pickupHours"], max(pw["from"], ready), pw["to"]) > 0:
            V["pickup_window_hits_real_closed"] += 1
        truth_dl = min(order["deadline"], T["noteDeadline"]) if T["noteDeadline"] is not None else order["deadline"]
        if dw["to"] > truth_dl:
            V["delivery_after_note_deadline"] += 1
        dh, th = d["hours"], T["deliveryHours"]
        if dh is not None and th is not None and _closed_but_stated_open(dh, th, dw["to"] - 1, dw["to"]) > 0:
            V["delivery_end_at_real_closed"] += 1
        if T["needsPlates"] and not ps.get("withPlates"):
            V["no_plates"] += 1
        if T["manualGearbox"] and not aud.get("manualLicence"):
            V["no_manual_licence"] += 1
    print(f"\n== stop-level check: {label} ({n} noted orders, posted at arrival)")
    for k in ("pickup_before_ready", "pickup_window_hits_real_closed", "delivery_after_note_deadline",
              "delivery_end_at_real_closed", "no_plates", "no_manual_licence", "REFUSED_starts_before_order",
              "REFUSED_private_outside_slot"):
        print(f"  {k:<34}{V[k]:>6}")
    return V


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--worst", type=int, default=30)
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--typos", type=int, default=0, help="also run N rounds with one random typo per note")
    ap.add_argument("--test", default="cal-,v13-", help="folder prefixes held out as the test set")
    args = ap.parse_args()
    rows = load(args.data)

    def ours(note, req):
        if not note:
            return parse_note(None, None, req["arrivedAt"])
        return parse_note(note["text"], note.get("language"), req["arrivedAt"])

    import notes as N
    from spread import starter as S
    test_prefixes = tuple(p for p in args.test.split(",") if p)
    train = [r for r in rows if not r["folder"].startswith(test_prefixes)]
    test = [r for r in rows if r["folder"].startswith(test_prefixes)]
    summary = []
    for name, part in (("TRAIN", train), ("HELD-OUT " + ",".join(test_prefixes), test)):
        if not part:
            continue
        print(f"\n################ {name}: {len(part)} won orders from "
              f"{len({r['folder'] for r in part})} folders ################")
        db = None
        if not args.no_baseline:
            Sb, Fb = evaluate(part, lambda note, req: baseline_parse(note, req), f"kit baseline [{name}]")
            db = sum(x.danger for x in Sb.values()) + sum(x.fn for x in Fb.values())
            check_stops(part, lambda o, legs, req, now: S.stops_for(o, legs, req["note"], S.Retry()),
                        lambda o, legs, req: {"experienced": False, "manualLicence": False},
                        f"kit starter.stops_for [{name}]")
        So, Fo = evaluate(part, ours, f"notes.parse_note [{name}]", worst=args.worst)
        # Stop level: every world through ONE shared parse cache with the real request ids, as
        # dispatch.py's threaded sweep does -- a cache keyed by request id alone fails here.
        V = check_stops(part, lambda o, legs, req, now: N.stops_for(o, legs, req, None, now),
                        lambda o, legs, req: N.audience_for(o, legs, req, None), f"notes.stops_for [{name}]")
        d = sum(x.danger for x in So.values()) + sum(x.fn for x in Fo.values())
        w = sum(x.waste for x in So.values()) + sum(x.fp for x in Fo.values())
        summary.append((name, len(part), sum(1 for r in part if r["req"]["note"]), d, w, db, sum(V.values())))
    print("\n== SUMMARY (danger = errors that cause a failed visit or a late car)")
    print(f"{'split':<26}{'orders':>8}{'noted':>7}{'ours danger':>13}{'ours waste':>12}{'kit danger':>12}{'stop viol.':>12}")
    for name, n, nn, d, w, db, v in summary:
        print(f"{name:<26}{n:>8}{nn:>7}{d:>13}{w:>12}{'' if db is None else db:>12}{v:>12}")
    if args.typos:
        import random
        for seed in range(args.typos):
            rng = random.Random(seed)
            noisy = []
            for r in rows:
                note = r["req"]["note"]
                if note:
                    r = dict(r, req=dict(r["req"], note=dict(note, text=typo(note["text"], rng))))
                noisy.append(r)
            evaluate(noisy, ours, f"notes.parse_note with one random typo per note (seed {seed})",
                     worst=args.worst)


if __name__ == "__main__":
    main()
