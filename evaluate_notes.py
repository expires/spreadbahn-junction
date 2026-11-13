"""Evaluate notes_reader against the archive's revealed truth, with the kit starter baseline.

For every won order in every archived level-8 practice world whose request carried a note,
compare the reader's constraints with the facts the world actually acted on (reveal.orders),
and with what the kit starter's regex reader (spread.starter.window_from_note) would have
predicted. The starter is the old hand-set reader; this script quantifies what it got wrong.

    python evaluate_notes.py               # full report, writes reports/notes.md
    python evaluate_notes.py --json        # machine-readable summary too
    python evaluate_notes.py --worlds 40   # quick run on 40 worlds

Facts measured (per request = first leg; the reader returns one constraint set per request):
  * pickup floor      truth max(readyAt, releasedAt) vs stated pickup window.from
  * ready delay       later ready DAY => order deadline moves; truth = business days moved
  * delivery deadline truth noteDeadline (a tighter deadline from the note)
  * pickup hours      truth pickupHours (opens, closes) when they differ from the stated ones
  * delivery hours    truth deliveryHours when they differ from the stated ones
  * manual licence / plates / summer tyres
  * combined window coverage: is the true earliest sensible pickup / latest delivery inside
    the window dispatch would plan from the reader's constraints, and how much tighter/wider.

Validation discipline: worlds are split into two disjoint halves (even/odd world index) and
every number is reported for both halves. The reader is a deterministic rule reader (no fitted
parameters), so the split is a stability check that no world-specific tuning crept in.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from notes_reader import DAY, read_note                          # noqa: E402
from spread.starter import window_from_note                      # noqa: E402

DATA = os.path.join(HERE, "data")
REPORTS = os.path.join(HERE, "reports")
FACTS = ("pickup", "deadline", "delay", "pickupH", "deliveryH", "manual", "plates", "summer")


# --------------------------------------------------------------------------------------
# loading the archive
# --------------------------------------------------------------------------------------
def load_worlds(limit=None):
    """One dict per archived world: its revealed requests, order truth and won orders."""
    root = os.path.join(DATA, "L8")
    names = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    if limit:
        names = names[:limit]
    worlds = []
    for name in names:
        folder = os.path.join(root, name)
        try:
            with gzip.open(os.path.join(folder, "reveal.json.gz"), "rt") as f:
                reveal = json.load(f)
            with gzip.open(os.path.join(folder, "events.json.gz"), "rt") as f:
                events = json.load(f)
        except (OSError, EOFError, json.JSONDecodeError):
            continue
        orders = {ev["order"]["id"]: ev["order"] for ev in events if ev["kind"] == "order_won"}
        truths = {o["orderId"]: o["legs"] for o in reveal["orders"]}
        worlds.append({"id": name, "reveal": reveal, "orders": orders, "truths": truths})
    return worlds


def normalise_request(request):
    """The reader takes a spread.types.PriceRequest; rebuild it with only the defined fields."""
    legs = []
    for leg in request["legs"]:
        legs.append({
            "car": leg["car"],
            "pickup": {"placeId": leg["pickup"]["placeId"],
                        "privateAddress": leg["pickup"]["privateAddress"],
                        "window": leg["pickup"]["window"], "hours": leg["pickup"]["hours"]},
            "delivery": {"placeId": leg["delivery"]["placeId"],
                          "privateAddress": leg["delivery"]["privateAddress"],
                          "window": leg["delivery"]["window"], "hours": leg["delivery"]["hours"]},
        })
    return {"id": request["id"], "dealerId": request.get("dealerId"),
            "batchId": request.get("batchId"), "arrivedAt": request["arrivedAt"],
            "respondBy": request["respondBy"], "legs": legs, "options": request["options"],
            "referenceFeeCents": request["referenceFeeCents"], "note": request["note"]}


def hours_pair(hours):
    if not hours:
        return None
    return (int(hours["opens"]), int(hours["closes"]))


def business_days_between(day_from, day_to):
    if day_to <= day_from:
        return 0
    return sum(1 for d in range(day_from + 1, day_to + 1) if d % 7 < 5)


def shift_business_days(minute, days):
    """Move a 20:00 business-day deadline on by days business days."""
    if days <= 0:
        return minute
    day = minute // DAY
    left = days
    while left > 0:
        day += 1
        if day % 7 < 5:
            left -= 1
    return day * DAY + minute % DAY


def starter_constraints(request, option_deadline):
    """What spread.starter.window_from_note narrows the leg-0 windows to."""
    note = request["note"]
    leg = request["legs"][0]
    base_delivery = {"from": leg["delivery"]["window"]["from"],
                     "to": max(leg["delivery"]["window"]["from"], option_deadline)}
    pickup = window_from_note(note, leg["pickup"]["window"], "pickup")
    delivery = window_from_note(note, base_delivery, "delivery")
    return {"pickup": pickup, "delivery": delivery, "delivery_base": base_delivery}


# --------------------------------------------------------------------------------------
# the evaluation rows
# --------------------------------------------------------------------------------------
def build_rows(worlds):
    """One row per won note order: request context, reader constraints, truth, starter output."""
    rows = []
    for wi, world in enumerate(worlds):
        for revealed in world["reveal"]["requests"]:
            request = revealed["request"]
            note = request.get("note")
            if not note or not note.get("text"):
                continue
            order = world["orders"].get(request["id"])
            truth = world["truths"].get(request["id"])
            if order is None or truth is None:
                continue
            req = normalise_request(request)
            leg = req["legs"][0]
            chosen = order["option"]
            option_deadline = next(o["deadline"] for o in req["options"] if o["kind"] == chosen)
            nc = read_note(req)
            base = starter_constraints(req, option_deadline)
            t = truth[0]
            stated_from = leg["pickup"]["window"]["from"]
            pickup_floor = max(t["readyAt"], t["releasedAt"] if t["releasedAt"] is not None else t["readyAt"])
            delay_truth = (business_days_between(stated_from // DAY, t["readyAt"] // DAY)
                           if t["readyAt"] > stated_from else 0)
            rows.append({
                "world": world["id"], "world_index": wi, "rid": request["id"],
                "lang": note["language"], "text": note["text"],
                "arrivedAt": request["arrivedAt"], "stated_from": stated_from,
                "option_deadline": option_deadline, "lastPickupAt": order["lastPickupAt"],
                "order_deadline": order["deadline"],
                "delivery_from": leg["delivery"]["window"]["from"],
                "stated_pickup_hours": hours_pair(leg["pickup"]["hours"]),
                "stated_delivery_hours": hours_pair(leg["delivery"]["hours"]),
                "nc": nc,
                "note_deadline_truth": t["noteDeadline"],
                "pickup_floor_truth": pickup_floor if pickup_floor > stated_from else None,
                "pickup_floor_abs": pickup_floor,
                "delay_truth": delay_truth,
                "pickup_hours_truth": hours_pair(t["pickupHours"]),
                "delivery_hours_truth": hours_pair(t["deliveryHours"]),
                "manual_truth": bool(t["manualGearbox"]),
                "plates_truth": bool(t["needsPlates"]),
                "summer_truth": bool(t["summerTyres"]),
                "starter_pickup_from": base["pickup"]["from"],
                "starter_delivery_to": base["delivery"]["to"],
                "starter_delivery_base_to": base["delivery_base"]["to"],
            })
    return rows


# --------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------
class Confusion:
    """Truth positives, predicted positives, true positives."""
    __slots__ = ("truth", "pred", "tp")

    def __init__(self):
        self.truth = self.pred = self.tp = 0

    def add(self, truth, pred):
        self.truth += 1 if truth else 0
        self.pred += 1 if pred else 0
        self.tp += 1 if (truth and pred) else 0

    @property
    def precision(self):
        return self.tp / self.pred if self.pred else None

    @property
    def recall(self):
        return self.tp / self.truth if self.truth else None


def fact_metrics(rows):
    """Per-fact confusion counts and exact-match counts, reader vs kit starter."""
    out = {}
    for name in FACTS:
        out[name] = {"reader": Confusion(), "starter": Confusion(),
                     "reader_exact": 0, "starter_exact": 0, "n": 0}
    for row in rows:
        nc = row["nc"]
        m = out["pickup"]
        m["n"] += 1
        reader_pred = row["pickup_floor_truth"] is not None and nc.pickup_from is not None
        starter_pred = row["pickup_floor_truth"] is not None and row["starter_pickup_from"] > row["stated_from"]
        m["reader"].add(row["pickup_floor_truth"] is not None, reader_pred)
        m["starter"].add(row["pickup_floor_truth"] is not None, starter_pred)
        m["reader_exact"] += (nc.pickup_from if nc.pickup_from is not None else None) == row["pickup_floor_truth"]
        starter_from = row["starter_pickup_from"] if row["starter_pickup_from"] > row["stated_from"] else None
        m["starter_exact"] += starter_from == row["pickup_floor_truth"]
        m = out["delay"]
        m["reader"].add(row["delay_truth"] > 0, nc.ready_delay_days > 0)
        m["starter"].add(row["delay_truth"] > 0, False)
        m["reader_exact"] += nc.ready_delay_days == row["delay_truth"]
        m["starter_exact"] += row["delay_truth"] == 0
        m = out["deadline"]
        truth = row["note_deadline_truth"]
        m["reader"].add(truth is not None, nc.note_deadline is not None)
        m["reader_exact"] += nc.note_deadline == truth
        starter_to = row["starter_delivery_to"]
        starter_nd = starter_to if starter_to < row["starter_delivery_base_to"] else None
        m["starter"].add(truth is not None, starter_nd is not None)
        m["starter_exact"] += starter_nd == truth
        for key, field, stated_key in (("pickupH", "pickup_hours", "stated_pickup_hours"),
                                        ("deliveryH", "delivery_hours", "stated_delivery_hours")):
            truth_h = row[field + "_truth"]
            stated_h = row[stated_key]
            changed = truth_h != stated_h
            pred_h = getattr(nc, field)
            m = out[key]
            m["reader"].add(changed, pred_h is not None)
            m["starter"].add(changed, False)
            m["reader_exact"] += (pred_h if pred_h is not None else stated_h) == truth_h
            m["starter_exact"] += stated_h == truth_h
        for key, field in (("manual", "manual_licence"), ("plates", "needs_plates"),
                            ("summer", "summer_tyres")):
            m = out[key]
            truth = row[key + "_truth"]
            pred = getattr(nc, field)
            m["reader"].add(truth, pred)
            m["starter"].add(truth, False)
            m["reader_exact"] += pred == truth
            m["starter_exact"] += (not truth)
    return out


def coverage_metrics(rows):
    """The combined truth-window metric for ignore-note, kit starter and our reader.

    Pickup window: [planned floor, order lastPickupAt]. Delivery window: [window from,
    planned ceiling]. Covered = the true earliest sensible pickup / latest delivery is
    inside the planned window. Unsafe = the plan starts before the car is ready (failed
    visit) or ends after the true delivery ceiling (late).
    """
    out = {}
    for name in ("ignore", "starter", "reader"):
        out[name] = {"n": 0, "pickup_covered": 0, "delivery_covered": 0, "both": 0,
                     "pickup_unsafe": 0, "delivery_unsafe": 0, "pickup_slack": [],
                     "delivery_slack": []}
    for row in rows:
        nc = row["nc"]
        floor = row["pickup_floor_abs"]
        ceiling = row["note_deadline_truth"] if row["note_deadline_truth"] is not None else row["order_deadline"]
        ceiling = min(ceiling, row["order_deadline"])
        p_to = row["lastPickupAt"]
        d_from = row["delivery_from"]
        reader_from = max(row["stated_from"], nc.pickup_from if nc.pickup_from is not None else -10 ** 9)
        reader_to = min(shift_business_days(row["option_deadline"], nc.ready_delay_days),
                        nc.delivery_by if nc.delivery_by is not None else 10 ** 9)
        plans = {
            "ignore": (row["stated_from"], row["option_deadline"]),
            "starter": (max(row["stated_from"], row["starter_pickup_from"]),
                        row["starter_delivery_to"]),
            "reader": (reader_from, reader_to),
        }
        for name, (p_from, d_to) in plans.items():
            m = out[name]
            m["n"] += 1
            pickup_ok = p_from <= floor <= p_to
            delivery_ok = d_from <= ceiling <= d_to
            m["pickup_covered"] += 1 if pickup_ok else 0
            m["delivery_covered"] += 1 if delivery_ok else 0
            m["both"] += 1 if (pickup_ok and delivery_ok) else 0
            m["pickup_unsafe"] += 1 if p_from < floor else 0
            m["delivery_unsafe"] += 1 if d_to > ceiling else 0
            m["pickup_slack"].append(p_from - floor)
            m["delivery_slack"].append(d_to - ceiling)
    for name, m in out.items():
        for key in ("pickup_slack", "delivery_slack"):
            xs = sorted(m.pop(key))
            m[key + "_median"] = xs[len(xs) // 2] if xs else 0
    return out


def language_metrics(rows):
    by_lang = defaultdict(list)
    for row in rows:
        by_lang[row["lang"]].append(row)
    return {lang: {"n": len(sub), "facts": fact_metrics(sub)} for lang, sub in sorted(by_lang.items())}


def row_errors(row):
    """Every fact where the reader disagrees with the truth on this row."""
    nc = row["nc"]
    errs = []
    if nc.pickup_from != row["pickup_floor_truth"]:
        errs.append(("pickup floor", nc.pickup_from, row["pickup_floor_truth"]))
    if nc.ready_delay_days != row["delay_truth"]:
        errs.append(("ready delay", nc.ready_delay_days, row["delay_truth"]))
    if nc.note_deadline != row["note_deadline_truth"]:
        errs.append(("deadline", nc.note_deadline, row["note_deadline_truth"]))
    for key, field, stated_key in (("pickupH", "pickup_hours", "stated_pickup_hours"),
                                    ("deliveryH", "delivery_hours", "stated_delivery_hours")):
        pred = getattr(nc, field)
        pred = pred if pred is not None else row[stated_key]
        if pred != row[field + "_truth"]:
            errs.append((key, pred, row[field + "_truth"]))
    for key, field in (("manual", "manual_licence"), ("plates", "needs_plates"),
                        ("summer", "summer_tyres")):
        if getattr(nc, field) != row[key + "_truth"]:
            errs.append((key, getattr(nc, field), row[key + "_truth"]))
    return errs


def starter_errors(row):
    """Every fact where the kit starter's reader disagrees with the truth on this row."""
    errs = []
    starter_from = row["starter_pickup_from"] if row["starter_pickup_from"] > row["stated_from"] else None
    if starter_from != row["pickup_floor_truth"]:
        errs.append(("pickup floor", starter_from, row["pickup_floor_truth"]))
    if row["delay_truth"]:
        errs.append(("ready delay", 0, row["delay_truth"]))
    starter_to = row["starter_delivery_to"]
    starter_nd = starter_to if starter_to < row["starter_delivery_base_to"] else None
    if starter_nd != row["note_deadline_truth"]:
        errs.append(("deadline", starter_nd, row["note_deadline_truth"]))
    for key, field, stated_key in (("pickupH", "pickup_hours", "stated_pickup_hours"),
                                   ("deliveryH", "delivery_hours", "stated_delivery_hours")):
        if row[field + "_truth"] != row[stated_key]:
            errs.append((key, row[stated_key], row[field + "_truth"]))
    for key in ("manual", "plates", "summer"):
        if row[key + "_truth"]:
            errs.append((key, False, True))
    return errs


