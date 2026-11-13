"""Bundle two single-leg orders into one gig.

The best archived level-8 worlds move 66% of their cars in two-order gigs: one driver
collects the first car, delivers it, then collects the second car near that delivery and
delivers it (the world's stop order is pickupA, deliveryA, pickupB, deliveryB). One gig
carries both cars at a fee far below two single posting fees - the archive's opening fees
fit fee = 0.79*(refA+refB) - 5 (R^2 0.86), i.e. ~0.88 of the two orders' reference fees
against 0.90 for each single posting. That is roughly a 23% cut in the driver fee for the
pair, and the proven worlds run 98% on time with it.

This module is pure geometry/timing: given two won orders (each one leg, one car) and the
request each came from, it decides whether they can be chained, in which orientation, and
what a sane opening fee is. dispatch.py does the posting; the offline evaluator reuses this.
"""
from __future__ import annotations

from typing import Optional

HOUR = 60
DAY = 1440
MAX_DRIVE = 12 * HOUR          # the rules' limit for a multi-car gig, from first pickup to last delivery
GRACE = 45                     # minutes of slack we demand before a deadline in the feasibility check
BUNDLE_BASE_RATIO = 0.88       # ladder base / (refA + refB): opening fee = 0.9*base = 0.79*(refA+refB)
BUNDLE_MIN_FEE = 2500          # cents: never open a two-car gig below EUR25


def _leg(order: dict, leg: int = 0) -> dict:
    return order["legs"][leg]


def ref_fee(request: dict, order: dict) -> float:
    """The typical fee for the cars of one order (cents) - a trade-in request's reference fee covers both legs."""
    return request["referenceFeeCents"] * len(order["legs"]) / len(order["legs"])


def chain(a: dict, ra: dict, b: dict, rb: dict, road, max_drive: int = MAX_DRIVE,
          max_link: int = 90) -> Optional[dict]:
    """Can order b be moved after order a by one driver? Returns the chain, or None.

    a and b are Desk orders (one leg each); ra/rb their PriceRequests. road.minutes/km are
    the map's road minutes/km. The chain's first pickup can start at a's pickup window
    opening; the driver delivers a, drives to b's pickup, then delivers b by b's deadline.
    """
    if len(a["legs"]) != 1 or len(b["legs"]) != 1:
        return None
    if a["legs"][0]["delivery"]["privateAddress"] or b["legs"][0]["delivery"]["privateAddress"]:
        pass  # private deliveries have a booked hour; timing still checkable through the window
    pa, da = a["legs"][0]["pickup"]["placeId"], a["legs"][0]["delivery"]["placeId"]
    pb, db = b["legs"][0]["pickup"]["placeId"], b["legs"][0]["delivery"]["placeId"]
    drive_a = road.minutes(pa, da)
    drive_link = road.minutes(da, pb)
    drive_b = road.minutes(pb, db)
    total = drive_a + drive_link + drive_b
    if total > max_drive or drive_link > max_link:
        return None
    start_a = max(a["legs"][0]["pickup"]["window"]["from"], b["legs"][0]["pickup"]["window"]["from"] - total)
    if start_a > a["legs"][0]["pickup"]["window"]["to"]:
        return None
    arrive_a = start_a + drive_a
    if arrive_a + GRACE > a["deadline"]:
        return None
    start_b = max(arrive_a + drive_link, b["legs"][0]["pickup"]["window"]["from"])
    if start_b > b["legs"][0]["pickup"]["window"]["to"]:
        return None
    arrive_b = start_b + drive_b
    if arrive_b + GRACE > b["deadline"]:
        return None
    # one driver's legal day: keep the delivery of b before the end of the world
    return {
        "first": a, "second": b, "total_drive": total, "link": drive_link,
        "start": start_a, "finish": arrive_b,
        # a chain that starts late is riskier: prefer short links and early starts
        "score": -(drive_link * 1.5 + total * 0.4 + max(0, start_a - a["legs"][0]["pickup"]["window"]["from"])),
    }


def best_chain(a: dict, ra: dict, others: list[tuple[dict, dict]], road, max_drive: int = MAX_DRIVE) -> Optional[dict]:
    """The best chain that starts with a; others is [(order, request)] of candidate second cars."""
    best = None
    for b, rb in others:
        if b["id"] == a["id"]:
            continue
        c = chain(a, ra, b, rb, road, max_drive)
        if c is None:
            continue
        if best is None or c["score"] > best["score"]:
            best = c
    return best


def bundle_base_fee(ra: dict, rb: dict, ratio: float = BUNDLE_BASE_RATIO) -> float:
    """Ladder base in CENTS for the two cars: ratio * (refA + refB); the opening fee is 0.9 of it,
    i.e. ~0.79*(refA+refB) as in the archived worlds. Kept in cents because steep()/FeeStep use cents."""
    return max(BUNDLE_MIN_FEE, ratio * (ra["referenceFeeCents"] + rb["referenceFeeCents"]))


def bundle_schedule(now: int, last_pickup: int, base: float, fee_open: float = 0.9,
                    fee_steps: tuple = ((24, 1.05), (16, 1.25), (10, 1.55), (6, 2.0))) -> list[dict]:
    """The same rising ladder the single postings use, from the pair's tightest last pickup."""
    steps = [(now, fee_open)]
    for hours_before, mult in fee_steps:
        at = last_pickup - hours_before * HOUR
        if at <= now:
            steps[0] = (now, max(steps[0][1], mult))
        else:
            steps.append((at, mult))
    out: list[dict] = []
    for at, mult in steps:
        fee = max(1, int(base * mult + 0.5))
        if out and fee <= out[-1]["feeCents"]:
            continue
        out.append({"at": int(at), "feeCents": fee})
    return out
