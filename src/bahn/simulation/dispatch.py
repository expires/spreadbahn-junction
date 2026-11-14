"""Dispatcher v1: margin-maximising quotes with an online competitor model, slack-aware fee schedules,
and an SLA controller. Built on the kit's starter for bookkeeping (rejoin, reposts, notes, final loop).

    python -m bahn.simulation.dispatch --seedlist nw-1,nw-2 --tag t   # practice sweep at level 8
    python -m bahn.simulation.dispatch --board --levels 8,7           # this round's ranking runs
    python -m bahn.simulation.dispatch --final                        # the live final
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor as ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from typing import Optional

from bahn.models import nemotron_notes, notes
from bahn.models.regime import PriorMix, Regime
from bahn.paths import ROOT
from spread import starter
from spread.client import Client, SpreadError, Unreachable
from spread.starter import DAY, HOUR, WAKE_ON, Desk, steep, undelivered
from spread.types import FeeStep, Order, PriceRequest

LIVE: set = set()
REGISTRY = os.path.join(ROOT, "runs", "practice_worlds.txt")  # practice worlds this process created and has not finished: deleted on exit


def _cleanup() -> None:
    if not LIVE:
        return
    client = Client()
    for wid in list(LIVE):
        try:
            client.world(wid).delete()
            print(f"cleanup: deleted unfinished practice world {wid}", file=sys.stderr)
        except SpreadError:
            pass


import atexit, signal  # noqa: E401,E402
atexit.register(_cleanup)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
signal.signal(signal.SIGHUP, lambda *_: sys.exit(1))
OPTIONS = ("same_day", "next_day", "within_3_days")


@dataclass(frozen=True)
class Policy:
    # Competitor: P(win) = sigmoid(slope * (log(comp * list) - log(price))). Fitted on exploration runs.
    comp0: float = 1.37          # prior competitor price level, x list
    slope: float = 8.7
    comp_lr: float = 0.02        # online learning rate on log(comp)
    comp_lo: float = 1.22        # level 8 range from the rules: 1.26-1.54 (with day factors around it)
    comp_hi: float = 1.60
    # Fees, as multiples of the typical fee, stepped by hours before lastPickupAt.
    fee_open: float = 0.9
    # Dynamic hedge on the 90% rule: spend the lateness budget only while the live projection says it is spare.
    hedge: bool = False
    hedge_below: float = 0.035   # aggressive while projected late share is below this (the rule allows 0.10)
    hedge_fee: float = 0.9       # fee multiple while aggressive
    hedge_lambda: float = 40.0   # late-order shadow price while aggressive
    hedge_edge_safe: bool = True # never aggressive on orders due on a Friday or the last Monday (no catch-up day)
    price_mult: float = 1.0      # probe lever: scales every quoted price after optimisation
    fee_mult: float = 0.95       # scales every posted fee schedule (risk-on: +€610/wk with note_fee 1.0, worst week 90.6% on 44 wks)
    tradein_fee: float = 1.0     # fee-estimate multiple for a trade-in (two cars, one price): realised −€18/order at 1.0
    private_fee: float = 1.3     # fee-estimate multiple when a stop is a private address (realised €10/order vs €39)
    note_fee: float = 1.0        # fee-estimate multiple when the request has a note (attribution: realised fee/typical
                                 # 1.03 with a note vs 1.04 without, singles, 8 weeks; we win 40% of quoted notes vs 54%)
    fee_open_tiers: tuple = ()   # ((supply index at least, opening multiple), ...), highest first; () = flat fee_open
    fee_steps: tuple = ((24, 1.05), (16, 1.25), (10, 1.55), (6, 2.0))
    hold_hours: float = 44.0     # post an order this many hours before lastPickupAt (44 h: +€213/wk vs 36 h on 44 weeks)
    post_clock: float = -1.0     # >= 0: post at this hour of day (e.g. 8.0) instead of a fixed lead (-1 = off)
    post_min_lead: float = 30.0  # with post_clock: at least this many hours before the last pickup     # post an order only this many hours before lastPickupAt
    # Cost model: expected fee ratio and P(late) as functions of slack hours at the quote.
    exp_fee: tuple = ((4, 2.0), (8, 1.55), (16, 1.25), (30, 1.05), (1e9, 0.95))
    p_late: tuple = ((4, 0.6), (8, 0.3), (16, 0.12), (30, 0.06), (1e9, 0.03))
    late_cost: float = 0.45      # refund + lost trust, as a share of the price
    pl_scale: float = 0.6        # calibration multiplier on the modelled P(late)
    late_shadow: float = 80.0    # euros: the shadow price of the 90% minimum, per expected late order
    km_late: float = 0.0         # extra P(late) per km (now inside the supply model)
    # P(late) from driver supply, fitted on v5 runs: logit = b0 + bp*log1p(S_pickup) + bd*log1p(S_delivery) + bk*km/100,
    # S = sum over towns of resident drivers * exp(-road minutes / 60).
    sup_b: tuple = (4.23, -0.816, -0.469, -0.425)
    sup_base: float = 0.045      # the model's typical P(late), already in the slack table
    sup_fee: float = 0.6         # fee uplift per unit of supply-model P(late)
    # Congestion: cars we pick up in one town on one day against what its drivers can move.
    cap_per_supply: float = 0.12  # cars per day per unit of supply index
    cap_min: float = 3.0
    cong_late: float = 0.35      # extra P(late) per unit of overload
    # Direct offers when an order gets its deadline warning without a driver.
    offers: bool = False
    trust_invest: float = 0.0    # weight on the value of dealer trust (level 8): 1% of expected future revenue per on-time order (0 = off)
    pair_quote: float = 0.25     # backhaul arbitrage: fee discount on a request that pairs with a held order (0 = off)
    viewer_offers: bool = False  # before a public raise, offer the next fee privately to drivers who viewed the gig recently
    viewer_lead: int = 40        # minutes before the raise
    viewer_recent: int = 240     # a view counts if this recent (minutes)
    chain_offers: bool = False   # offer a nearby open gig to a driver who just finished one of ours (known to be working)
    chain_reach: int = 45        # road minutes from the drop-off to the next pickup
    chain_fee: float = 1.0       # x the gig's current app fee
    low_fee_scale: float = 0.75  # levels 1-3 (no competitor, many drivers): fees and fee estimates scale by this
    mid_fee_scale: float = 0.88  # levels 4-6
    regime: bool = True          # Bayesian day level + price-war mixture + outage detection (regime.py)
    price_slope: float = 10.5    # dealer price slope used with the regime learner
    # Bid-price control: the minimum margin follows how full the deadline day already is (idle capacity -> take
    # thin cars; full -> only good ones). utilisation = legs due that day / day capacity (day_cap per 100 drivers).
    bar_adapt: bool = False
    bar_util_ref: float = 0.8    # utilisation at which the bar equals min_margin
    bar_lo: float = 0.25         # floor of the bar as a share of min_margin (idle days)
    bar_hi: float = 1.6          # ceiling (full days)
    regime_sd0: float = 0.07     # prior sd of the competitor's log level: wider = learns a cheap/dear week faster
    # "Learn fast early, then settle" (regime.py, warmup_eval.py), all off by default:
    regime_mix: float = 0.0      # prior weight of a second, wide-prior learner (PriorMix): Bayes hands it the weight only
    regime_mix_sd: float = 0.20  # when the week is far from the prior (sd of its log level prior)
    regime_warm_n: int = 0       # extra level variance on the session's first n outcomes, decaying to 0 (0 = off)
    regime_warm_q: float = 0.03  # its sd per outcome at the start
    dealer_fx: bool = False      # add persistent per-dealer price effects (tested: −€229, off)
    lanes_v9: bool = False       # the v9 branch's per-lane competitor factors (798 lanes; tested: +€41, off)
    late_v9: bool = True         # P(late) from the v9 branch's archive-fitted model (74,731 orders, OOF AUC 0.79)
    late_v9_scale: float = 1.0   # calibration multiplier on it
    late_v9_extras: bool = True  # keep our day-capacity, edge-day, congestion and snow terms on top
    late_v9_day_scale: float = 1.0    # separate weight on the day-capacity term (-1 = same as late_v9_extra_scale)
    late_v9_extra_scale: float = 0.3  # weight on those terms (their model already sees backlog and weather): +€848/wk
    lanes: bool = True           # use the persistent pickup/delivery price effects (lane_effects.json)
    # Multi-car gigs: one driver takes car A, then car B from near A's delivery.
    notes_v2: bool = True             # the team's note parser (notes.py) instead of the kit's keyword reader
    notes_llm: bool = False           # Nemotron 3.5 Super VL reads the note too; safer reading per field wins (nemotron_notes.py)
    combine: bool = True
    pair_alpha_return: float = 0.75   # fee of a pair whose second car ends near the first's start, x (refA + refB)
    pair_alpha_chain: float = 0.82    # any other feasible pair
    pair_hop: int = 30                # max road minutes from A's delivery to B's pickup
    pair_return: int = 45             # B ending within this of A's pickup counts as a return
    pair_chains: bool = True          # also pair cars whose second leg does not come back
    pair_max_drive: int = 240         # total planned driving of a pair, minutes (friend's v9 finding: tight caps, 280)
    pair_dead_gap: float = 18.0       # only pair orders whose deadlines are at most this many hours apart (v9: 18)
    snow_late: float = 0.15      # extra P(late) for a leg touching a snowed place on its pickup day
    snow_fee: float = 0.15       # fee uplift for such a leg
    snow_km: float = 0.08        # and this much more per 100 km of the leg: a long drive into snow rarely finds a driver
    offer_fee: float = 2.0       # x typical fee
    offer_reach: int = 150       # only drivers living within this many road minutes of the pickup
    max_km: float = 520.0        # decline longer lanes
    min_margin: float = 6.0      # euros of expected margin to quote an option at all
    # SLA controller.
    sla_target: float = 0.94
    sla_gain: float = 2.0        # how hard projected lateness raises the margin bar
    sla_prior_n: int = 120       # orders assumed on time, so early noise cannot trip the controller
    decline_mult: float = 4.0    # a declined option is quoted at this x list
    # Day capacity: legs due (by deadline day) against the whole roster. Fridays and the last Monday
    # collect the next-day and 3-day work of the days before them, and a flu or snow day on a full day
    # leaves dozens of cars without a driver (ho-3: 136 legs due on a flu Friday, 39 late).
    day_cap: float = 10.0        # legs per day per 100 drivers before P(late) rises (0 = off)
    day_late: float = 0.75       # extra P(late) per unit of day overload (before pl_scale)
    day_pend: float = 0.6        # an open quote counts as this share of its legs (a bulk order is quoted all at once)
    # Edge days: a car due on a Friday or the last Monday has no next business day to catch up on, and
    # long lanes then run late four times as often as midweek (17% vs 4%, 40 runs). Extra P(late) by km:
    edge_late: tuple = (0.07, 0.05, 0.2)  # under 150 km, 150-300 km, over 300 km (before pl_scale)


def step_lookup(table: tuple, x: float) -> float:
    for bound, value in table:
        if x <= bound:
            return value
    return table[-1][1]


class Road:
    def __init__(self, wmap: dict):
        self.r = {(x["from"], x["to"]): x for x in wmap["roads"]}
        self.places = wmap.get("places", [])

    def minutes(self, a: str, b: str) -> int:
        return self.r[(a, b)]["minutes"]

    def km(self, a: str, b: str) -> float:
        return self.r[(a, b)]["km"]


class Dispatcher(Desk):
    def __init__(self, world, level, now, policy: Policy, road: Road):
        super().__init__(world, level, now)
        self.supply: dict[str, float] = {}
        self.qlog: list[dict] = []               # per quoted option: the model's predictions, for calibration
        self.load: dict[tuple, float] = {}       # (pickup place, day) -> expected cars
        self.snow: list[tuple] = []              # (from, to, set of place ids) from forecasts
        self.pending: dict[str, tuple] = {}      # request id -> (key, weight) of an open quote
        self.p, self.road = policy, road
        # The competitor's price level by level, from the rules: near list up to 6, 1.12-1.38 at 7, 1.26-1.54 at 8.
        self.comp_lo, self.comp_hi, comp0 = {7: (1.05, 1.45, 1.25), 8: (policy.comp_lo, policy.comp_hi, policy.comp0)}.get(level, (0.85, 1.2, 1.0))
        self.log_comp = math.log(comp0)
        self.regime = None
        if policy.regime and level >= 8 and road.places:
            kw = dict(level0=math.log(comp0), first_day=now // DAY, warm_n=policy.regime_warm_n, warm_q=policy.regime_warm_q)
            self.regime = Regime(road.places, sd_level0=policy.regime_sd0, **kw)
            if policy.regime_mix > 0:
                self.regime = PriorMix([self.regime, Regime(road.places, sd_level0=policy.regime_mix_sd, **kw)],
                                       [1 - policy.regime_mix, policy.regime_mix])
        self.lanes = json.load(open(os.path.join(ROOT, "configs", "lane_effects.json"))) if policy.lanes else None
        self.lanes9 = json.load(open(os.path.join(ROOT, "configs", "lane_effects_v9.json"))) if policy.lanes_v9 else None
        self.fee_scale = policy.low_fee_scale if level <= 3 else policy.mid_fee_scale if level <= 6 else 1.0
        self.day_of_comp = now // DAY
        self.quoted: dict[str, tuple[float, int]] = {}  # request id -> (log ratio of cheapest quote, day)
        self.won = self.late = self.on_time = 0
        self.stats = {"quoted": 0, "declined": 0, "won": 0, "lost": 0}
        self.timers: list[tuple[int, str]] = []  # (minute, order id): held orders to post later
        self.trust: dict[str, int] = {}  # dealer -> orders delivered on time, as the rules count it (max 25, halved on a late one)
        self.settled: set[str] = set()
        self.dealer_seen: dict[str, int] = {}
        self.won_rev: list[float] = []  # euros of each won order, for the average
        self.gig_driver: dict[str, str] = {}
        self.viewers: dict[str, list] = {}       # gig id -> [(minute, driver id)]
        self.viewer_done: set = set()            # (gig id, raise minute) already offered
        self.gig_delivered: dict[str, int] = {}
        self.pend_day: dict[str, tuple[int, float]] = {}  # request id -> (deadline day, weight) of an open quote

    # ---------- the SLA controller ----------
    def aggressive(self, deadline: Optional[int] = None) -> bool:
        """The hedge: True while the projected late share leaves plenty of room under the 10% allowance,
        and (optionally) never for an order due on an edge day."""
        if not self.p.hedge:
            return False
        if self.p.hedge_edge_safe and deadline is not None and (deadline // DAY % 7 == 4 or deadline // DAY == 7):
            return False
        projected = self.sla_pressure() + (1 - self.p.sla_target)
        on = projected < self.p.hedge_below
        self.stats["hedge_on" if on else "hedge_off"] = self.stats.get("hedge_on" if on else "hedge_off", 0) + 1
        return on

    def sla_pressure(self) -> float:
        """Projected late share minus the allowance, from orders delivered late and orders now at risk."""
        n = max(len(self.orders), self.p.sla_prior_n)
        late_now = at_risk = 0.0
        for o in self.orders.values():
            if o["status"] == "cancelled":
                continue
            done = all(t is not None for t in o["deliveredAt"])
            if any(t is not None and t > o["deadline"] for t in o["deliveredAt"]) or (not done and self.now > o["deadline"]):
                late_now += 1
            elif not done and not self.has_driver(o["id"]) and o["id"] not in self.held():
                left = (o["lastPickupAt"] - self.now) / 60
                at_risk += 0.8 if o["id"] in self.urgent else 0.3 if left < 4 else 0.0
        projected = (late_now + at_risk) / n
        return projected - (1 - self.p.sla_target)

    def has_driver(self, order_id: str) -> bool:
        gigs = self.gigs_of(order_id)
        return bool(gigs) and all(g in self.booked for g in gigs)

    # ---------- driver supply ----------
    def init_supply(self, roster: list[dict]) -> None:
        self.roster = roster
        self.v9_model = self.v9_struct = None
        if self.p.late_v9:
            from bahn.models import v9_supply
            blob = json.load(open(os.path.join(ROOT, "configs", "fits", "late2.json")))
            self.v9_model = blob
            self.v9_struct = v9_supply.build_supply(roster, self.road)
        self.offers_today: dict[tuple, int] = {}   # (driver, day) -> offers sent
        self.offered: dict[str, set] = {}          # gig id -> drivers already asked
        self.open_offers: dict[str, int] = {}      # gig id -> offers awaiting an answer
        homes: dict[str, int] = {}
        for d in roster:
            homes[d["homePlaceId"]] = homes.get(d["homePlaceId"], 0) + 1
        places = {a for a, _ in self.road.r}
        self.supply = {p: sum(n * math.exp(-self.road.minutes(h, p) / 60) for h, n in homes.items()) for p in places}

    def v9_p_late(self, request: PriceRequest, option: dict) -> float:
        """The v9 branch's fitted lateness logit on its supply features (standardised, coefficients in late2.json)."""
        from bahn.models import v9_supply
        outstanding = sum(1 for o in self.orders.values() if any(t is None for t in o["deliveredAt"]))
        weather = 1.0 if self.snow_risk(request, option["deadline"]) > 0 else 0.0
        feats = v9_supply.order_features(request, option, self.road, self.v9_struct, outstanding=outstanding, weather=weather)
        m = self.v9_model
        z = m["intercept"]
        for name, mu, sc, c in zip(m["features"], m["mean"], m["scale"], m["coef"]):
            z += c * (float(feats.get(name, 0.0)) - mu) / (sc or 1.0)
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def supply_late(self, request: PriceRequest) -> float:
        if not self.supply:
            return self.p.sup_base
        b0, bp, bd, bk = self.p.sup_b
        worst = 0.0
        for l in request["legs"]:
            a, b = l["pickup"]["placeId"], l["delivery"]["placeId"]
            z = b0 + bp * math.log1p(self.supply.get(a, 0)) + bd * math.log1p(self.supply.get(b, 0)) + bk * self.road.km(a, b) / 100
            worst = max(worst, 1 / (1 + math.exp(-z)))
        return worst

    def snow_risk(self, request: PriceRequest, deadline: int) -> float:
        """Snow slows driving and keeps drivers home: a leg touching a snowed place between its pickup and deadline is riskier."""
        if not self.snow or not self.p.snow_late:
            return 0.0
        for leg in request["legs"]:
            start = leg["pickup"]["window"]["from"]
            places = {leg["pickup"]["placeId"], leg["delivery"]["placeId"]}
            if any(f < deadline and t > start and places & ps for f, t, ps in self.snow):
                return self.p.snow_late + self.p.snow_km * self.road.km(leg["pickup"]["placeId"], leg["delivery"]["placeId"]) / 100
        return 0.0

    def load_key(self, request: PriceRequest) -> tuple:
        leg = request["legs"][0]
        return (leg["pickup"]["placeId"], leg["pickup"]["window"]["from"] // DAY)

    def congestion(self, request: PriceRequest) -> float:
        key = self.load_key(request)
        cap = max(self.p.cap_min, self.p.cap_per_supply * self.supply.get(key[0], 0))
        over = (self.load.get(key, 0.0) + len(request["legs"])) / cap - 1
        return self.p.cong_late * max(0.0, over)

    # ---------- day capacity ----------
    def day_loads(self) -> dict[int, float]:
        """Legs due by deadline day: won orders, and open quotes at their weight."""
        out: dict[int, float] = {}
        for o in self.orders.values():
            d = o["deadline"] // DAY
            out[d] = out.get(d, 0.0) + len(o["legs"])
        for d, w in self.pend_day.values():
            out[d] = out.get(d, 0.0) + w
        return out

    def day_capacity(self, day: int) -> float:
        return self.p.day_cap * len(getattr(self, "roster", []) or []) / 100

    def day_risk(self, loads: dict[int, float], day: int, legs: int) -> float:
        if not self.p.day_cap:
            return 0.0
        cap = self.day_capacity(day)
        if cap <= 0:
            return 0.0
        return self.p.day_late * max(0.0, (loads.get(day, 0.0) + legs) / cap - 1)

    def edge_risk(self, deadline: int, km: float) -> float:
        day = deadline // DAY
        if day % 7 != 4 and day != 7:
            return 0.0
        return self.p.edge_late[0 if km < 150 else 1 if km < 300 else 2]

    def add_load(self, request_id: str, outcome: Optional[bool]) -> None:
        if request_id not in self.pending:
            return
        key, w = self.pending.pop(request_id)
        self.load[key] = self.load.get(key, 0.0) + ((len(self.requests[request_id]["legs"]) - w) if outcome else -w)

    # ---------- quotes ----------
    def slack_hours(self, request: PriceRequest, deadline: int) -> float:
        """Hours from posting (at respondBy) to the last pickup that still makes the deadline."""
        drive = sum(self.road.minutes(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in request["legs"])
        last_pickup = deadline - drive - 30
        return (last_pickup - request["respondBy"]) / 60

    def quote_fixed(self, request: PriceRequest) -> None:
        """Levels 1-3: every quote wins the first option at its list price, so the only choice is
        whether to quote. Declining means sending nothing (a high quote would win)."""
        p, o = self.p, request["options"][0]
        ref, lst = request["referenceFeeCents"] / 100, o["listPriceCents"] / 100
        km = sum(self.road.km(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in request["legs"])
        pressure = max(0.0, self.sla_pressure())
        slack = self.slack_hours(request, o["deadline"])
        sl = self.supply_late(request) + self.congestion(request)
        fee = self.fee_scale * ref * step_lookup(p.exp_fee, slack) * (p.note_fee if request["note"] else 1.0) * (1 + p.sup_fee * max(0.0, sl - p.sup_base))
        pl = min(0.95, p.pl_scale * (step_lookup(p.p_late, slack) + max(0.0, sl - p.sup_base)) + pressure)
        lam = p.hedge_lambda if self.aggressive(o["deadline"]) else p.late_shadow
        margin = lst * (1 - pl * p.late_cost) - fee - pl * lam * (1 + p.sla_gain * pressure * 10)
        if km > p.max_km or margin < p.min_margin * (1 + p.sla_gain * pressure * 10):
            self.stats["declined"] += 1
            return
        self.stats["quoted"] += 1
        key = self.load_key(request)
        self.load[key] = self.load.get(key, 0.0) + len(request["legs"])
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": {o["kind"]: o["listPriceCents"]}})

    def quote(self, request: PriceRequest) -> None:
        self.requests[request["id"]] = request
        if self.p.notes_llm and request.get("note"):
            nemotron_notes.client().prefetch(request)  # async; the answer is in long before the post
        self.dealer_seen[request["dealerId"]] = self.dealer_seen.get(request["dealerId"], 0) + 1
        if self.level <= 3:
            return self.quote_fixed(request)
        p = self.p
        tv = self.trust_value(request)
        ref = request["referenceFeeCents"] / 100
        km = sum(self.road.km(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in request["legs"])
        if km > p.max_km:
            self.stats["declined"] += 1
            self.stats["d_km"] = self.stats.get("d_km", 0) + 1
            self.outbox.append({"type": "quote", "requestId": request["id"],
                                "prices": {o["kind"]: int(o["listPriceCents"] * p.decline_mult) for o in request["options"]}})
            return
        pressure = max(0.0, self.sla_pressure())
        bar = p.min_margin * (1 + p.sla_gain * pressure * 10)
        boost = 1 + 0.01 * self.trust.get(request["dealerId"], 0) if self.level >= 8 else 1.0
        lane = self.lane_offset(request)
        if self.regime:
            comp = math.exp(self.regime.level(self.now) + lane + self.regime.adjust(request, self.now)) * boost
        else:
            comp = math.exp(self.log_comp + lane) * boost
        slope = p.price_slope if self.regime else p.slope
        prices: dict[str, int] = {}
        best_lr = None
        loads = self.day_loads() if p.day_cap else {}
        nlegs = len(request["legs"])
        for o in request["options"]:
            lst = o["listPriceCents"] / 100
            lam = p.hedge_lambda if self.aggressive(o["deadline"]) else p.late_shadow
            slack = self.slack_hours(request, o["deadline"])
            sl = self.supply_late(request) + self.congestion(request) + self.snow_risk(request, o["deadline"]) \
                + self.day_risk(loads, o["deadline"] // DAY, nlegs) + self.edge_risk(o["deadline"], km)
            fee = self.fee_scale * ref * step_lookup(p.exp_fee, slack) * (p.note_fee if request["note"] else 1.0) * (1 + p.sup_fee * max(0.0, sl - p.sup_base))
            if len(request["legs"]) > 1:
                fee *= p.tradein_fee
            if any(l["pickup"]["privateAddress"] or l["delivery"]["privateAddress"] for l in request["legs"]):
                fee *= p.private_fee
            if p.pair_quote and self.has_backhaul(request, o):
                fee *= 1 - p.pair_quote
            if p.late_v9 and self.v9_model is not None:
                extra = (self.congestion(request) + self.snow_risk(request, o["deadline"])
                         + self.edge_risk(o["deadline"], km)) if p.late_v9_extras else 0.0
                day = self.day_risk(loads, o["deadline"] // DAY, nlegs) if p.late_v9_extras else 0.0
                day_w = p.late_v9_extra_scale if p.late_v9_day_scale < 0 else p.late_v9_day_scale
                pl = min(0.95, p.late_v9_scale * self.v9_p_late(request, o)
                         + p.pl_scale * (p.late_v9_extra_scale * extra + day_w * day) + pressure)
            else:
                pl = min(0.95, p.pl_scale * (step_lookup(p.p_late, slack) + p.km_late * km + max(0.0, sl - p.sup_base)) + pressure)
            best, best_x = -1e9, None
            for i in range(60):
                x = 0.85 + i * 0.02  # price as x list
                price = lst * x
                win = 1 / (1 + math.exp(slope * (math.log(x) - math.log(comp))))
                margin = price * (1 - pl * p.late_cost) - fee - pl * lam * (1 + p.sla_gain * pressure * 10) + tv[0] * (1 - pl) - tv[1] * pl
                if margin * win > best:
                    best, best_x, best_margin = margin * win, x, margin
            bar_o = bar
            if p.bar_adapt and p.day_cap:
                cap = self.day_capacity(o["deadline"] // DAY)
                util = (loads.get(o["deadline"] // DAY, 0.0) + nlegs) / cap if cap > 0 else 1.0
                bar_o = bar * min(p.bar_hi, max(p.bar_lo, util / p.bar_util_ref))
            if best_x is not None and best_margin >= bar_o:
                self.qlog.append({"rid": request["id"], "opt": o["kind"], "pl": round(pl, 4), "sl": round(sl, 4), "fee": round(fee, 2),
                                  "x": round(best_x, 3), "slack": round(slack, 1), "pressure": round(pressure, 4)})
                prices[o["kind"]] = int(lst * best_x * p.price_mult * 100)
                lr = math.log(best_x)
                best_lr = lr if best_lr is None else min(best_lr, lr)
            else:
                prices[o["kind"]] = int(o["listPriceCents"] * p.decline_mult)
        if best_lr is None:
            self.stats["declined"] += 1
            self.stats["d_bar" if pressure > 0 else "d_margin"] = self.stats.get("d_bar" if pressure > 0 else "d_margin", 0) + 1
        else:
            self.stats["quoted"] += 1
            key, w = self.load_key(request), 0.6 * len(request["legs"])
            self.pending[request["id"]] = (key, w)
            self.load[key] = self.load.get(key, 0.0) + w
            if p.day_cap:  # the open quote weighs on the day of its fastest option offered
                kind = next(o for o in request["options"] if prices[o["kind"]] < o["listPriceCents"] * 3)
                self.pend_day[request["id"]] = (kind["deadline"] // DAY, p.day_pend * nlegs)
            # Learn the competitor only where we offered the dealer's fastest option: without it,
            # dealers turn to the competitor at any price, and the estimate would sink.
            fastest = request["options"][0]
            if prices[fastest["kind"]] < fastest["listPriceCents"] * 3:
                self.quoted[request["id"]] = (best_lr - math.log(boost) - lane, self.now // DAY)
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})

    def lane_offset(self, request: PriceRequest) -> float:
        """Log price offset of the competitor on this lane, persistent across worlds (fitted on 39 worlds)."""
        leg = request["legs"][0]
        if self.lanes9:
            total = 0.0
            for l in request["legs"]:
                a, b = l["pickup"]["placeId"], l["delivery"]["placeId"]
                v = self.lanes9["lane_factor"].get(f"{a}>{b}")
                if v is None:
                    fb = self.lanes9["fallback"]
                    v = fb.get("const", 0.0) + fb["origin"].get(a, 0.0) + fb["dest"].get(b, 0.0)
                total += v
            return total
        if not self.lanes:
            return 0.0
        off = self.lanes["pickup"].get(leg["pickup"]["placeId"], 0.0) + self.lanes["delivery"].get(leg["delivery"]["placeId"], 0.0)
        if self.p.dealer_fx:
            off += self.lanes.get("dealer", {}).get(request["dealerId"], 0.0)
        return off

    def trust_value(self, request: PriceRequest) -> tuple[float, float]:
        """(gain if delivered on time, loss if late) in euros of this week's future margin with this dealer.
        Each on-time order lets the dealer accept 1% higher prices from us (max 25); a late one halves the count."""
        if not self.p.trust_invest or self.level < 8:
            return (0.0, 0.0)
        end_quotes = 4 * DAY + 18 * HOUR                       # Friday 18:00: no more requests after
        hours_left = max(0.0, (end_quotes - self.now) / 60)
        biz_left = hours_left * 10 / 24                         # requests come mostly 09-18 on weekdays
        hours_gone = max(10.0, (self.now - 8 * HOUR) / 60 * 10 / 24)
        rate = self.dealer_seen.get(request["dealerId"], 1) / hours_gone      # requests per business hour
        avg = sum(self.won_rev) / len(self.won_rev) if self.won_rev else 95.0
        future = rate * biz_left * 0.4 * avg                   # ~40% win rate on their requests
        count = self.trust.get(request["dealerId"], 0)
        gain = 0.01 * future if count < 25 else 0.0
        loss = 0.01 * (count - count // 2) * future
        return (self.p.trust_invest * gain, self.p.trust_invest * loss)

    def learn(self, request_id: str, won: bool) -> None:
        """One SGD step of the logistic likelihood on log(comp)."""
        if request_id not in self.quoted:
            return
        lr, _ = self.quoted.pop(request_id)
        if self.regime:
            self.stats["won" if won else "lost"] += 1
            self.regime.observe(self.requests[request_id], lr, won, self.now)
            self.log_comp = self.regime.level(self.now)
            return
        pwin = 1 / (1 + math.exp(self.p.slope * (lr - self.log_comp)))
        self.log_comp += self.p.comp_lr * ((1.0 if won else 0.0) - pwin)
        self.log_comp = min(math.log(self.comp_hi), max(math.log(self.comp_lo), self.log_comp))
        self.stats["won" if won else "lost"] += 1

    def handle(self, event) -> None:
        if event["kind"] == "order_won":
            self.won_rev.append(event["order"]["priceCents"] / 100)
            self.learn(event["order"]["id"], True)
            self.add_load(event["order"]["id"], True)
            self.pend_day.pop(event["order"]["id"], None)
        elif event["kind"] == "order_lost":
            self.learn(event["requestId"], False)
            self.add_load(event["requestId"], False)
            self.pend_day.pop(event["requestId"], None)
        if event["kind"] == "forecast":
            self.snow.append((event["from"], event["to"], set(event["placeIds"])))
        super().handle(event)
        if self.p.offers and event["kind"] == "deadline_close":
            self.offer_order(event["orderId"])
        elif event["kind"] == "offer_answered":
            self.open_offers[event["gigId"]] = max(0, self.open_offers.get(event["gigId"], 0) - 1)
            self.stats["offer_" + event["answer"]] = self.stats.get("offer_" + event["answer"], 0) + 1
            if self.p.offers and event["answer"] != "accepted":
                oid = self.posts.get(event["gigId"], {}).get("stops", [{}])[0].get("orderId")
                if oid:
                    self.offer_order(oid)
        if event["kind"] == "gig_booked":
            self.gig_driver[event["gigId"]] = event["driverId"]
        elif event["kind"] == "gig_viewed" and self.p.viewer_offers:
            self.viewers.setdefault(event["gigId"], []).append((event["at"], event["driverId"]))
        if event["kind"] == "delivered":
            self.settle(event["orderId"])
            if self.p.chain_offers:
                self.chain_offer(event)

    def settle(self, order_id: str) -> None:
        """Dealer trust at level 8: +1 per order on time, up to 25; a late order halves the count."""
        o = self.orders.get(order_id)
        if o is None or order_id in self.settled or any(t is None for t in o["deliveredAt"]):
            return
        self.settled.add(order_id)
        d = o["dealerId"]
        on_time = all(t <= o["deadline"] for t in o["deliveredAt"])
        self.trust[d] = min(25, self.trust.get(d, 0) + 1) if on_time else self.trust.get(d, 0) // 2

    def post(self, order, legs, retry=starter.Retry()) -> None:
        if not legs:
            return
        if self.p.combine and self.level >= 3 and retry == starter.Retry() and self.pairable(order):
            best = self.find_partner(order)
            if best is not None:
                return self.post_pair(*best)
        self.post_single(order, legs, retry)

    def post_single(self, order, legs, retry=starter.Retry()) -> None:
        if not self.p.notes_v2:
            return Desk.post(self, order, legs, retry)
        req = self.requests[order["id"]]
        N = nemotron_notes.client() if self.p.notes_llm else notes  # falls back to the rules within 2 s
        stops = starter.without_windows(N.stops_for(order, legs, req, retry, self.now), self.now)
        audience = N.audience_for(order, legs, req, retry)
        self.outbox.append({"type": "post", "stops": stops, "audience": audience, "schedule": self.fees_for(order, len(legs))})
        if len(legs) > 1:  # a trade-in over the 12-hour limit goes as one gig per car, as in the kit
            self.send()
            if not self.gigs_of(order["id"]):
                for leg in legs:
                    self.post_single(order, [leg], retry)

    # ---------- multi-car gigs ----------
    def pairable(self, o: Order) -> bool:
        if len(o["legs"]) != 1 or any(t is not None for t in o["deliveredAt"]):
            return False
        leg = o["legs"][0]
        return not (leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"])

    def has_backhaul(self, request: PriceRequest, option: dict) -> bool:
        """Would this request, won with this option, form a return pair with an order we hold unposted or unbooked?"""
        if len(request["legs"]) != 1 or not self.p.combine:
            return False
        leg = request["legs"][0]
        if leg["pickup"]["privateAddress"] or leg["delivery"]["privateAddress"]:
            return False
        drive = self.road.minutes(leg["pickup"]["placeId"], leg["delivery"]["placeId"])
        hypo = {"id": request["id"], "legs": request["legs"], "deadline": option["deadline"],
                "lastPickupAt": option["deadline"] - drive - 30, "deliveredAt": [None]}
        held = self.held()
        for b in self.orders.values():
            if not self.pairable(b) or b["id"] in self.urgent:
                continue
            gigs = self.gigs_of(b["id"])
            if gigs and (any(g in self.booked for g in gigs) or any(len(self.posts.get(g, {}).get("stops", [])) != 2 for g in gigs)):
                continue
            if not gigs and b["id"] not in held:
                continue
            for x, y in ((hypo, b), (b, hypo)):
                if self.pair_plan(x, y) and self.road.minutes(y["legs"][0]["delivery"]["placeId"], x["legs"][0]["pickup"]["placeId"]) <= self.p.pair_return:
                    return True
        return False

    def pair_plan(self, x: Order, y: Order) -> Optional[tuple]:
        """(score, alpha) if one driver can take x then y on time, by road minutes with buffers; else None."""
        X, Y = x["legs"][0], y["legs"][0]
        xp, xd, yp, yd = X["pickup"]["placeId"], X["delivery"]["placeId"], Y["pickup"]["placeId"], Y["delivery"]["placeId"]
        hop = self.road.minutes(xd, yp)
        if hop > self.p.pair_hop:
            return None
        xdrive, ydrive = self.road.minutes(xp, xd), self.road.minutes(yp, yd)
        if xdrive + hop + ydrive > min(12 * 60 - 30, self.p.pair_max_drive):
            return None
        if abs(x["deadline"] - y["deadline"]) > self.p.pair_dead_gap * 60:
            return None
        start = max(self.now + 120, X["pickup"]["window"]["from"])
        if start > x["lastPickupAt"] - 60:
            return None
        xdel = start + xdrive + (40 if X["car"]["electric"] and (X["car"]["chargePercent"] or 100) < 30 else 0)
        if xdel > x["deadline"] - 60:
            return None
        ypick = max(xdel + hop, Y["pickup"]["window"]["from"])
        if ypick > y["lastPickupAt"] - 60 or ypick + ydrive > y["deadline"] - 60:
            return None
        back = self.road.minutes(yd, xp)
        if back > self.p.pair_return and not self.p.pair_chains:
            return None
        alpha = self.p.pair_alpha_return if back <= self.p.pair_return else self.p.pair_alpha_chain
        return (back + hop, alpha)

    def find_partner(self, a: Order) -> Optional[tuple]:
        best = None
        held = self.held()
        for b in self.orders.values():
            if b["id"] == a["id"] or not self.pairable(b) or b["id"] in self.urgent:
                continue
            gigs = self.gigs_of(b["id"])
            if gigs and (any(g in self.booked for g in gigs) or any(len(self.posts.get(g, {}).get("stops", [])) != 2 for g in gigs)):
                continue  # booked, or already in a pair
            if not gigs and b["id"] not in held:
                continue  # not won-and-waiting in our books
            for x, y in ((a, b), (b, a)):
                plan = self.pair_plan(x, y)
                if plan and (best is None or plan[0] < best[0]):
                    best = (plan[0], x, y, plan[1])
        return None if best is None else best[1:]

    def post_pair(self, x: Order, y: Order, alpha: float) -> None:
        for o in (x, y):  # take an open single gig of either order out of the app first
            for g in self.gigs_of(o["id"]):
                self.outbox.append({"type": "withdraw", "gigId": g})
            self.leg_gigs.pop(o["id"], None)
        self.timers = [t for t in self.timers if t[1] not in (x["id"], y["id"])]
        stops = []
        audience = {"experienced": False, "manualLicence": False}
        for o in (x, y):
            req = self.requests[o["id"]]
            if self.p.notes_v2:
                N = nemotron_notes.client() if self.p.notes_llm else notes
                stops += starter.without_windows(N.stops_for(o, [0], req, starter.Retry(), self.now), self.now)
                a = N.audience_for(o, [0], req, starter.Retry())
            else:
                stops += starter.without_windows(starter.stops_for(o, [0], req["note"], starter.Retry()), self.now)
                a = {"experienced": o["legs"][0]["car"]["premium"], "manualLicence": False}
            audience = {k: audience[k] or a[k] for k in audience}
        ref = alpha * (self.requests[x["id"]]["referenceFeeCents"] + self.requests[y["id"]]["referenceFeeCents"])
        sched = self.schedule(ref, min(x["lastPickupAt"], y["lastPickupAt"]), x["legs"][0]["pickup"]["placeId"], max(x["deadline"], y["deadline"]))
        self.outbox.append({"type": "post", "stops": stops, "audience": audience, "schedule": sched})
        self.send()
        if not self.gigs_of(x["id"]):  # refused: post each alone
            self.stats["pair_refused"] = self.stats.get("pair_refused", 0) + 1
            for o in (x, y):
                self.post_single(o, [0])
        else:
            self.stats["pairs"] = self.stats.get("pairs", 0) + 1

    def send_post_of(self, order_id: str) -> None:
        if any(a["type"] == "post" and any(s["orderId"] == order_id for s in a["stops"]) for a in self.outbox):
            self.send()

    def withdraw(self, order_id: str) -> bool:
        """As the kit's, and a partner that shared the withdrawn gig is posted again on its own."""
        self.send_post_of(order_id)
        gigs = self.gigs_of(order_id)
        if any(g in self.booked for g in gigs):
            return False
        partners = {s["orderId"] for g in gigs for s in self.posts.get(g, {}).get("stops", [])} - {order_id}
        ok = super().withdraw(order_id)
        for pid in partners:
            self.leg_gigs.pop(pid, None)
            if pid in self.orders:
                self.post_single(self.orders[pid], undelivered(self.orders[pid]))
        return ok

    def rejoin(self, snapshot, history) -> None:
        """The kit's rejoin posts every waiting order without a gig at once. Orders that win() would still hold go back
        on a timer instead. Its first posts come as post(order, legs); a retry after a failed visit passes a Retry."""
        post = self.post

        def first_post(order, legs, *retry):
            post_at = self.post_time(order)
            if not retry and order["id"] not in self.urgent and post_at > self.now + 30:
                self.timers.append((post_at, order["id"]))
            else:
                post(order, legs, *retry)
        self.post = first_post
        try:
            super().rejoin(snapshot, history)
        finally:
            del self.post

    # ---------- sending: the live loop's IO ----------
    pacer = None        # the final's action bucket, set by play_live; None in practice (no limit)

    def orders_of(self, action) -> set:
        """The orders an action touches: a post's stops (two orders for a pair), else the stops of the gig it names."""
        stops = action["stops"] if action["type"] == "post" else self.posts.get(action.get("gigId"), {}).get("stops", [])
        return {s["orderId"] for s in stops}

    def by_priority(self, actions: list) -> list:
        """Quotes first (15 world minutes, 4.5 real seconds), then the actions on orders past their deadline warning, then
        the rest, each class in outbox order. A post or withdraw never jumps ahead of an earlier post or withdraw that touches
        one of its orders (a withdraw, then the post that replaces it; the singles a pair post takes out of the app)."""
        keyed, touched = [], set()
        for i, a in enumerate(actions):
            orders = self.orders_of(a) if a["type"] != "quote" else set()
            moves = a["type"] in ("post", "withdraw")
            k = 0 if a["type"] == "quote" else 1 if orders & self.urgent and not (moves and orders & touched) else 2
            if moves:
                touched |= orders
            keyed.append((k, i, a))
        return [a for _, _, a in sorted(keyed, key=lambda x: x[:2])]

    def send(self, budget: Optional[int] = None) -> None:
        """Sends the outbox by priority, in batches of 50: all of it, or at most `budget` actions, and the rest waits
        in the outbox for the next tick. A quote past its request's respondBy would be refused, so it is dropped."""
        if not self.outbox:
            return
        keep = [a for a in self.outbox if a["type"] != "quote" or self.now <= self.requests.get(a["requestId"], {}).get("respondBy", self.now)]
        self.stats["late_quotes"] = self.stats.get("late_quotes", 0) + len(self.outbox) - len(keep)
        keep = self.by_priority(keep)
        n = len(keep) if budget is None else max(0, budget)
        actions, self.outbox = keep[:n], keep[n:]
        for i in range(0, len(actions), 50):
            batch = actions[i:i + 50]
            if self.pacer is not None:
                self.pacer.spend(len(batch))
            self.read_results(batch, self.act(batch))

    def act(self, batch: list) -> list:
        """world.act, without letting one bad action stop the desk: an action that does not parse (a 400 naming it, or a
        fraction that the client stops) is dropped and the others are sent again; another refusal drops the batch.
        Unreachable and 5xx go up, and the caller rebuilds from the server: a resent post could make a second gig."""
        results: list = [None] * len(batch)
        todo = list(range(len(batch)))
        while todo:
            try:
                for i, result in zip(todo, self.world.act([batch[i] for i in todo])):
                    results[i] = result
                break
            except Unreachable:
                raise
            except SpreadError as error:
                if error.status >= 500:
                    raise
                bad = re.match(r"\[(\d+)\]", error.field or "")
                if bad and int(bad[1]) < len(todo):
                    dropped = [todo.pop(int(bad[1]))]
                else:
                    dropped, todo = todo, []
                for i in dropped:
                    results[i] = {"ok": False, "reason": f"not sent: {error}"}
                self.stats["refused"] = self.stats.get("refused", 0) + len(dropped)
                print(f"live: dropped {len(dropped)} action(s) ({batch[dropped[0]]['type']}): {error}", file=sys.stderr, flush=True)
        return results

    def read_results(self, actions, results) -> None:
        super().read_results(actions, results)
        for action, result in zip(actions, results):
            if action["type"] == "offer" and result["ok"] is not True:
                g = action["gigId"]
                self.open_offers[g] = max(0, self.open_offers.get(g, 0) - 1)
                self.stats["offer_refused"] = self.stats.get("offer_refused", 0) + 1

    # ---------- chaining by direct offer ----------
    def current_fee(self, gig_id: str) -> int:
        sched = self.posts[gig_id]["schedule"]
        fee = sched[0]["feeCents"]
        for st in sched:
            if st["at"] <= self.now:
                fee = st["feeCents"]
        return fee

    def chain_offer(self, event) -> None:
        """A driver who just delivered our car's last stop is working today and stands at the drop-off:
        offer them our open gig that picks up nearest, before the app's raises do."""
        gid = event["gigId"]
        post = self.posts.get(gid)
        driver = self.gig_driver.get(gid)
        if post is None or driver is None:
            return
        self.gig_delivered[gid] = self.gig_delivered.get(gid, 0) + 1
        if self.gig_delivered[gid] < sum(1 for st in post["stops"] if st["kind"] == "delivery"):
            return  # the driver still has cars on this gig
        hour = self.now % DAY // 60
        if hour >= 21 or hour < 7 or self.offers_today.get((driver, self.now // DAY), 0) >= 3:
            return
        here = event["placeId"]
        best = None
        for oid, legs in self.leg_gigs.items():
            for g in set(legs.values()):
                if g in self.booked or g not in self.posts or self.open_offers.get(g, 0) >= 3 or driver in self.offered.get(g, set()):
                    continue
                order = self.orders.get(oid)
                first = next((st for st in self.posts[g]["stops"] if st["kind"] == "pickup"), None)
                if order is None or first is None or first["orderId"] != oid:
                    continue
                if self.posts[g]["audience"].get("experienced") or self.posts[g]["audience"].get("manualLicence"):
                    continue  # we do not know this driver's licence/rating here cheaply; keep it simple
                pick = order["legs"][first["leg"]]["pickup"]
                mins = self.road.minutes(here, pick["placeId"])
                if mins > self.p.chain_reach or pick["window"]["from"] > self.now + 180 or order["lastPickupAt"] < self.now + mins + 30:
                    continue
                key = (order["lastPickupAt"], mins)
                if best is None or key < best[0]:
                    best = (key, g)
        if best is None:
            return
        g = best[1]
        self.outbox.append({"type": "offer", "gigId": g, "driverId": driver, "feeCents": int(self.current_fee(g) * self.p.chain_fee)})
        self.offered.setdefault(g, set()).add(driver)
        self.offers_today[(driver, self.now // DAY)] = self.offers_today.get((driver, self.now // DAY), 0) + 1
        self.open_offers[g] = self.open_offers.get(g, 0) + 1
        self.stats["chain_offers"] = self.stats.get("chain_offers", 0) + 1

    # ---------- direct offers ----------
    def offer_order(self, order_id: str) -> None:
        """Offers each unbooked gig of the order to the nearest suitable drivers, up to 3 open offers per gig."""
        order = self.orders.get(order_id)
        if order is None or not getattr(self, "roster", None) or self.now >= order["lastPickupAt"]:
            return
        hour = self.now % DAY // 60
        if hour >= 22 or hour < 7:   # nobody reads offers 23:00-07:00
            return
        self.send_post_of(order_id)
        req = self.requests[order_id]
        for gig_id in self.gigs_of(order_id):
            if gig_id in self.booked or gig_id not in self.posts:
                continue
            post = self.posts[gig_id]
            first = next(s for s in post["stops"] if s["kind"] == "pickup")
            pickup = order["legs"][first["leg"]]["pickup"]["placeId"]
            need_manual, need_exp = post["audience"]["manualLicence"], post["audience"]["experienced"]
            cars = sum(1 for s in post["stops"] if s["kind"] == "pickup")
            fee = int(req["referenceFeeCents"] * cars / len(order["legs"]) * self.p.offer_fee)
            asked = self.offered.setdefault(gig_id, set())
            cands = []
            for d in self.roster:
                if d["id"] in asked or self.offers_today.get((d["id"], self.now // DAY), 0) >= 3:
                    continue
                if need_manual and d["licence"] != "manual":
                    continue
                if need_exp and (d["rating"] < 4.8 or d["completedGigs"] < 50):
                    continue
                mins = self.road.minutes(d["homePlaceId"], pickup)
                if mins <= self.p.offer_reach:
                    cands.append((mins + 20 * d["drops"], d["id"]))
            cands.sort()
            free = 3 - self.open_offers.get(gig_id, 0)
            for _, did in cands[:max(0, free)]:
                self.outbox.append({"type": "offer", "gigId": gig_id, "driverId": did, "feeCents": fee})
                asked.add(did)
                self.offers_today[(did, self.now // DAY)] = self.offers_today.get((did, self.now // DAY), 0) + 1
                self.open_offers[gig_id] = self.open_offers.get(gig_id, 0) + 1
                self.stats["offers"] = self.stats.get("offers", 0) + 1

    # ---------- holding orders until closer to pickup ----------
    def win(self, order: Order) -> None:
        if order["id"] in self.requests and order["id"] not in self.orders:
            self.orders[order["id"]] = order
            post_at = self.post_time(order)
            if post_at > self.now + 30:
                self.timers.append((post_at, order["id"]))
            else:
                self.post(order, undelivered(order))

    def post_time(self, order: Order) -> int:
        """When to put a won order in the app. Default: a fixed lead before its last pickup (hold_hours).
        With post_clock >= 0: at that hour of day, on the last day that still leaves post_min_lead hours
        before the last pickup, so every gig appears when drivers are browsing (not in the evening or at night)."""
        last = order["lastPickupAt"]
        if self.p.post_clock < 0:
            return last - int(self.p.hold_hours * HOUR)
        slot = (last - int(self.p.post_min_lead * HOUR)) // DAY * DAY + int(self.p.post_clock * HOUR)
        while slot > last - int(self.p.post_min_lead * HOUR):
            slot -= DAY
        return slot

    def held(self) -> set[str]:
        return {oid for _, oid in self.timers}

    def next_timer(self) -> int:
        return min((t for t, _ in self.timers), default=10**9)

    def viewer_offer_round(self) -> None:
        """For each open gig whose next public raise is near, offer that fee to up to 3 recent viewers instead."""
        hour = self.now % DAY // 60
        if hour < 7 or hour >= 22:
            return
        roster = {d["id"]: d for d in getattr(self, "roster", []) or []}
        live = {g for legs in self.leg_gigs.values() for g in legs.values()}
        for g in live:
            if g in self.booked or g not in self.posts:
                continue
            post = self.posts[g]
            nxt = next((st for st in post["schedule"] if st["at"] > self.now), None)
            if nxt is None or nxt["at"] - self.now > self.p.viewer_lead or (g, nxt["at"]) in self.viewer_done:
                continue
            self.viewer_done.add((g, nxt["at"]))
            need_exp, need_man = post["audience"].get("experienced"), post["audience"].get("manualLicence")
            asked = self.offered.setdefault(g, set())
            seen = []
            for at, d in reversed(self.viewers.get(g, [])):
                if at < self.now - self.p.viewer_recent or d in asked or d in seen:
                    continue
                prof = roster.get(d)
                if prof is None or (need_man and prof["licence"] != "manual") or (need_exp and (prof["rating"] < 4.8 or prof["completedGigs"] < 50)):
                    continue
                if self.offers_today.get((d, self.now // DAY), 0) >= 3:
                    continue
                seen.append(d)
            for d in seen[:max(0, 3 - self.open_offers.get(g, 0))]:
                self.outbox.append({"type": "offer", "gigId": g, "driverId": d, "feeCents": nxt["feeCents"]})
                asked.add(d)
                self.offers_today[(d, self.now // DAY)] = self.offers_today.get((d, self.now // DAY), 0) + 1
                self.open_offers[g] = self.open_offers.get(g, 0) + 1
                self.stats["viewer_offers"] = self.stats.get("viewer_offers", 0) + 1

    def next_check(self) -> int:
        """With viewer offers on, wake at least every 20 minutes in the day so raises are not missed."""
        return self.now + 20 if self.p.viewer_offers else 10**9

    def fire_timers(self) -> None:
        if self.p.viewer_offers:
            self.viewer_offer_round()
        due = [x for x in self.timers if x[0] <= self.now]
        self.timers = [x for x in self.timers if x[0] > self.now]
        for _, oid in due:
            order = self.orders.get(oid)
            if order is not None and order["status"] == "waiting" and not self.gigs_of(oid):
                self.post(order, undelivered(order))

    # ---------- fees ----------
    def fees_for(self, order: Order, cars: int) -> list[FeeStep]:
        ref = self.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
        if order["id"] in self.urgent:
            return steep(self.now, ref, self.level)
        if self.level == 1:  # one fee per gig at level 1
            return [{"at": self.now, "feeCents": max(1, int(ref * 1.05 * self.fee_scale))}]
        return self.schedule(ref, order["lastPickupAt"], order["legs"][0]["pickup"]["placeId"], order["deadline"])

    def opening(self, pickup: Optional[str]) -> float:
        """Opening fee multiple by driver supply around the pickup: 86% of booked gigs book at the opening fee,
        in 3 minutes where drivers are plentiful, so there it can open lower."""
        if not self.p.fee_open_tiers or pickup is None:
            return self.p.fee_open
        s = self.supply.get(pickup, 0.0)
        for threshold, mult in self.p.fee_open_tiers:  # (supply index at least, opening multiple), highest first
            if s >= threshold:
                return mult
        return self.p.fee_open

    def schedule(self, ref: float, last: int, pickup: Optional[str] = None, deadline: Optional[int] = None) -> list[FeeStep]:
        steps = [(self.now, self.opening(pickup))]
        for hours_before, mult in self.p.fee_steps:
            at = last - hours_before * HOUR
            if at <= self.now:
                steps[0] = (self.now, max(steps[0][1], mult))
            else:
                steps.append((at, mult))
        out: list[FeeStep] = []
        for at, mult in steps:
            fee = max(1, int(ref * mult * self.fee_scale * self.p.fee_mult * (self.p.hedge_fee if self.aggressive(deadline) else 1.0)))
            if out and fee <= out[-1]["feeCents"]:
                continue
            out.append({"at": int(at), "feeCents": fee})
        return out


# ---------------- runners ----------------

FAST = os.environ.get("BAHN_FAST") == "1"  # backtests: fixed chunks instead of waking on every event


def chunk_end(now: int, ends: int, desk) -> int:
    """Fast mode: 12-minute chunks on weekday daytime (a request has 15 minutes, so every quote still lands in
    time), 30 minutes otherwise; never past a hold timer or midnight."""
    day, minute = now // DAY, now % DAY
    step = 12 if (day % 7 < 5 or day == 7) and 7 * 60 <= minute < 20 * 60 else 30
    return min(ends, (day + 1) * DAY, max(now + 1, min(now + step, desk.next_timer())))


def play_practice(world, level: int, policy: Policy, road: Road, desk: Optional[Dispatcher] = None, fast: Optional[bool] = None) -> Dispatcher:
    fast = FAST if fast is None else fast
    snap = world.snapshot()
    ends = snap["endsAt"]
    if desk is None:
        desk = Dispatcher(world, level, snap["now"], policy, road)
        desk.init_supply(world.roster())
    while desk.now < ends:
        actions, desk.outbox = desk.outbox, []
        if fast:
            step = world.advance(to=chunk_end(desk.now, ends, desk), actions=actions)  # the kit batches >50 actions
            desk.read_results(actions, step["results"])
            desk.now = step["now"]
            for event in step["events"]:
                desk.handle(event)
            desk.fire_timers()
            continue
        step = world.advance(until=WAKE_ON + ["order_lost", "offer_answered"] + (["delivered"] if desk.p.chain_offers else []), max=min(ends, (desk.now // DAY + 1) * DAY, max(desk.now + 1, min(desk.next_timer(), desk.next_check()))), actions=actions)
        desk.read_results(actions, step["results"])
        desk.now = step["now"]
        for event in step["events"]:
            desk.handle(event)
        desk.fire_timers()
    return desk


def run_seed(seed: str, level: int, policy: Policy, tag: str, keep: bool) -> dict:
    client = Client()
    road = Road(client.map())
    for attempt in range(90):  # the team shares 8 practice slots: wait for one rather than fail
        try:
            world = client.create(level=level, seed=seed)
            break
        except SpreadError as e:
            if getattr(e, "status", None) != 409 or attempt == 89:
                raise
            time.sleep(20)
    LIVE.add(world.id)
    with open(REGISTRY, "a") as fh:  # survives a hard kill: `python -m bahn.simulation.dispatch --cleanup` deletes what is left
        fh.write(world.id + "\n")
    t0 = time.time()
    try:
        desk = play_practice(world, level, policy, road)
    except BaseException:
        try:
            world.delete()
        except SpreadError:
            pass
        raise
    LIVE.discard(world.id)
    if keep:
        from bahn.data.reveal_io import save
        save(client, world, f"{tag}-{seed}", level, desk.qlog)
    s = world.score()
    return {"seed": seed, "world": world.id, "secs": round(time.time() - t0), "comp": round(math.exp(desk.log_comp), 3), **desk.stats, **s}


def sweep(seeds: list[str], level: int, policy: Policy, tag: str, parallel: int, keep: bool) -> list[dict]:
    out = []
    with ProcessPoolExecutor(max_workers=parallel) as pool:
        futs = {pool.submit(run_seed, s, level, policy, tag, keep): s for s in seeds}
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                print(f"{futs[f]}: FAILED {e!r}", file=sys.stderr, flush=True)
                continue
            out.append(r)
            print(f"{tag:>6} {r['seed']:>8}  margin €{r['marginCents']/100:>10,.0f}  {r['wonOrders']:>4} orders  "
                  f"{r['onTimeShare']:6.1%} on time  comp≈{r['comp']}  q/d={r['quoted']}/{r['declined']} km/bar/mrg={r.get('d_km',0)}/{r.get('d_bar',0)}/{r.get('d_margin',0)}  {r['secs']}s", flush=True)
    if out:
        m = sum(r["marginCents"] for r in out) / len(out) / 100
        ot = min(r["onTimeShare"] for r in out)
        ranked = sum(r["ranked"] for r in out)
        print(f"{tag:>6} MEAN margin €{m:,.0f}/week  worst on-time {ot:.1%}  ranked {ranked}/{len(out)}", flush=True)
        os.makedirs(os.path.join(ROOT, "runs"), exist_ok=True)
        with open(os.path.join(ROOT, "runs", f"{tag}.json"), "w") as fh:
            json.dump({"policy": asdict(policy), "results": out}, fh, indent=1)
    return out


def rebuild(world, policy: Policy, road: Road, level: int) -> tuple[Dispatcher, int, int]:
    """Builds a desk from the server's own record, so a restart (ours or the server's) loses nothing:
    orders, gigs, open requests (quoted at once), dealer trust and the competitor estimate."""
    history = world.events()
    snap = world.snapshot()
    desk = Dispatcher(world, level, snap["now"], policy, road)
    desk.init_supply(world.roster())
    for o in snap["orders"]:
        if o["status"] != "cancelled":
            key = (o["legs"][0]["pickup"]["placeId"], o["legs"][0]["pickup"]["window"]["from"] // DAY)
            desk.load[key] = desk.load.get(key, 0.0) + len(o["legs"])
    desk.rejoin(snap, history)
    for order_id in list(desk.orders):
        desk.settle(order_id)
    desk.snow = [(e["from"], e["to"], set(e["placeIds"])) for e in history if e["kind"] == "forecast"]
    quotes = {}
    for e in (history if desk.regime is None else []):  # replay the SGD learner; the regime learner restarts from its prior
        if e["kind"] == "price_request":
            quotes[e["request"]["id"]] = e["request"]
        elif e["kind"] in ("order_won", "order_lost"):
            rid = e["order"]["id"] if e["kind"] == "order_won" else e["requestId"]
            if rid in quotes and rid not in desk.quoted:
                desk.quoted[rid] = (math.log(policy.comp0), 0)  # unknown old quote: a weak, neutral step
            desk.learn(rid, e["kind"] == "order_won")
    # The caller sends the outbox: play_live by priority within the action bucket, play_practice with its first advance.
    return desk, (history[-1]["seq"] if history else -1), snap["endsAt"]


class Pacer:
    """The client's copy of the final's action bucket: 20 tokens a second, at most 50, one per action."""

    def __init__(self, rate: float = 20.0, cap: float = 50.0):
        self.rate, self.cap, self.tokens, self.at = rate, cap, cap, time.monotonic()

    def available(self) -> int:
        now = time.monotonic()
        self.tokens, self.at = min(self.cap, self.tokens + (now - self.at) * self.rate), now
        return max(0, int(self.tokens))

    def spend(self, n: int) -> None:
        self.available()
        self.tokens -= n


def guarded(fn, *args) -> None:
    """A bug in one handler must not stop the final: report it and go on. Request failures go up (rebuild)."""
    try:
        fn(*args)
    except SpreadError:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def play_live(world, policy: Policy, road: Road, level: int = 8, clock=None) -> None:
    """The final: events from the stream, timers and the clock on a short idle tick. `clock()` gives the world's
    minute: the final's clock by default; livesim.py passes a practice world's. The outbox goes out quotes first,
    within the action bucket; what does not fit waits for the next tick (0.25 s), so a burst never blocks the stream."""
    clock = clock or (lambda: world.client.final()["now"])
    t0 = time.monotonic()
    desk, seq, ends = rebuild(world, policy, road, level)
    desk.pacer = Pacer()
    # The events do not hold our quoted prices, so rebuild can only guess the competitor estimate (or restart the regime
    # learner from its prior): after a crash of our own, take back what this desk had learned, from disk.
    saved_at, checkpoint = time.monotonic(), os.path.join(ROOT, "runs", f"live-{world.id}.pkl")
    rebuilt_comp = desk.log_comp
    try:
        with open(checkpoint, "rb") as fh:
            saved = pickle.load(fh)
        desk.log_comp, desk.regime = saved["log_comp"], saved["regime"] if desk.regime is not None else None
        desk.quoted = {**{k: v for k, v in saved["quoted"].items() if k in desk.requests}, **desk.quoted}
    except Exception:  # noqa: BLE001  (none yet, or unreadable: rebuild's guess stands)
        pass
    print(f"live: rebuilt at minute {desk.now} in {time.monotonic() - t0:.1f} s: {len(desk.orders)} orders, "
          f"{len(desk.outbox)} actions to send, comp≈{math.exp(desk.log_comp):.3f} (rebuild guessed {math.exp(rebuilt_comp):.3f})", flush=True)
    last_day = desk.now // DAY
    while desk.now < ends:
        heard = ticked = time.monotonic()
        stream = world.stream(after=seq, idle=0.25)
        try:
            for event in stream:
                now = time.monotonic()
                if now - saved_at >= 5:
                    saved_at = now
                    try:
                        os.makedirs(os.path.dirname(checkpoint), exist_ok=True)
                        with open(checkpoint + ".tmp", "wb") as fh:
                            pickle.dump({"log_comp": desk.log_comp, "quoted": desk.quoted, "regime": desk.regime}, fh)
                        os.replace(checkpoint + ".tmp", checkpoint)
                    except Exception:  # noqa: BLE001
                        pass
                if event is not None:
                    seq, heard = event["seq"], now
                    desk.now = max(desk.now, event["at"])
                    guarded(desk.handle, event)
                elif now - ticked >= 1.0:
                    ticked = now
                    desk.now = max(desk.now, clock())
                    if now - heard >= 2:  # a quiet stream can be a dead connection that does not know it
                        heard = now
                        if world.events(after=seq, limit=1):
                            print(f"live: the stream is silent with events waiting after seq {seq}; reconnecting", file=sys.stderr, flush=True)
                            break
                guarded(desk.fire_timers)
                desk.send(desk.pacer.available())
                if desk.now // DAY > last_day:
                    last_day = desk.now // DAY
                    try:
                        s = world.score()
                        print(f"day {last_day}: margin €{s['marginCents']/100:,.0f}  {s['wonOrders']} orders  {s['onTimeShare']:.1%} on time  "
                              f"comp≈{math.exp(desk.log_comp):.3f}  {desk.stats}", flush=True)
                    except SpreadError:
                        pass
                if desk.now >= ends:
                    return
            else:
                return  # the stream ended: the world was deleted
        finally:
            stream.close()


def run_final(policy: Policy, world=None, clock=None) -> None:
    """Plays the final (or `world`, on `clock`) to its end. Any failure, a server restart or a bug, rebuilds the desk
    from the server and goes on: the clock does not wait."""
    client = world.client if world is not None else Client()
    road, fails = None, 0
    while True:
        started = time.monotonic()
        try:
            road = road or Road(client.map())
            w = world or starter.next_final(client)
            print(f"final world {w.id}", flush=True)
            play_live(w, policy, road, clock=clock)
            s = w.score()
            print(f"END margin €{s['marginCents']/100:,.2f}  {s['wonOrders']} orders  {s['onTimeShare']:.1%} on time  ranked={s['ranked']}", flush=True)
            return
        except Exception as e:  # noqa: BLE001  (Unreachable while the server restarts, a refused request, a bug)
            fails = 1 if time.monotonic() - started > 30 else fails + 1
            wait = min(2.0, 0.25 * 2 ** (fails - 1))  # at once for a one-off failure, every 2 s while the server is down
            print(f"live loop failed: {e!r}; rebuilding in {wait:.2f} s", file=sys.stderr, flush=True)
            if not isinstance(e, Unreachable):
                traceback.print_exc()
            time.sleep(wait)


def run_board(level: int, policy: Policy, runs: int = 5) -> None:
    """Plays the round's ranking runs at a level, in order. An unfinished run of ours (a crash) is resumed
    from the server's record, never deleted: a deleted ranking run plays on without us and still counts."""
    client = Client()
    road = Road(client.map())
    for _ in range(runs):
        mine = [w for w in client.worlds() if w["boardRun"] and w["level"] == level and not w["ended"]]
        if mine:
            world = client.world(mine[0]["worldId"])
            desk, _, _ = rebuild(world, policy, road, level)
            print(f"L{level} resuming ranking run {world.id} (world {mine[0]['boardRun']}) at minute {desk.now}", flush=True)
        else:
            world = None
            for _ in range(180):
                try:
                    world = client.create(level=level, board=True)
                    break
                except SpreadError as e:
                    if "practice worlds that have not ended" in str(e):
                        time.sleep(20)  # slots busy: wait, a ranking run is worth it
                        continue
                    print(f"L{level}: no ranking run to start ({e})", flush=True)
                    return
            if world is None:
                print(f"L{level}: gave up waiting for a slot", flush=True)
                return
            desk = None
        play_practice(world, level, policy, road, desk)
        s = world.score()
        print(f"L{level} END {world.id}  margin €{s['marginCents']/100:,.2f}  {s['wonOrders']} orders  {s['onTimeShare']:.1%} on time  ranked={s['ranked']}", flush=True)


def run_board_levels(levels: list[int], policy: Policy) -> None:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(levels)) as pool:
        for f in [pool.submit(run_board, lv, policy) for lv in levels]:
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                print(f"ranking thread failed: {e!r}", file=sys.stderr, flush=True)


def cleanup_registry() -> None:
    """Deletes practice worlds from our registry that have not ended (never ranking runs: those are not registered)."""
    if not os.path.exists(REGISTRY):
        return
    client = Client()
    mine = set(open(REGISTRY).read().split())
    live = {w["worldId"]: w for w in client.worlds() if not w["ended"]}
    keep = set()
    for wid in mine:
        w = live.get(wid)
        if w is None:
            continue                      # ended or gone: drop from the registry
        if w["boardRun"]:
            keep.add(wid)                 # never delete a ranking run
            continue
        if wid in LIVE:
            keep.add(wid)                 # this process is still playing it
            continue
        client.world(wid).delete()
        print(f"deleted {wid}")
    with open(REGISTRY, "w") as fh:     # keep only what is still ours and alive
        fh.write("".join(f"{w}\n" for w in sorted(keep)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", action="store_true", help="play the next ranking run")
    ap.add_argument("--cleanup", action="store_true", help="delete unfinished practice worlds this tool created (registry)")
    ap.add_argument("--final", action="store_true", help="play the live final (or a test final)")
    ap.add_argument("--level", type=int, default=8)
    ap.add_argument("--levels", default="", help="with --board: comma-separated levels, played in parallel")
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--prefix", default="ev")
    ap.add_argument("--seedlist", default="", help="comma-separated seeds, instead of prefix-1..n")
    ap.add_argument("--tag", default="v7")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--keep", action="store_true", help="save the reveal for refitting")
    ap.add_argument("--set", nargs="*", default=[], help="policy overrides, e.g. fee_open=0.9 min_margin=5")
    a = ap.parse_args()
    pol = Policy()
    for kv in a.set:
        k, v = kv.split("=", 1)
        cur, val = getattr(pol, k), json.loads(v)
        val = tuple(tuple(x) if isinstance(x, list) else x for x in val) if isinstance(cur, tuple) else type(cur)(val)
        pol = replace(pol, **{k: val})
    if a.cleanup:
        return cleanup_registry()
    if a.final:
        return run_final(pol)
    if a.board:
        return run_board_levels([int(x) for x in a.levels.split(",")] if a.levels else [a.level], pol)
    seeds = a.seedlist.split(",") if a.seedlist else [f"{a.prefix}-{i}" for i in range(1, a.seeds + 1)]
    sweep(seeds, a.level, pol, a.tag, a.parallel, a.keep)


if __name__ == "__main__":
    main()