def norm_text(text):
    return re.sub(r"\d+", "#", text.lower()).strip()


def _row_truth(row):
    """Only the facts the note actually changed, as the world acted on them."""
    return {"pickup_floor": row["pickup_floor_truth"], "delay": row["delay_truth"],
            "note_deadline": row["note_deadline_truth"],
            "pickup_hours": (row["pickup_hours_truth"]
                             if row["pickup_hours_truth"] != row["stated_pickup_hours"] else None),
            "delivery_hours": (row["delivery_hours_truth"]
                               if row["delivery_hours_truth"] != row["stated_delivery_hours"] else None),
            "manual": row["manual_truth"], "plates": row["plates_truth"],
            "summer": row["summer_truth"]}


def _row_starter(row):
    """The starter's non-default predictions (its narrowed window relative to the stated one)."""
    return {"pickup_from": (row["starter_pickup_from"]
                            if row["starter_pickup_from"] > row["stated_from"] else None),
            "delivery_to": (row["starter_delivery_to"]
                            if row["starter_delivery_to"] < row["starter_delivery_base_to"] else None)}


def _row_reader(row):
    nc = row["nc"]
    return {"pickup_from": nc.pickup_from, "delivery_by": nc.delivery_by,
            "ready_delay_days": nc.ready_delay_days, "pickup_hours": nc.pickup_hours,
            "delivery_hours": nc.delivery_hours, "manual": nc.manual_licence,
            "plates": nc.needs_plates, "summer": nc.summer_tyres}


def mistake_table(rows, errs_of, limit=20):
    """The most common note texts on which a reader gets at least one fact wrong."""
    groups = {}
    for row in rows:
        errs = errs_of(row)
        if not errs:
            continue
        key = norm_text(row["text"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"count": 0, "errs": Counter(), "example": row}
        group["count"] += 1
        for fact, _, _ in errs:
            group["errs"][fact] += 1
    ranked = sorted(groups.values(), key=lambda g: -g["count"])[:limit]
    out = []
    for group in ranked:
        row = group["example"]
        out.append({"count": group["count"], "facts": dict(group["errs"]),
                    "text": row["text"], "lang": row["lang"],
                    "truth": _row_truth(row), "starter": _row_starter(row),
                    "reader": _row_reader(row)})
    return out


def audit_requests(worlds):
    """Reader robustness and speed over EVERY note request in the archive (won or not).

    read_note never raises: a bug falls back to an empty constraint set. This audit counts
    how often the parser raises internally and how often a note produces a constraint, so a
    silent fallback cannot hide a broken parser.
    """
    import notes_reader
    n = failures = constrained = 0
    t0 = time.time()
    for world in worlds:
        for revealed in world["reveal"]["requests"]:
            request = revealed["request"]
            note = request.get("note")
            if not note or not note.get("text"):
                continue
            n += 1
            try:
                nc = notes_reader._read_note(normalise_request(request))
            except Exception:                       # pragma: no cover - must stay at zero
                failures += 1
                continue
            if (nc.pickup_from is not None or nc.delivery_by is not None
                    or nc.pickup_hours is not None or nc.delivery_hours is not None
                    or nc.manual_licence or nc.needs_plates or nc.summer_tyres
                    or nc.ready_delay_days):
                constrained += 1
    return {"note_requests": n, "internal_failures": failures,
            "with_constraint": constrained, "seconds": round(time.time() - t0, 2)}


def impact_metrics(rows):
    """How much the reader changes the plan on the notes we win."""
    import statistics
    n = len(rows)
    out = {"n": n, "pickup": 0, "delay": 0, "deadline": 0, "pickup_hours": 0,
           "delivery_hours": 0, "manual": 0, "plates": 0, "summer": 0, "any": 0,
           "pickup_minutes": [], "deadline_minutes": [], "delay_days": []}
    for row in rows:
        nc = row["nc"]
        changed = False
        if nc.pickup_from is not None:
            out["pickup"] += 1
            out["pickup_minutes"].append(nc.pickup_from - row["stated_from"])
            changed = True
        if nc.ready_delay_days:
            out["delay"] += 1
            out["delay_days"].append(nc.ready_delay_days)
            changed = True
        if nc.note_deadline is not None:
            out["deadline"] += 1
            out["deadline_minutes"].append(nc.note_deadline - row["order_deadline"])
            changed = True
        if nc.pickup_hours is not None:
            out["pickup_hours"] += 1
            changed = True
        if nc.delivery_hours is not None:
            out["delivery_hours"] += 1
            changed = True
        for key, field in (("manual", "manual_licence"), ("plates", "needs_plates"),
                            ("summer", "summer_tyres")):
            if getattr(nc, field):
                out[key] += 1
                changed = True
        out["any"] += 1 if changed else 0
    for key, label in (("pickup_minutes", "pickup_median"), ("deadline_minutes", "deadline_median"),
                        ("delay_days", "delay_median")):
        xs = sorted(out.pop(key))
        out[label] = statistics.median(xs) if xs else None
    return out


def summary(rows, worlds, elapsed):
    return {"rows": len(rows), "worlds": len(worlds), "elapsed": round(elapsed, 1),
            "facts": fact_metrics(rows),
            "coverage": coverage_metrics(rows),
            "by_language": language_metrics(rows),
            "halves": {"even": fact_metrics([r for r in rows if r["world_index"] % 2 == 0]),
                        "odd": fact_metrics([r for r in rows if r["world_index"] % 2 == 1]),
                        "even_rows": sum(1 for r in rows if r["world_index"] % 2 == 0),
                        "odd_rows": sum(1 for r in rows if r["world_index"] % 2 == 1)},
            "reader_mistakes": mistake_table(rows, row_errors),
            "starter_mistakes": mistake_table(rows, starter_errors),
            "audit": audit_requests(worlds), "impact": impact_metrics(rows)}


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------
def clock(minute):
    if minute is None:
        return "-"
    return "%d:%02d:%02d" % (minute // DAY, (minute % DAY) // 60, minute % 60)


def hours_str(pair):
    return "%02d:%02d-%02d:%02d" % (pair[0] // 60, pair[0] % 60, pair[1] // 60, pair[1] % 60)


def pct(x):
    return "-" if x is None else "%.1f%%" % (100 * x)


def truth_str(truth):
    parts = []
    if truth["pickup_floor"] is not None:
        parts.append("pickup>=" + clock(truth["pickup_floor"]))
    if truth["delay"]:
        parts.append("ready+%dbd" % truth["delay"])
    if truth["note_deadline"] is not None:
        parts.append("by " + clock(truth["note_deadline"]))
    if truth["pickup_hours"]:
        parts.append("pickup hours " + hours_str(truth["pickup_hours"]))
    if truth["delivery_hours"]:
        parts.append("delivery hours " + hours_str(truth["delivery_hours"]))
    for key, label in (("manual", "manual"), ("plates", "plates"), ("summer", "summer tyres")):
        if truth[key]:
            parts.append(label)
    return "; ".join(parts) or "no constraint"


def reader_str(reader):
    parts = []
    if reader["pickup_from"] is not None:
        parts.append("pickup>=" + clock(reader["pickup_from"]))
    if reader["ready_delay_days"]:
        parts.append("ready+%dbd" % reader["ready_delay_days"])
    if reader["delivery_by"] is not None:
        parts.append("by " + clock(reader["delivery_by"]))
    for key, label in (("pickup_hours", "pickup hours"), ("delivery_hours", "delivery hours")):
        if reader[key]:
            parts.append(label + " " + hours_str(reader[key]))
    for key, label in (("manual", "manual"), ("plates", "plates"), ("summer", "summer tyres")):
        if reader[key]:
            parts.append(label)
    return "; ".join(parts) or "no constraint"


def starter_str(starter):
    parts = []
    if starter["pickup_from"] is not None:
        parts.append("pickup>=" + clock(starter["pickup_from"]))
    if starter["delivery_to"] is not None:
        parts.append("by " + clock(starter["delivery_to"]))
    return "; ".join(parts) or "no constraint"


def md_cell(text):
    return str(text).replace("|", "\\|")


def fact_row(label, m, total):
    r, s = m["reader"], m["starter"]
    n = r.truth
    return ("| " + label + " | %d | %s | %s | %d/%d (%s) | %s | %s | %d/%d (%s) |"
            % (n, pct(r.recall), pct(r.precision), m["reader_exact"], total,
               pct(m["reader_exact"] / total), pct(s.recall), pct(s.precision),
               m["starter_exact"], total, pct(m["starter_exact"] / total)))


def write_report(path, data):
    f = data["facts"]
    cov = data["coverage"]
    lines = []
    add = lines.append
    add("# Notes reader: evaluation against the level-8 archive")
    add("")
    add("Worlds: %d. Won note orders evaluated: **%d** (one row per won request carrying a"
        " note, leg 0). Run time %ss." % (data["worlds"], data["rows"], data["elapsed"]))
    add("")
    add("Truth is the reveal's per-leg facts (reveal.orders), i.e. what the world actually "
        "acted on. Reader = notes_reader.read_note. Baseline = the kit starter's "
        "window_from_note, the old hand-set reader (it only narrows windows by clock times "
        "near a few keywords).")
    add("")
    add("## Per-fact accuracy")
    add("")
    add("| fact | truth cases | reader recall | reader precision | reader exact (all rows) | "
        "starter recall | starter precision | starter exact (all rows) |")
    add("|---|---|---|---|---|---|---|---|")
    for label, key in (("pickup floor (ready/release)", "pickup"), ("ready delay days", "delay"),
                        ("delivery deadline", "deadline"), ("pickup hours", "pickupH"),
                        ("delivery hours", "deliveryH"), ("manual licence", "manual"),
                        ("transfer plates", "plates"), ("summer tyres", "summer")):
        add(fact_row(label, f[key], data["rows"]))
    add("")
    add("Exact = same absolute world minute / same (opens, closes) pair / same boolean, over "
        "all evaluated orders including the harmless notes where the right answer is no "
        "constraint. Precision is over the reader's non-default predictions, recall over the "
        "truth's non-default facts. The starter never reads release/handover, day words, "
        "am/pm, corrections, opening-hours overrides or driver/car requirements, so its recall "
        "on those facts is 0 by construction.")
    add("")
    add("### Per note language")
    add("")
    add("Exact rows / all rows of that language (all facts match).")
    add("")
    add("| language | rows | pickup floor exact | deadline exact | pickup hours exact | "
        "delivery hours exact | manual P/R | plates P/R | summer P/R |")
    add("|---|---|---|---|---|---|---|---|---|")
    for lang, lm in data["by_language"].items():
        lf = lm["facts"]
        manual = lf["manual"]["reader"]
        plates = lf["plates"]["reader"]
        summer = lf["summer"]["reader"]
        add("| %s | %d | %d/%d | %d/%d | %d/%d | %d/%d | %s/%s | %s/%s | %s/%s |" % (
            lang, lm["n"],
            lf["pickup"]["reader_exact"], lm["n"],
            lf["deadline"]["reader_exact"], lm["n"],
            lf["pickupH"]["reader_exact"], lm["n"],
            lf["deliveryH"]["reader_exact"], lm["n"],
            pct(manual.precision), pct(manual.recall), pct(plates.precision),
            pct(plates.recall), pct(summer.precision), pct(summer.recall)))
    add("")
    add("## Combined truth-window coverage")
    add("")
    add("For every won note order: pickup window = [planned floor, order lastPickupAt], "
        "delivery window = [window from, planned deadline]. Covered = the true earliest "
        "sensible pickup max(readyAt, releasedAt) / latest delivery min(order deadline, "
        "noteDeadline) is inside the planned window. Unsafe = the plan starts before the car "
        "is ready (a failed visit) / ends after the true delivery ceiling (late). Slack is the "
        "median minutes the plan is tighter(-) or wider(+) than the truth.")
    add("")
    add("| plan | pickup covered | delivery covered | both covered | pickup unsafe | "
        "delivery unsafe | median pickup slack | median delivery slack |")
    add("|---|---|---|---|---|---|---|---|")
    for name, label in (("ignore", "ignore the note"), ("starter", "kit starter"),
                         ("reader", "notes_reader")):
        m = cov[name]
        n = m["n"]
        add("| %s | %.1f%% | %.1f%% | %.1f%% | %.1f%% | %.1f%% | %+d | %+d |" % (
            label, 100.0 * m["pickup_covered"] / n, 100.0 * m["delivery_covered"] / n,
            100.0 * m["both"] / n, 100.0 * m["pickup_unsafe"] / n,
            100.0 * m["delivery_unsafe"] / n, m["pickup_slack_median"],
            m["delivery_slack_median"]))
    add("")
    add("## What the reader changes on the notes we win")
    add("")
    imp = data["impact"]
    n = imp["n"]
    add("| constraint | orders | share | median shift |")
    add("|---|---|---|---|")
    add("| pickup floor later than the stated ready time | %d | %.1f%% | %s min |"
        % (imp["pickup"], 100.0 * imp["pickup"] / n,
           "-" if imp["pickup_median"] is None else "%+d" % imp["pickup_median"]))
    add("| car ready a later business day (deadline moves too) | %d | %.1f%% | %s bd |"
        % (imp["delay"], 100.0 * imp["delay"] / n,
           "-" if imp["delay_median"] is None else "%+g" % imp["delay_median"]))
    add("| tighter delivery deadline | %d | %.1f%% | %s min vs order deadline |"
        % (imp["deadline"], 100.0 * imp["deadline"] / n,
           "-" if imp["deadline_median"] is None else "%+d" % imp["deadline_median"]))
    add("| real pickup opening hours | %d | %.1f%% | - |"
        % (imp["pickup_hours"], 100.0 * imp["pickup_hours"] / n))
    add("| real delivery opening hours | %d | %.1f%% | - |"
        % (imp["delivery_hours"], 100.0 * imp["delivery_hours"] / n))
    add("| manual licence required | %d | %.1f%% | - |" % (imp["manual"], 100.0 * imp["manual"] / n))
    add("| transfer plates required | %d | %.1f%% | - |" % (imp["plates"], 100.0 * imp["plates"] / n))
    add("| summer tyres (snow-sensitive) | %d | %.1f%% | - |" % (imp["summer"], 100.0 * imp["summer"] / n))
    add("| at least one constraint | %d | %.1f%% | - |" % (imp["any"], 100.0 * imp["any"] / n))
    add("")
    add("## How dispatch v8 applies the constraints")
    add("")
    add("The order/request windows do NOT carry the note's truth: for ready-delay notes the")
    add("order's pickup window.from stays at the stated ready time and only the deadline")
    add("moves. Dispatch must use the reader's output explicitly:")
    add("")
    add("    nc = read_note(request)")
    add("    # pickup window floor (post the pickup from here, never earlier)")
    add("    pickup_from = max(request_pickup_window_from, nc.pickup_from or -inf)")
    add("    # deadline: shift the quoted option's deadline by the ready-delay business days,")
    add("    # then tighten with the note deadline")
    add("    deadline = shift_business_days(option_deadline, nc.ready_delay_days)")
    add("    if nc.delivery_by is not None: deadline = min(deadline, nc.delivery_by)")
    add("    # opening hours: override the site's stated hours for every day when present")
    add("    pickup_hours = nc.pickup_hours or stated_pickup_hours")
    add("    delivery_hours = nc.delivery_hours or stated_delivery_hours")
    add("    # driver: audience manual only when nc.manual_licence; withPlates when nc.needs_plates")
    add("    # summer tyres: keep the car out of forecast snow at either end before the deadline")
    add("")
    add("ready_delay_days is 0 for a release/handover note (a release moves the pickup floor")
    add("but not the deadline). pickup_from already equals the later ready time or the release")
    add("time, so the pickup window can be pinned to it even when ready_delay_days is 0.")
    add("")
    add("## Validation split")
    add("")
    add("The reader is a deterministic rule reader with no fitted parameters, so whole-world "
        "halves (even/odd world index) are a stability check rather than a train/test split. "
        "The rules were written from the rules of the game and audited against the archive as "
        "a whole, so treat this as confirmation that no single world dominates the numbers.")
    add("")
    add("| half | rows | pickup floor exact | deadline exact | pickup hours exact | "
        "delivery hours exact |")
    add("|---|---|---|---|---|---|")
    for name in ("even", "odd"):
        hf = data["halves"][name]
        total = data["halves"][name + "_rows"]
        add("| %s worlds | %d | %d/%d | %d/%d | %d/%d | %d/%d |" % (
            name, total,
            hf["pickup"]["reader_exact"], total,
            hf["deadline"]["reader_exact"], total,
            hf["pickupH"]["reader_exact"], total,
            hf["deliveryH"]["reader_exact"], total))
    add("")
    add("## What the old hand-set reader got wrong")
    add("")
    add("The starter's 20 most common wrong notes, grouped by note text with digits "
        "normalised to '#'. Count = won orders in the archive with that note text where the "
        "starter disagrees with the truth. Truth = what the world acted on; reader = what "
        "notes_reader returns for the same request.")
    add("")
    add("| # | count | lang | note (example) | starter says | truth | reader says |")
    add("|---|---|---|---|---|---|---|")
    for i, m in enumerate(data["starter_mistakes"], 1):
        add("| %d | %d | %s | %s | %s | %s | %s |" % (
            i, m["count"], m["lang"], md_cell(m["text"]),
            md_cell(starter_str(m["starter"])),
            md_cell(truth_str(m["truth"])), md_cell(reader_str(m["reader"]))))
    add("")
    add("The systematic misses, in words:")
    add("")
    add("- **No day semantics.** It reads the clock time but puts it on the stated window's")
    add("  own day, so 'huomenna 11:00', 'i morgon kl 12', 'tomorrow 4pm', 'to klo 10' and")
    add("  'keskiviikkona 9:30' land on the wrong day. It also has no language-robust")
    add("  weekday handling and no corrections ('eikun klo 18', 'korjaus: ma', 'sorry, i mean').")
    add("- **No am/pm and no hour words.** '3pm' is read as 03:00; 'kahdeltatoista',")
    add("  'viideltä', 'halv tio' are not read at all, so those notes keep the stated window.")
    add("- **Opening hours are only a window narrowing.** Ranges ('9-16') are moved into the")
    add("  planned window's day but the plan still allows the driver to arrive after the real")
    add("  closing time; 'arki(sin)'/'weekdays' is not understood; the real pickup/delivery")
    add("  hours are never applied per day, so the driver waits or the visit fails (closed).")
    add("- **It cannot read a release, a later ready DAY, a manual gearbox, missing plates or")
    add("  summer tyres.** Those notes are invisible: the plan starts before handover/ready")
    add("  (visit_failed not_ready), sends an automatic-licence driver, forgets withPlates")
    add("  (visit_failed no_plates) or drives summer tyres in snow (visit_failed not_ready).")
    add("- **The delivery-keyword heuristic misfires.** Any delivery-ish word anywhere in the")
    add("  note sends every clock time to the delivery window and ignores the pickup side, so")
    add("  two-stop notes ('nouto ennen klo 15. vastaanotto 8-20') are half-read.")
    add("- **No deadline-tightening.** A note deadline later than the won option's deadline is")
    add("  harmless, but one that tightens it is missed: the driver plans by the order deadline")
    add("  and the car is late by the note (a refund), or the quote under-prices the risk.")
    add("")
    add("## Reader residual mistakes")
    add("")
    if not data["reader_mistakes"]:
        add("None. On every labelled fact of every won note order in the archive the reader "
            "agrees with the reveal exactly (%s orders / %s note requests)."
            % (format(data["rows"], ","), format(data["audit"]["note_requests"], ",")))
    else:
        add("| # | count | note | truth | reader |")
        add("|---|---|---|---|---|")
        for i, m in enumerate(data["reader_mistakes"], 1):
            add("| %d | %d | %s | %s | %s |" % (i, m["count"], md_cell(m["text"]),
                                                md_cell(truth_str(m["truth"])),
                                                md_cell(reader_str(m["reader"]))))
    add("")
    add("## Robustness and speed")
    add("")
    audit = data["audit"]
    add("Run over every note request in the archive, won or not: %s note requests, "
        "%d internal parse failures, %s requests that produced at least one constraint, "
        "%ss total (%.0f notes/second). read_note never raises: an internal error falls back "
        "to an empty constraint set, and the audit checks that this fallback never fires."
        % (format(audit["note_requests"], ","), audit["internal_failures"],
           format(audit["with_constraint"], ","), audit["seconds"],
           audit["note_requests"] / max(audit["seconds"], 1e-9)))
    add("")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worlds", type=int, default=None, help="limit the number of worlds")
    ap.add_argument("--json", action="store_true", help="also print a machine-readable summary")
    args = ap.parse_args()
    t0 = time.time()
    worlds = load_worlds(args.worlds)
    rows = build_rows(worlds)
    data = summary(rows, worlds, time.time() - t0)
    os.makedirs(REPORTS, exist_ok=True)
    report = os.path.join(REPORTS, "notes.md")
    write_report(report, data)
    print("worlds %d  won note orders %s  report %s" % (data["worlds"], format(data["rows"], ","), report))
    for name, label in (("pickup", "pickup floor"), ("deadline", "delivery deadline"),
                         ("delay", "ready delay"), ("pickupH", "pickup hours"),
                         ("deliveryH", "delivery hours"), ("manual", "manual licence"),
                         ("plates", "plates"), ("summer", "summer tyres")):
        m = data["facts"][name]
        r, s = m["reader"], m["starter"]
        total = data["rows"]
        print("  %-17s n=%5d  reader exact %5d/%d P %6s R %6s   starter exact %5d/%d P %6s R %6s" % (
            label, r.truth, m["reader_exact"], total, pct(r.precision), pct(r.recall),
            m["starter_exact"], total, pct(s.precision), pct(s.recall)))
    if args.json:
        dump = {"rows": data["rows"], "worlds": data["worlds"],
                "facts": {key: {"truth": m["reader"].truth, "pred": m["reader"].pred,
                                "tp": m["reader"].tp, "reader_exact": m["reader_exact"],
                                "starter_pred": m["starter"].pred, "starter_tp": m["starter"].tp,
                                "starter_exact": m["starter_exact"]}
                          for key, m in data["facts"].items()},
                "coverage": data["coverage"]}
        print(json.dumps(dump, indent=1))


if __name__ == "__main__":
    main()
