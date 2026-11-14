"""Dispatcher v9 (shipped): v7's margin-max quotes plus the changes the level-8 archive and the
live deterministic A/B sweeps support. Built on the kit's starter for bookkeeping.

    python quick_test.py --tag T --seeds ev-1 --set k=v     # one-off A/B on a known seed
    python final.py                                          # the live final (stream-driven runner)

Shipped configuration and why (numbers in NOTES.md / NOTES-run.md):
  * post a won order at once (hold_hours 0) instead of 36 h before the last pickup: worth
    +1.7k-2.8k EUR/week and 1-2 points of on-time on identical worlds.
  * two single-car orders in one gig (bundling.py) only when the chain is tight: link
    delivery->pickup <= 35 min, total drive <= 280 min, deadlines within 18 h; opening fee
    ~0.79*(refA+refB). Loose caps are catastrophic on long-chain seeds.
  * read the dealer's note (notes_reader.py, 100% exact against the reveal truth) in safe mode:
    manual-licence / transfer-plate audiences and a later-ready pickup floor; pricing uses the
    moved deadline.
  * competitor threshold from the fitted online model (comp_from_qm 1): exp(log_comp_for(ctx,
    trust) + QuoteModel.A_value(ctx, n_options)) replaces v7's single global SGD level, so the
    persistent lane factors and dealer trust enter every price. Worth +~1.1k/week at equal
    punctuality - the largest remaining margin lever.
  * fitted P(late) (supply.py features refit by fit_late2.py, OOF AUC 0.789) drives the quote
    margin and the gate: an option with P(late) > 0.15 is declined.
  * expected-net selection bar (select_model.py, OOF R2 0.41, decile calibrated): decline a
    request whose menu expected net (sum p_take x expected net) is below EUR5. Together with the
    0.15 gate this moved the calibrated seeds from 14.8k/96.4% to 14.5k/97.2% (both targets).
  * off after A/B: fitted booking-model fee (hurt unseen seeds), direct offers (neutral), rescue
    repricing and failure fee inflation (lost more margin than they saved), price multiplier
    (chaotic), dynamic gate, full menu optimiser (use_quote_model 0).

Measured with these defaults on the six archived seeds: mean EUR14,524/week, 97.22% on time,
worst 95.6%, all ranked (v7-lam50: EUR11,275 / 92.4%). Unseen-seed panels: vb-1..6 EUR13,072 /
96.52%, vc-1..4 EUR12,519 / 97.04%.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor as ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace

from spread import starter
from spread.client import Client, SpreadError, Unreachable
from spread.starter import DAY, HOUR, WAKE_ON, Desk, pickups, steep, undelivered, without_windows
from spread.types import FeeStep, Order, PriceRequest

try:  # the pair-bundling geometry (see bundling.py); the policy still runs without it
    from bundling import chain, best_chain, bundle_base_fee, bundle_schedule
except Exception:  # pragma: no cover
    chain = best_chain = bundle_base_fee = bundle_schedule = None
try:  # the note reader (see notes_reader.py); pricing/posting work without it too
    from notes_reader import read_note
except Exception:  # pragma: no cover
    read_note = None
try:  # the fitted P(late) models (see late_model.py); v7's hand-set table is the fallback
    from late_model import LateModel, SupplyLateModel, features_for as late_features
except Exception:  # pragma: no cover
    LateModel = SupplyLateModel = late_features = None
try:  # the fitted competitor/P(win) model (see quote_model.py); the v7 SGD is the fallback
    from quote_model import OnlineCompetitor, QuoteModel
except Exception:  # pragma: no cover
    OnlineCompetitor = QuoteModel = None
try:  # the fitted supply/booking/lateness models (see supply.py); the hand tables are the fallback
    import supply as supply_mod
except Exception:  # pragma: no cover
    supply_mod = None
try:  # the fitted expected-net selection model (see select_model.py)
    from select_model import SelectModel
except Exception:  # pragma: no cover
    SelectModel = None

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE: set = set()  # practice worlds this process created and has not finished: deleted on exit


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
    fee_steps: tuple = ((24, 1.05), (16, 1.25), (10, 1.55), (6, 2.0))
    hold_hours: float = 0.0      # legacy: if > 0, hold a single order until lastPickupAt - hold_hours
    pair_enabled: float = 1.0    # 1 = pair two single-car orders into one gig when the chain works
    pair_wait: float = 20.0      # minutes to hold an unpaired order before posting it singly
    bundle_base_ratio: float = 0.88  # two-car ladder base as a share of (refA+refB): opening fee 0.9*that
    bundle_max_drive: float = 280.0  # minutes of total driving the chain may plan (rules allow 720)
    bundle_max_link: float = 35.0    # minutes from the first car's delivery to the second car's pickup
    bundle_min_lead_h: float = 3.0   # do not bundle an order with less than this many hours to its last pickup
    bundle_max_dead_gap: float = 18.0  # only chain orders whose deadlines are at most this many hours apart
    # Cost model: expected fee ratio and P(late) as functions of slack hours at the quote.
    exp_fee: tuple = ((4, 2.0), (8, 1.55), (16, 1.25), (30, 1.05), (1e9, 0.95))
    p_late: tuple = ((4, 0.6), (8, 0.3), (16, 0.12), (30, 0.06), (1e9, 0.03))
    late_cost: float = 0.45      # refund + lost trust, as a share of the price
    late_shadow: float = 80.0    # euros: the shadow price of the 90% minimum, per expected late order
    km_late: float = 0.0         # extra P(late) per km (now inside the supply model)
    # P(late) from driver supply, fitted on v5 runs: logit = b0 + bp*log1p(S_pickup) + bd*log1p(S_delivery) + bk*km/100,
    # S = sum over towns of resident drivers * exp(-road minutes / 60).
    sup_b: tuple = (4.23, -0.816, -0.469, -0.425)
    sup_base: float = 0.045      # the model's typical P(late), already in the slack table
    sup_fee: float = 0.6         # fee uplift per unit of supply-model P(late)
    max_km: float = 520.0        # decline longer lanes
    min_margin: float = 8.0      # euros of expected margin to quote an option at all
    # v8 experiment hooks (A/B sweeps): price multiplier, hard gates.
    price_mult: float = 1.0      # scale every quoted price by this (selectivity lever)
    min_slack_h: float = 0.0     # decline an option whose slack to the deadline is below this
    supply_gate: float = 2.0     # decline an option whose supply-model P(late) exceeds this (2 = off)
    # SLA controller.
    rescue_h: float = 0.0         # (off) reprice an unbooked gig when its last pickup is this close
    rescue_mult: float = 1.35     # rescue reprice starts at this multiple of the ladder base
    retry_raise: float = 0.0      # each failed booking (drop/no-show/expiry/change) raises later fees by this share
    drop_raise: float = 0.0       # reprice an unbooked gig at once when a driver drops it (0 = wait for the ladder)
    use_quote_model: float = 0.0  # 1 = use the fitted competitor/P(win) model when available
    ev_bar: float = 0.0           # legacy pricing: decline a request whose best expected value (EUR) is below this
    select_qm: float = 0.0        # legacy pricing: decline a request whose menu EV under the fitted model is below this EUR
    select_net: float = 5.0       # decline a request whose expected net (fitted per-order net model) is below this EUR
    lane_gate: float = 0.0        # decline requests whose persistent competitor lane factor is below -this (log)
    comp_from_qm: float = 1.0     # 1 = take the competitor threshold from quote_model's online filter (lane-aware)
                                  #     instead of v7's single global SGD comp
    steer: float = 0.0            # legacy pricing: raise same_day by this share when a slower option is offered
    gate_pl: float = 0.15         # decline an option whose fitted P(late) exceeds this (0 = off)
    gate_dynamic: float = 0.0     # 1 = tighten gate with SLA pressure, 2 = with the raw share of decided orders that were late
    use_model_pl: float = 0.0     # 1 = late_model.LateModel for P(late), 0 = v7's hand-set table
    use_notes: float = 1.0        # 1 = read dealer notes for windows/audience/fees, 0 = kit starter behaviour
    note_mode: float = 1.0        # 1 = safe use (audience + pickup floor only), 2 = also delivery ceilings and opening hours
    use_supply_model: float = 1.0  # 1 = supply.py P(late) in the quote margin and gate (see use_supply_fee)
    use_supply_fee: float = 0.0   # 1 = also use the fitted expected driver fee; 0 = keep v7's hand fee formula
    use_fee_model: float = 0.0    # 1 = set the opening fee from supply.booking.fee_for_target instead of 0.9 x ref
    fee_target: float = 0.85      # target P(booked within fee_horizon) for the fee model
    fee_horizon: float = 8.0
    fee_ratio_min: float = 0.75   # clamp the model's fee ratio to [min, max]
    fee_ratio_max: float = 1.60
    fee_floor_target: float = 0.0  # >0: never open below the fee that books with this P within fee_floor_horizon
    fee_floor_horizon: float = 12.0
    gate_book: float = 0.0        # decline an option whose fitted P(booked within 8h) is below this (0 = off)
    offer_h: float = 0.0          # direct offers: start offering an unbooked gig this many hours before its last pickup (0 = off)
    offer_max_open: float = 3.0   # open direct offers per gig (rules allow 3)
    offer_fee_ratio: float = 1.0  # offer fee = the gig's current app fee times this
    offer_radius_min: float = 90.0  # only drivers whose home is this many road minutes from the pickup
    sla_target: float = 0.94
    sla_gain: float = 2.0        # how hard projected lateness raises the margin bar
    sla_prior_n: int = 120       # orders assumed on time, so early noise cannot trip the controller
    decline_mult: float = 4.0    # a declined option is quoted at this x list


def step_lookup(table: tuple, x: float) -> float:
    for bound, value in table:
        if x <= bound:
            return value
    return table[-1][1]


class Road:
    def __init__(self, wmap: dict):
        self.r = {(x["from"], x["to"]): x for x in wmap["roads"]}

    def minutes(self, a: str, b: str) -> int:
        return self.r[(a, b)]["minutes"]

    def km(self, a: str, b: str) -> float:
        return self.r[(a, b)]["km"]


class Dispatcher(Desk):
    def __init__(self, world, level, now, policy: Policy, road: Road):
        super().__init__(world, level, now)
        self.supply: dict[str, float] = {}
        p = policy
        self.p, self.road = policy, road
        self.log_comp = math.log(policy.comp0)
        self.day_of_comp = now // DAY
        self.quoted: dict[str, tuple[float, int]] = {}  # request id -> (log ratio of cheapest quote, day)
        self.won = self.late = self.on_time = 0
        self.stats = {"quoted": 0, "declined": 0, "won": 0, "lost": 0}
        self.timers: list[tuple[int, str]] = []  # (minute, order id): held orders to post later
        self.unposted: dict[str, int] = {}  # order id -> minute at which it is posted singly if no partner came
        self.pending_bundles: list[tuple[dict, tuple[str, str]]] = []  # post action not yet accepted -> its two orders
        self.bundle_of: dict[str, tuple[str, str]] = {}  # gig id -> the two order ids the gig carries
        self.note_cache: dict[str, object] = {}  # request id -> notes_reader.NoteConstraints
        self.homes: dict[str, list[dict]] = {}       # place -> the drivers who live there
        self.active: dict[str, int] = {}             # driver id -> last minute we saw them act (view/booking)
        self.offers_open: dict[str, dict[str, int]] = {}  # gig id -> driver id -> minute the offer was sent
        self.driver_offers: dict[tuple[str, int], int] = {}  # (driver, day) -> offers sent
        self.offered: dict[str, set] = {}            # gig id -> drivers already offered this gig
        self.gig_fee: dict[str, int] = {}            # gig id -> the fee currently shown in the app
        self._min_cache: dict[tuple[str, str], int] = {}
        self.attempts: dict[str, int] = {}  # order id -> failed booking attempts, raises later fees
        self.rescued: set[str] = set()      # order ids whose unbooked gig was already rescue-repriced
        self.drivers = 0
        self.world_mean_supply = 1.0
        try:
            self.late = LateModel.load() if (LateModel is not None and p.use_model_pl) else None
        except Exception:  # noqa: BLE001 - a missing/unreadable fit falls back to the step table
            self.late = None
        self.qm = None
        self.oc = None
        try:
            if QuoteModel is not None and (p.use_quote_model or p.select_qm > 0 or p.lane_gate > 0
                                          or p.comp_from_qm > 0 or p.select_net > 0):
                self.qm = QuoteModel.load()
                self.oc = OnlineCompetitor(self.qm)
        except Exception:  # noqa: BLE001 - a missing/unreadable fit falls back to the v7 SGD
            self.qm = self.oc = None
        self.quoted_model: dict[str, tuple[dict, dict, int]] = {}  # request id -> (ctx, prices, trust)
        self.sel = None
        if p.select_net > 0 and SelectModel is not None:
            try:
                self.sel = SelectModel.load()
            except Exception:  # noqa: BLE001 - a missing fit just disables selection
                self.sel = None
        self.smodels = None
        self.supply_struct = None
        self.forecasts: list[tuple[int, int, set]] = []
        try:
            if supply_mod is not None and p.use_supply_model:
                self.smodels = supply_mod.load_models()
                if SupplyLateModel is not None:  # the shipped lateness block is degenerate; use the refit
                    self.smodels.lateness = SupplyLateModel.load()
        except Exception:  # noqa: BLE001 - a missing/unreadable fit keeps the hand tables
            self.smodels = None
        self.trust: dict[str, int] = {}  # dealer -> orders delivered on time, as the rules count it (max 25, halved on a late one)
        self.settled: set[str] = set()

    # ---------- the SLA controller ----------
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

    def late_stats(self) -> tuple[int, int]:
        """(late, decided) over the orders we have already settled: the ranking-risk signal."""
        late = decided = 0
        for order in self.orders.values():
            if order["status"] == "cancelled":
                continue
            delivered = order["deliveredAt"]
            done = all(t is not None for t in delivered)
            if not done and self.now <= order["deadline"]:
                continue
            decided += 1
            if any(t is None or t > order["deadline"] for t in delivered):
                late += 1
        return late, decided

    def has_driver(self, order_id: str) -> bool:
        gigs = self.gigs_of(order_id)
        return bool(gigs) and all(g in self.booked for g in gigs)

    # ---------- driver supply ----------
    def init_supply(self, roster: list[dict]) -> None:
        homes: dict[str, int] = {}
        for d in roster:
            homes[d["homePlaceId"]] = homes.get(d["homePlaceId"], 0) + 1
        places = {a for a, _ in self.road.r}
        self.supply = {p: sum(n * math.exp(-self.road.minutes(h, p) / 60) for h, n in homes.items()) for p in places}
        self.drivers = len(roster)
        self.world_mean_supply = sum(self.supply.values()) / max(1, len(self.supply))
        homes: dict[str, list[dict]] = {}
        for driver in roster:
            homes.setdefault(driver["homePlaceId"], []).append(driver)
        self.homes = homes
        if self.smodels is not None and supply_mod is not None and self.supply_struct is None:
            try:
                struct = supply_mod.build_supply(roster, self.road)
                self.supply_struct = struct
                self.supply = {place: (value.get("kernel", 0.0) if isinstance(value, dict) else value)
                               for place, value in struct.items()}
            except Exception:  # noqa: BLE001
                self.supply_struct = None

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

    # ---------- the dealer's note ----------
    def note_of(self, request: PriceRequest):
        """The request's note constraints (notes_reader.NoteConstraints), cached; None when unreadable."""
        rid = request.get("id")
        if rid in self.note_cache:
            return self.note_cache[rid]
        constraints = None
        if read_note is not None:
            try:
                constraints = read_note(request)
            except Exception:  # noqa: BLE001 - the note reader must never break the week
                constraints = None
        self.note_cache[rid] = constraints
        return constraints

    def effective_deadline(self, request: PriceRequest, deadline: int) -> int:
        """The option's deadline after what the note says: a car ready later moves the deadline,
        a tighter deadline in the note caps it."""
        if not self.p.use_notes:
            return deadline
        nc = self.note_of(request)
        if nc is None:
            return deadline
        out = deadline
        delay = getattr(nc, "ready_delay_days", 0) or 0
        if delay:
            out += int(delay) * DAY
        cap = getattr(nc, "delivery_by", None)
        if cap is not None:
            out = min(out, int(cap))
        return out

    def note_fee_uplift(self, request: PriceRequest) -> float:
        """A note that needs a manual licence, transfer plates or snow-free driving shrinks the driver pool."""
        if not self.p.use_notes:
            return 1.0
        nc = self.note_of(request)
        if nc is None:
            return 1.0
        uplift = 1.0
        if getattr(nc, "manual_licence", False):
            uplift += 0.06
        if getattr(nc, "needs_plates", False):
            uplift += 0.04
        if getattr(nc, "summer_tyres", False):
            uplift += 0.04
        return uplift

    # ---------- quotes ----------
    def slack_hours(self, request: PriceRequest, deadline: int) -> float:
        """Hours from posting (at respondBy) to the last pickup that still makes the deadline."""
        drive = sum(self.road.minutes(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in request["legs"])
        last_pickup = deadline - drive - 30
        return (last_pickup - request["respondBy"]) / 60

    def quote(self, request: PriceRequest) -> None:
        self.requests[request["id"]] = request
        if self.p.use_quote_model and self.qm is not None and self.oc is not None:
            try:
                return self._quote_model(request)
            except Exception:  # noqa: BLE001 - never leave a request unanswered because a model call failed
                pass
        return self._quote_legacy(request)

    def _outstanding(self) -> int:
        return sum(1 for order in self.orders.values()
                   if order["status"] != "cancelled" and any(t is None for t in order["deliveredAt"]))

    def _weather(self, request: PriceRequest, deadline: int) -> float:
        if not self.forecasts:
            return 0.0
        places = set()
        for leg in request["legs"]:
            places.add(leg["pickup"]["placeId"])
            places.add(leg["delivery"]["placeId"])
        for start, end, where in self.forecasts:
            if (not where or where & places) and start <= deadline and end >= request["respondBy"]:
                return 1.0
        return 0.0

    def _supply_terms(self, request: PriceRequest, option: dict, deadline: int):
        """Fitted P(late) and expected driver fee (euros) for one option, or None without the fit."""
        if self.smodels is None or self.supply_struct is None or supply_mod is None:
            return None
        try:
            feats = supply_mod.order_features(request, option, self.road, self.supply_struct,
                                              outstanding=self._outstanding(), weather=self._weather(request, deadline))
            pl = float(self.smodels.lateness.p_late(feats))
            fee = float(self.smodels.booking.expected_fee_cents(feats)) / 100.0
            if not (0.0 <= pl <= 1.0) or fee <= 0.0:
                return None
            return pl, fee
        except Exception:  # noqa: BLE001
            return None

    def _quote_legacy(self, request: PriceRequest) -> None:
        p = self.p
        ref = request["referenceFeeCents"] / 100
        km = sum(self.road.km(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in request["legs"])
        if p.lane_gate > 0 and self.qm is not None:
            try:
                lane = self.qm.context(request, self.road)["lane_log_factor"]
                if lane < -p.lane_gate:
                    self.stats["declined"] += 1
                    self.stats["d_lane"] = self.stats.get("d_lane", 0) + 1
                    self.outbox.append({"type": "quote", "requestId": request["id"],
                                        "prices": {o["kind"]: int(o["listPriceCents"] * p.decline_mult) for o in request["options"]}})
                    return
            except Exception:  # noqa: BLE001
                pass
        if km > p.max_km:
            self.stats["declined"] += 1
            self.stats["d_km"] = self.stats.get("d_km", 0) + 1
            self.outbox.append({"type": "quote", "requestId": request["id"],
                                "prices": {o["kind"]: int(o["listPriceCents"] * p.decline_mult) for o in request["options"]}})
            return
        pressure = max(0.0, self.sla_pressure())
        bar = p.min_margin * (1 + p.sla_gain * pressure * 10)
        best_ev = -1e9
        cost_of: dict[str, tuple[float, float]] = {}  # option kind -> (expected fee EUR, p_late)
        context = None
        boost = 1 + 0.01 * self.trust.get(request["dealerId"], 0) if self.level >= 8 else 1.0
        comp = math.exp(self.log_comp) * boost
        context = None
        if p.comp_from_qm and self.oc is not None and self.qm is not None:
            try:
                context = self.qm.context(request, self.road)
                trust = self.trust.get(request["dealerId"], 0)
                comp = math.exp(self.oc.log_comp_for(context, trust) + self.qm.A_value(context, len(request["options"])))
            except Exception:  # noqa: BLE001
                comp = math.exp(self.log_comp) * boost
        prices: dict[str, int] = {}
        best_lr = None
        gated_any = False
        for o in request["options"]:
            lst = o["listPriceCents"] / 100
            deadline_eff = self.effective_deadline(request, o["deadline"])
            slack = self.slack_hours(request, deadline_eff)
            sl = self.supply_late(request)
            fee = (ref * step_lookup(p.exp_fee, slack) * (1.15 if request["note"] else 1.0)
                   * (1 + p.sup_fee * max(0.0, sl - p.sup_base)) * self.note_fee_uplift(request))
            pl_hand = min(0.95, step_lookup(p.p_late, slack) + p.km_late * km + max(0.0, sl - p.sup_base) + pressure)
            pl = pl_hand
            terms = (self._supply_terms(request, o, deadline_eff)
                     if (self.p.use_supply_model and self.smodels is not None) else None)
            if terms is not None:
                pl = terms[0]  # fitted P(late)
                if self.p.use_supply_fee:
                    fee = terms[1]  # fitted expected driver fee
            elif self.late is not None and self.supply:
                try:
                    adjusted = {"kind": o["kind"], "deadline": deadline_eff, "listPriceCents": o["listPriceCents"]}
                    row = late_features(request, adjusted, None, self.supply, self.road, self.drivers, self.world_mean_supply)
                    pl = float(self.late.p_late(row))
                except Exception:  # noqa: BLE001
                    pl = pl_hand
            gate_now = p.gate_pl * (1.0 + p.gate_dynamic * p.sla_gain * pressure * 10)
            if p.gate_dynamic >= 2:
                late_n, decided_n = self.late_stats()
                if decided_n >= 20:
                    share = late_n / decided_n
                    gate_now = p.gate_pl * (1.0 + max(0.0, share - (1.0 - p.sla_target)) * 40.0)
            gated_late = bool(gate_now > 0 and pl > gate_now)
            if gated_late:
                gated_any = True
            cost_of[o["kind"]] = (fee, pl)
            best, best_x = -1e9, None
            for i in range(60):
                x = 0.85 + i * 0.02  # price as x list
                price = lst * x
                win = 1 / (1 + math.exp(p.slope * (math.log(x) - math.log(comp))))
                margin = price * (1 - pl * p.late_cost) - fee - pl * p.late_shadow * (1 + p.sla_gain * pressure * 10)
                if margin * win > best:
                    best, best_x, best_margin = margin * win, x, margin
            gated = slack < p.min_slack_h or sl > p.supply_gate or gated_late
            if best_x is not None and not gated:
                pwin_best = 1 / (1 + math.exp(p.slope * (math.log(best_x) - math.log(comp))))
                ev = pwin_best * best_margin
                if ev > best_ev:
                    best_ev = ev
            if best_x is not None and best_margin >= bar and not gated:
                prices[o["kind"]] = int(lst * best_x * p.price_mult * 100)
                lr = math.log(best_x * p.price_mult)
                best_lr = lr if best_lr is None else min(best_lr, lr)
            else:
                prices[o["kind"]] = int(o["listPriceCents"] * p.decline_mult)
        if p.select_qm > 0 and self.oc is not None and self.qm is not None:
            try:
                if context is None:
                    context = self.qm.context(request, self.road)
                genuine = {o["kind"]: prices[o["kind"]] for o in request["options"]
                           if prices.get(o["kind"], 0) < 3 * o["listPriceCents"]}
                if genuine:
                    takes = self.oc.p_take(context, genuine, self.trust.get(request["dealerId"], 0))
                    expected = 0.0
                    for kind, probability in takes.items():
                        fee, pl = cost_of.get(kind, (0.0, 0.05))
                        price = genuine[kind] / 100.0
                        expected += probability * (price - fee - pl * (p.late_cost * price + p.late_shadow))
                    if expected < p.select_qm:
                        self.stats["declined"] += 1
                        self.stats["d_sel"] = self.stats.get("d_sel", 0) + 1
                        self.outbox.append({"type": "quote", "requestId": request["id"],
                                            "prices": {o["kind"]: int(o["listPriceCents"] * p.decline_mult) for o in request["options"]}})
                        return
                    self.quoted_model[request["id"]] = (context, genuine, self.trust.get(request["dealerId"], 0))
            except Exception:  # noqa: BLE001
                pass
        if p.comp_from_qm and self.oc is not None and context is not None:
            genuine = {o["kind"]: prices[o["kind"]] for o in request["options"]
                       if prices.get(o["kind"], 0) < 3 * o["listPriceCents"]}
            if genuine:
                self.quoted_model[request["id"]] = (context, genuine, self.trust.get(request["dealerId"], 0))
        if p.select_net > 0 and self.oc is not None and self.sel is not None:
            try:
                if context is None:
                    context = self.qm.context(request, self.road)
                options_by_kind = {o["kind"]: o for o in request["options"]}
                genuine = {kind: prices[kind] for kind in options_by_kind
                           if prices.get(kind, 0) < 3 * options_by_kind[kind]["listPriceCents"]}
                if genuine:
                    takes = self.oc.p_take(context, genuine, self.trust.get(request["dealerId"], 0))
                    lane = context.get("lane_log_factor", 0.0)
                    expected = 0.0
                    for kind, probability in takes.items():
                        if probability <= 0:
                            continue
                        expected += probability * self.sel.net(request, options_by_kind[kind], self.road,
                                                               self.supply, self.world_mean_supply, lane)
                    if expected < p.select_net:
                        self.stats["declined"] += 1
                        self.stats["d_selnet"] = self.stats.get("d_selnet", 0) + 1
                        self.outbox.append({"type": "quote", "requestId": request["id"],
                                            "prices": {kind: int(o["listPriceCents"] * p.decline_mult)
                                                       for kind, o in options_by_kind.items()}})
                        return
                    self.quoted_model[request["id"]] = (context, genuine, self.trust.get(request["dealerId"], 0))
            except Exception:  # noqa: BLE001
                pass
        if p.ev_bar > 0 and best_ev < p.ev_bar:
            self.stats["declined"] += 1
            self.stats["d_ev"] = self.stats.get("d_ev", 0) + 1
            self.outbox.append({"type": "quote", "requestId": request["id"],
                                "prices": {o["kind"]: int(o["listPriceCents"] * p.decline_mult) for o in request["options"]}})
            return
        if p.steer > 0 and len(request["options"]) > 1 and "same_day" in prices:
            if prices["same_day"] < request["options"][0]["listPriceCents"] * p.decline_mult * 0.9:
                prices["same_day"] = int(prices["same_day"] * (1.0 + p.steer))
        if best_lr is None:
            self.stats["declined"] += 1
            if gated_any:
                self.stats["gated"] = self.stats.get("gated", 0) + 1
            self.stats["d_bar" if pressure > 0 else "d_margin"] = self.stats.get("d_bar" if pressure > 0 else "d_margin", 0) + 1
        else:
            self.stats["quoted"] += 1
            # Learn the competitor only where we offered the dealer's fastest option: without it,
            # dealers turn to the competitor at any price, and the estimate would sink.
            fastest = request["options"][0]
            if prices[fastest["kind"]] < fastest["listPriceCents"] * 3:
                self.quoted[request["id"]] = (best_lr - math.log(boost), self.now // DAY)
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})

    def _quote_model(self, request: PriceRequest) -> None:
        """Price a request with the fitted competitor model: choose the whole MENU that maximises
        expected margin. The dealer picks one option out of the genuine quotes (fitted conditional
        logit), so the menu is optimised jointly over options."""
        p = self.p
        qm, oc = self.qm, self.oc
        ctx = qm.context(request, self.road)
        ref = request["referenceFeeCents"] / 100.0
        km = sum(self.road.km(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in request["legs"])
        pressure = max(0.0, self.sla_pressure())
        trust = self.trust.get(request["dealerId"], 0)
        sl = self.supply_late(request)
        uplift = self.note_fee_uplift(request)
        penalty = p.late_shadow * (1 + p.sla_gain * pressure * 10)
        lists, fees, pls, slack_of = {}, {}, {}, {}
        for option in request["options"]:
            kind = option["kind"]
            deadline = self.effective_deadline(request, option["deadline"])
            slack = self.slack_hours(request, deadline)
            lists[kind] = option["listPriceCents"]
            fees[kind] = (ref * step_lookup(p.exp_fee, slack) * (1.15 if request["note"] else 1.0)
                          * (1 + p.sup_fee * max(0.0, sl - p.sup_base)) * uplift)
            hand = min(0.95, step_lookup(p.p_late, slack) + p.km_late * km + max(0.0, sl - p.sup_base) + pressure)
            pl = hand
            if self.late is not None and self.supply:
                try:
                    row = late_features(request, {"kind": kind, "deadline": deadline, "listPriceCents": lists[kind]},
                                        None, self.supply, self.road, self.drivers, self.world_mean_supply)
                    pl = float(self.late.p_late(row))
                except Exception:  # noqa: BLE001
                    pl = hand
            pls[kind], slack_of[kind] = pl, slack
        order = [option["kind"] for option in request["options"]]
        live = [k for k in order if slack_of[k] >= p.min_slack_h and sl <= p.supply_gate
                and not (p.gate_pl > 0 and pls[k] > p.gate_pl)]
        if not live:
            self.stats["declined"] += 1
            self.stats["gated"] = self.stats.get("gated", 0) + 1
            self.outbox.append({"type": "quote", "requestId": request["id"],
                                "prices": {k: int(lists[k] * p.decline_mult) for k in order}})
            return
        nq = len(live)

        def margin(kind: str, price_cents: float) -> float:
            price = price_cents / 100.0
            return price - fees[kind] - pls[kind] * (p.late_cost * price + penalty)

        def expected(menu: dict) -> float:
            try:
                takes = oc.p_take(ctx, menu, trust)
            except Exception:  # noqa: BLE001
                return -1e18
            return sum(prob * margin(kind, menu[kind]) for kind, prob in takes.items())

        eff_lc = oc.log_comp_for(ctx, trust) + qm.A_value(ctx, nq)
        grid = tuple(round(0.70 + 0.05 * i, 2) for i in range(27))
        menu: dict[str, int] = {}
        for kind in live:
            list_euros = lists[kind] / 100.0
            cost_ratio = (fees[kind] + pls[kind] * penalty) / max(1e-6, list_euros)
            x = qm.optimal_ratio(eff_lc, cost_ratio, pls[kind] * p.late_cost, grid)
            menu[kind] = max(1, int(lists[kind] * x * p.price_mult))
        best_ev = expected(menu)
        for _ in range(2):
            improved = False
            for kind in live:
                keep, keep_ev = menu[kind], best_ev
                for x in grid:
                    trial = dict(menu)
                    trial[kind] = max(1, int(lists[kind] * x * p.price_mult))
                    if trial[kind] == keep:
                        continue
                    value = expected(trial)
                    if value > best_ev + 1e-9:
                        best_ev, keep = value, trial[kind]
                if keep != menu[kind]:
                    menu[kind] = keep
                    improved = True
            if not improved:
                break
        bar = p.min_margin * (1 + p.sla_gain * pressure * 10)
        prices = {}
        if best_ev < bar or best_ev <= 0:
            self.stats["declined"] += 1
            key = "d_bar" if pressure > 0 else "d_margin"
            self.stats[key] = self.stats.get(key, 0) + 1
            for kind in order:
                prices[kind] = int(lists[kind] * p.decline_mult)
        else:
            for kind in order:
                prices[kind] = menu.get(kind, int(lists[kind] * p.decline_mult))
            self.stats["quoted"] += 1
            self.quoted_model[request["id"]] = (ctx, dict(menu), trust)
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})

    def learn(self, request_id: str, won: bool) -> None:
        """One SGD step of the logistic likelihood on log(comp)."""
        if self.oc is not None:
            saved = self.quoted_model.pop(request_id, None)
            if saved is not None:
                ctx, prices, trust = saved
                try:
                    self.oc.observe(ctx, prices, won, self.now, trust)
                except Exception:  # noqa: BLE001
                    pass
                self.stats["won" if won else "lost"] = self.stats.get("won" if won else "lost", 0) + 1
                if self.p.use_quote_model:
                    try:
                        self.log_comp = self.oc.log_level + self.oc.day_mu
                    except Exception:  # noqa: BLE001
                        pass
                    return
        if request_id not in self.quoted:
            return
        lr, _ = self.quoted.pop(request_id)
        pwin = 1 / (1 + math.exp(self.p.slope * (lr - self.log_comp)))
        self.log_comp += self.p.comp_lr * ((1.0 if won else 0.0) - pwin)
        self.log_comp = min(math.log(self.p.comp_hi), max(math.log(self.p.comp_lo), self.log_comp))
        self.stats["won" if won else "lost"] += 1

    def handle(self, event) -> None:
        if event["kind"] == "order_won":
            self.learn(event["order"]["id"], True)
        elif event["kind"] == "order_lost":
            self.learn(event["requestId"], False)
        if event["kind"] == "gig_viewed":
            self.active[event.get("driverId")] = event["at"]
        elif event["kind"] == "offer_answered":
            self.offers_open.get(event.get("gigId"), {}).pop(event.get("driverId"), None)
        elif event["kind"] == "gig_expired":
            self.stats["expired"] = self.stats.get("expired", 0) + 1
        elif event["kind"] == "deadline_close":
            self.stats["urgent"] = self.stats.get("urgent", 0) + 1
        super().handle(event)
        if event["kind"] in ("gig_dropped", "no_show") and self.p.drop_raise > 0:
            self._failed_booking(event.get("gigId"), self.p.drop_raise)
        if event["kind"] == "delivered":
            self.settle(event["orderId"])
        elif event["kind"] == "forecast":
            try:
                self.forecasts.append((int(event["from"]), int(event["to"]), set(event.get("placeIds") or [])))
            except Exception:  # noqa: BLE001
                pass
        elif event["kind"] == "order_cancelled":
            # A withdrawn gig stopped moving every car it carried: post the other orders again.
            for gig in event.get("gigs") or []:
                if gig.get("outcome") != "withdrawn":
                    continue
                action = self.posts.get(gig["gigId"])
                if action is None:
                    continue
                others = {s["orderId"] for s in action["stops"]} - {event["orderId"]}
                for stop in action["stops"]:
                    if stop["orderId"] in others:
                        self.leg_gigs.get(stop["orderId"], {}).pop(stop["leg"], None)
                for oid in others:
                    order = self.orders.get(oid)
                    if order is not None:
                        legs = undelivered(order)
                        if legs and not self.gigs_of(oid):
                            self.post(order, legs)

    def settle(self, order_id: str) -> None:
        """Dealer trust at level 8: +1 per order on time, up to 25; a late order halves the count."""
        o = self.orders.get(order_id)
        if o is None or order_id in self.settled or any(t is None for t in o["deliveredAt"]):
            return
        self.settled.add(order_id)
        d = o["dealerId"]
        on_time = all(t <= o["deadline"] for t in o["deliveredAt"])
        self.trust[d] = min(25, self.trust.get(d, 0) + 1) if on_time else self.trust.get(d, 0) // 2

    def _note_window(self, window: dict, nc, floor: int, is_delivery: bool) -> dict:
        """Narrow a stop window by what the note says; on any doubt the stated window stays."""
        start, end = int(window["from"]), int(window["to"])
        if nc is not None:
            if not is_delivery:
                ready = getattr(nc, "pickup_from", None)
                if ready is not None:
                    start = max(start, int(ready))
            elif self.p.note_mode >= 2:
                cap = getattr(nc, "delivery_by", None)
                if cap is not None:
                    end = min(end, int(cap))
            hours = (getattr(nc, "delivery_hours" if is_delivery else "pickup_hours", None)
                     if self.p.note_mode >= 2 else None)
            if hours:
                opens, closes = int(hours[0]), int(hours[1])
                day = start // DAY * DAY
                start = max(start, day + opens)
                end = min(end, day + closes)
                if start > end or end <= day + opens:
                    start, end = int(window["from"]), int(window["to"])
        start = max(start, int(floor))
        if start > end:
            start, end = int(window["from"]), int(window["to"])
        return {"from": int(start), "to": int(end)}

    def post(self, order, legs, retry=starter.Retry()) -> None:
        """Post the order's cars as one gig, with the dealer's note read: pickup floor, delivery
        ceiling, real opening hours, manual-licence and transfer-plate requirements."""
        if not legs:
            return
        if not self.p.use_notes:
            super().post(order, legs, retry)
            return
        request = self.requests.get(order["id"])
        nc = self.note_of(request) if request is not None else None
        floor = int(getattr(nc, "pickup_from", 0) or 0) if nc is not None else 0
        stops: list[dict] = []
        for leg in legs:
            car = order["legs"][leg]["car"]
            pickup, delivery = order["legs"][leg]["pickup"], order["legs"][leg]["delivery"]
            pickup_window = self._note_window(pickup["window"], nc, max(0, retry.pickup_from, floor), False)
            delivery_base = {"from": delivery["window"]["from"], "to": max(delivery["window"]["from"], order["deadline"])}
            delivery_window = self._note_window(delivery_base, nc, 0, True)
            low = bool(car.get("electric") and car.get("chargePercent") is not None and car.get("chargePercent") < 30)
            stops.append(starter.narrower(
                {"orderId": order["id"], "leg": leg, "kind": "pickup",
                 "withPlates": bool(retry.plates or (nc is not None and getattr(nc, "needs_plates", False))),
                 "withCharging": low}, pickup_window, pickup["window"]))
            stops.append(starter.narrower({"orderId": order["id"], "leg": leg, "kind": "delivery"},
                                          delivery_window, delivery_base))
        audience = {"experienced": any(leg["car"]["premium"] for leg in order["legs"]),
                    "manualLicence": bool(retry.manual_licence or (nc is not None and getattr(nc, "manual_licence", False)))}
        self.outbox.append({"type": "post", "stops": stops, "audience": audience, "schedule": self.fees_for(order, len(legs))})
        if len(legs) == 1:
            return
        # a trade-in over the 12-hour limit for a gig with two cars goes as one gig per car
        self.send()
        if not self.gigs_of(order["id"]):
            for leg in legs:
                self.post(order, [leg], retry)

    # ---------- holding orders until closer to pickup ----------
    def win(self, order: Order) -> None:
        if order["id"] in self.requests and order["id"] not in self.orders:
            self.orders[order["id"]] = order
            if self.p.hold_hours > 0:
                post_at = order["lastPickupAt"] - int(self.p.hold_hours * HOUR)
                if post_at > self.now + 30:
                    self.timers.append((post_at, order["id"]))
                    return
            if self._pairable(order):
                self.unposted[order["id"]] = self.now + int(self.p.pair_wait)
            else:
                self.post(order, undelivered(order))

    def held(self) -> set[str]:
        return {oid for _, oid in self.timers}

    def next_timer(self) -> int:
        return min([t for t, _ in self.timers] + list(self.unposted.values()), default=10**9)

    def fire_timers(self) -> None:
        due = [x for x in self.timers if x[0] <= self.now]
        self.timers = [x for x in self.timers if x[0] > self.now]
        for _, oid in due:
            order = self.orders.get(oid)
            if order is not None and order["status"] == "waiting" and not self.gigs_of(oid):
                self.post(order, undelivered(order))
        if self.unposted:
            self.pair_now()
        self.rescue_due()
        self.offers_due()

    # ---------- rescuing gigs that did not find a driver ----------
    def _rescue_schedule(self, gig_id: str, share: float = 0.0):
        action = self.posts.get(gig_id)
        if action is None:
            return None
        oids = list(dict.fromkeys(stop["orderId"] for stop in action["stops"]))
        if len(oids) == 2:
            ra, rb = self.requests.get(oids[0]), self.requests.get(oids[1])
            if ra is None or rb is None:
                return None
            base = bundle_base_fee(ra, rb, self.p.bundle_base_ratio)
        else:
            order, request = self.orders.get(oids[0]), self.requests.get(oids[0])
            if order is None or request is None:
                return None
            cars = sum(1 for stop in action["stops"] if stop["kind"] == "pickup" and stop["orderId"] == oids[0]) or 1
            base = request["referenceFeeCents"] * cars / len(order["legs"]) * self.note_fee_uplift(request)
        base *= max(1.0 + self.p.retry_raise * max(self.attempts.get(o, 0) for o in oids),
                    self.p.rescue_mult * (1.0 + share))
        mults = (1.0, 1.15, 1.32, 1.55) if share == 0 else (1.0, 1.2, 1.45)
        out = []
        for i, mult in enumerate(mults):
            fee = max(1, int(base * mult + 0.5))
            if out and fee <= out[-1]["feeCents"]:
                continue
            out.append({"at": int(self.now + i * 2 * HOUR), "feeCents": fee})
        return out

    def _reprice_rescue(self, gig_id: str, share: float = 0.0) -> None:
        schedule = self._rescue_schedule(gig_id, share)
        if schedule:
            self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": schedule})
            posted = self.posts.get(gig_id)
            if posted is not None:
                posted["schedule"] = schedule
            self.stats["rescues"] = self.stats.get("rescues", 0) + 1

    def _failed_booking(self, gig_id, share: float) -> None:
        """A driver gave the gig back or did not show: our later fees must rise now, not at the ladder's step."""
        if not gig_id:
            return
        action = self.posts.get(gig_id)
        if action is None:
            return
        for oid in dict.fromkeys(stop["orderId"] for stop in action["stops"]):
            self.attempts[oid] = self.attempts.get(oid, 0) + 1
        if gig_id not in self.booked:
            self._reprice_rescue(gig_id, share)

    def rescue_due(self) -> None:
        """Reprice gigs with no driver once their last pickup is close: the 3-hour warning is too late."""
        if self.p.rescue_h <= 0:
            return
        for oid, order in self.orders.items():
            if oid in self.rescued or oid in self.unposted or oid in self.urgent or order["status"] == "cancelled":
                continue
            if any(t is not None for t in order["deliveredAt"]):
                continue
            gigs = self.gigs_of(oid)
            if not gigs or any(gig in self.booked for gig in gigs):
                continue
            if (order["lastPickupAt"] - self.now) / HOUR <= self.p.rescue_h:
                self.rescued.add(oid)
                for gig_id in gigs:
                    self._reprice_rescue(gig_id, 0.25)

    # ---------- direct offers to nearby drivers ----------
    def _nearby_home_minutes(self, home: str, place: str) -> int:
        key = (home, place)
        got = self._min_cache.get(key)
        if got is None:
            got = self.road.minutes(home, place)
            self._min_cache[key] = got
        return got

    def _drivers_near(self, place: str, exclude: set) -> list[str]:
        """Drivers whose home is within the offer radius of the pickup, recently active ones first."""
        out = []
        for home, drivers in self.homes.items():
            minutes = self._nearby_home_minutes(home, place)
            if minutes > self.p.offer_radius_min:
                continue
            for driver in drivers:
                if driver["id"] in exclude:
                    continue
                active = self.active.get(driver["id"])
                score = (0 if (active is not None and self.now - active < 180) else 1,
                         minutes, -int(driver.get("completedGigs") or 0), int(driver.get("drops") or 0))
                out.append((score, driver["id"]))
        out.sort()
        return [driver for _, driver in out]

    def offers_due(self) -> None:
        """Nudge drivers who do not see the app raise: a direct offer to the nearest home towns."""
        if self.p.offer_h <= 0 or not self.homes:
            return
        day = self.now // DAY
        limit = int(self.p.offer_max_open)
        for order in list(self.orders.values()):
            if order["status"] == "cancelled" or any(t is not None for t in order["deliveredAt"]):
                continue
            if (order["lastPickupAt"] - self.now) / HOUR > self.p.offer_h:
                continue
            for gig_id in self.gigs_of(order["id"]):
                if gig_id in self.booked:
                    continue
                action = self.posts.get(gig_id)
                if action is None or not action.get("stops"):
                    continue
                open_offers = self.offers_open.setdefault(gig_id, {})
                if len(open_offers) >= limit or any(self.now - at < 60 for at in open_offers.values()):
                    continue
                schedule = action.get("schedule") or []
                fee_now = schedule[0]["feeCents"] if schedule else 0
                for step in schedule:
                    if step["at"] <= self.now:
                        fee_now = step["feeCents"]
                offer_fee = int(fee_now * self.p.offer_fee_ratio)
                if offer_fee <= 0:
                    continue
                stop = action["stops"][0]
                leg = order["legs"][stop["leg"]]
                sent = 0
                for driver_id in self._drivers_near(leg["pickup"]["placeId"], self.offered.get(gig_id, set())):
                    key = (driver_id, day)
                    if self.driver_offers.get(key, 0) >= 3 or driver_id in open_offers:
                        continue
                    self.driver_offers[key] = self.driver_offers.get(key, 0) + 1
                    open_offers[driver_id] = self.now
                    self.offered.setdefault(gig_id, set()).add(driver_id)
                    self.outbox.append({"type": "offer", "gigId": gig_id, "driverId": driver_id, "feeCents": offer_fee})
                    self.stats["offers"] = self.stats.get("offers", 0) + 1
                    sent += 1
                    if sent >= 2 or len(open_offers) >= limit:
                        break

    # ---------- pairing two single-car orders into one gig ----------
    def _pairable(self, order: Order) -> bool:
        if not self.p.pair_enabled or chain is None or len(order["legs"]) != 1:
            return False
        if order["lastPickupAt"] - self.now < int(self.p.pair_wait) + 30 + int(self.p.bundle_min_lead_h * HOUR):
            return False
        return not any(t is not None for t in order["deliveredAt"])

    def _order_for_chain(self, order: Order) -> Order:
        """The order as the note will really make it: pickup floor and tighter deadline applied."""
        request = self.requests.get(order["id"])
        nc = self.note_of(request) if request is not None else None
        if nc is None:
            return order
        floor = getattr(nc, "pickup_from", None)
        cap = getattr(nc, "delivery_by", None)
        if floor is None and cap is None:
            return order
        copy = dict(order)
        legs = []
        for leg in order["legs"]:
            leg_copy = dict(leg)
            pickup = dict(leg_copy["pickup"])
            window = dict(pickup["window"])
            if floor is not None:
                window["from"] = max(int(window["from"]), int(floor))
            pickup["window"] = window
            leg_copy["pickup"] = pickup
            legs.append(leg_copy)
        copy["legs"] = legs
        if cap is not None:
            copy["deadline"] = min(int(copy["deadline"]), int(cap))
        return copy

    def _best_pair(self, a_id: str, b_id: str):
        oa, ob = self.orders.get(a_id), self.orders.get(b_id)
        ra, rb = self.requests.get(a_id), self.requests.get(b_id)
        if not (oa and ob and ra and rb):
            return None
        if abs(self.orders[a_id]["deadline"] - self.orders[b_id]["deadline"]) > self.p.bundle_max_dead_gap * HOUR:
            return None
        best = None
        for first, rf, second, rs in ((self._order_for_chain(oa), ra, self._order_for_chain(ob), rb),
                                      (self._order_for_chain(ob), rb, self._order_for_chain(oa), ra)):
            got = chain(first, rf, second, rs, self.road, int(self.p.bundle_max_drive), int(self.p.bundle_max_link))
            if got is not None and (best is None or got["score"] > best["score"]):
                best = got
        return best

    def _open_single_orders(self) -> list[str]:
        """Orders whose only gig is an open, unbooked, single-order posting of ours: rebundle candidates."""
        out = []
        for oid, order in self.orders.items():
            if oid in self.unposted or order["status"] == "cancelled":
                continue
            if any(t is not None for t in order["deliveredAt"]):
                continue
            gigs = self.gigs_of(oid)
            if len(gigs) != 1 or gigs[0] in self.booked:
                continue
            action = self.posts.get(gigs[0])
            if action is None or len({stop["orderId"] for stop in action["stops"]}) != 1:
                continue
            out.append(oid)
        return out

    def _withdraw_open_single(self, order_id: str) -> bool:
        """Takes the order's open single posting out of the app so both cars can be posted as one gig."""
        gigs = self.gigs_of(order_id)
        if len(gigs) != 1 or gigs[0] in self.booked:
            return False
        action = self.posts.get(gigs[0])
        if action is None or len({stop["orderId"] for stop in action["stops"]}) != 1:
            return False
        for stop in action["stops"]:
            self.leg_gigs.get(stop["orderId"], {}).pop(stop["leg"], None)
        self.outbox.append({"type": "withdraw", "gigId": gigs[0]})
        self.stats["rebundles"] = self.stats.get("rebundles", 0) + 1
        return True

    def pair_now(self) -> None:
        """Chains orders that fit together - unposted ones and open single postings - then posts the rest."""
        for oid in list(self.unposted):
            order = self.orders.get(oid)
            if order is None or order["status"] != "waiting" or any(t is not None for t in order["deliveredAt"]):
                self.unposted.pop(oid, None)
        pool = list(self.unposted) + [oid for oid in self._open_single_orders() if oid not in self.unposted]
        possible = []
        for i, a_id in enumerate(pool):
            for b_id in pool[i + 1:]:
                got = self._best_pair(a_id, b_id)
                if got is not None:
                    possible.append(got)
        possible.sort(key=lambda c: -c["score"])
        used: set[str] = set()
        for got in possible:
            a, b = got["first"]["id"], got["second"]["id"]
            if a in used or b in used or a not in pool or b not in pool:
                continue
            withdrawn = True
            for oid in (a, b):
                if oid not in self.unposted and not self._withdraw_open_single(oid):
                    withdrawn = False
                    break
            if not withdrawn:
                continue
            self._post_bundle(got, self.requests[a], self.requests[b])
            used.update((a, b))
        for oid in used:
            self.unposted.pop(oid, None)
        for oid, due in list(self.unposted.items()):
            order = self.orders.get(oid)
            if order is None:
                self.unposted.pop(oid, None)
            elif self.now >= due or self.p.pair_wait <= 0:
                self.unposted.pop(oid, None)
                self.post(order, undelivered(order))
                self.stats["singles"] = self.stats.get("singles", 0) + 1

    def _bundle_stops(self, first: Order, second: Order) -> list[dict]:
        stops = []
        for order in (first, second):
            leg = order["legs"][0]
            request = self.requests.get(order["id"])
            nc = self.note_of(request) if request is not None else None
            low = bool(leg["car"]["electric"] and leg["car"]["chargePercent"] is not None and leg["car"]["chargePercent"] < 30)
            plates = bool(nc is not None and getattr(nc, "needs_plates", False))
            stops.append({"orderId": order["id"], "leg": 0, "kind": "pickup", "withPlates": plates, "withCharging": low})
            stops.append({"orderId": order["id"], "leg": 0, "kind": "delivery"})
        return stops

    def _bundle_schedule(self, first: Order, second: Order, ra: dict, rb: dict) -> list[FeeStep]:
        last = min(first["lastPickupAt"], second["lastPickupAt"])
        base = bundle_base_fee(ra, rb, self.p.bundle_base_ratio)
        base *= 1.0 + self.p.retry_raise * max(self.attempts.get(first["id"], 0), self.attempts.get(second["id"], 0))
        model_ratio = self._booking_ratio(first, 2, first, second,
                                          chain_min=self.road.minutes(first["legs"][-1]["delivery"]["placeId"],
                                                                     second["legs"][0]["pickup"]["placeId"]))
        if model_ratio is not None:
            base = (ra["referenceFeeCents"] + rb["referenceFeeCents"]) * model_ratio / max(1e-6, self.p.fee_open)
        if self.p.fee_floor_target > 0:
            floor = self._booking_ratio(first, 2, first, second,
                                        chain_min=self.road.minutes(first["legs"][-1]["delivery"]["placeId"],
                                                                   second["legs"][0]["pickup"]["placeId"]),
                                        target=self.p.fee_floor_target, horizon=self.p.fee_floor_horizon)
            if floor is not None and floor > self.p.fee_open:
                base = max(base, (ra["referenceFeeCents"] + rb["referenceFeeCents"])
                           * floor / max(1e-6, self.p.fee_open))
        return bundle_schedule(self.now, last, base, self.p.fee_open, tuple(self.p.fee_steps))

    def _post_bundle(self, got: dict, ra: dict, rb: dict) -> None:
        first, second = got["first"], got["second"]
        action = {"type": "post", "stops": self._bundle_stops(first, second),
                  "audience": {"experienced": any(o["legs"][0]["car"]["premium"] for o in (first, second)),
                               "manualLicence": any(self._manual_note(o) for o in (first, second))},
                  "schedule": self._bundle_schedule(first, second, ra, rb)}
        self.outbox.append(action)
        self.pending_bundles.append((action, (first["id"], second["id"])))
        self.stats["pairs"] = self.stats.get("pairs", 0) + 1

    def _manual_note(self, order: Order) -> bool:
        request = self.requests.get(order["id"])
        nc = self.note_of(request) if request is not None else None
        return bool(nc is not None and getattr(nc, "manual_licence", False))

    def read_results(self, actions, results) -> None:
        super().read_results(actions, results)
        for action, result in zip(actions, results):
            if action.get("type") == "post" and result.get("ok") is not True:
                self.stats["refused"] = self.stats.get("refused", 0) + 1
            if action.get("type") == "post" and result.get("ok") is True:
                for i, (posted, pair) in enumerate(self.pending_bundles):
                    if posted is action:
                        self.bundle_of[result["id"]] = pair
                        del self.pending_bundles[i]
                        break

    # ---------- fees ----------
    def _booking_ratio(self, order: Order, cars: int, first: Order = None, second: Order = None,
                       chain_min: float = 0.0, target: Optional[float] = None,
                       horizon: Optional[float] = None, follow_lead: bool = False) -> Optional[float]:
        """The fee ratio (opening fee / sum of reference fees) the booking model says books the gig."""
        if not self.p.use_fee_model or self.smodels is None or self.supply_struct is None or supply_mod is None:
            return None
        try:
            legs = order["legs"]
            p0 = legs[0]["pickup"]["placeId"]
            d0 = legs[-1]["delivery"]["placeId"]
            km = sum(self.road.km(leg["pickup"]["placeId"], leg["delivery"]["placeId"]) for leg in legs)
            route_min = sum(self.road.minutes(leg["pickup"]["placeId"], leg["delivery"]["placeId"]) for leg in legs)
            if second is not None:
                route_min += self.road.minutes(first["legs"][-1]["delivery"]["placeId"], second["legs"][0]["pickup"]["placeId"])
                km += self.road.km(first["legs"][-1]["delivery"]["placeId"], second["legs"][0]["pickup"]["placeId"])
            pickup_from = min(leg["pickup"]["window"]["from"] for leg in legs)
            ref_cents = int(sum(self.requests[o["id"]]["referenceFeeCents"] for o in ((first, second) if second is not None else (order,))))
            n_orders = 2 if second is not None else 1
            features = supply_mod.gig_features(
                self.supply_struct,
                supply_pickup=self.supply.get(p0, 0.0), supply_delivery=self.supply.get(d0, 0.0),
                km=km, route_min=route_min, lead_h=(pickup_from - self.now) / HOUR, now=self.now,
                n_cars=cars, n_orders=n_orders, chain_min=chain_min,
                ref_cents=ref_cents, fee_ratio=0.9)
            if follow_lead:
                horizon = max(0.5, (pickup_from - self.now) / HOUR)
            ratio = float(self.smodels.booking.fee_for_target(
                features, target=self.p.fee_target if target is None else target,
                horizon_h=self.p.fee_horizon if horizon is None else horizon))
            if not (0.2 <= ratio <= 3.0):
                return None
            return max(self.p.fee_ratio_min, min(self.p.fee_ratio_max, ratio))
        except Exception:  # noqa: BLE001
            return None

    def fees_for(self, order: Order, cars: int) -> list[FeeStep]:
        request = self.requests[order["id"]]
        ref = (request["referenceFeeCents"] * cars / len(order["legs"]) * self.note_fee_uplift(request)
               * (1.0 + self.p.retry_raise * self.attempts.get(order["id"], 0)))
        model_ratio = self._booking_ratio(order, cars)
        if model_ratio is not None:
            ref = ref * model_ratio / max(1e-6, self.p.fee_open)
        if self.p.fee_floor_target > 0:
            floor = self._booking_ratio(order, cars, target=self.p.fee_floor_target,
                                        horizon=self.p.fee_floor_horizon, follow_lead=True)
            if floor is not None and floor > self.p.fee_open:
                ref = max(ref, request["referenceFeeCents"] * cars / len(order["legs"])
                          * self.note_fee_uplift(request) * floor / max(1e-6, self.p.fee_open))
        if order["id"] in self.urgent:
            return steep(self.now, ref, self.level)
        last = order["lastPickupAt"]
        steps = [(self.now, self.p.fee_open)]
        for hours_before, mult in self.p.fee_steps:
            at = last - hours_before * HOUR
            if at <= self.now:
                steps[0] = (self.now, max(steps[0][1], mult))
            else:
                steps.append((at, mult))
        out: list[FeeStep] = []
        for at, mult in steps:
            fee = max(1, int(ref * mult))
            if out and fee <= out[-1]["feeCents"]:
                continue
            out.append({"at": int(at), "feeCents": fee})
        return out


    # ---------- keeping the books right when a gig changes ----------
    def _withdraw_gigs(self, order_id: str) -> set[str]:
        """Withdraws the order's unbooked gigs; returns the other orders those gigs carried."""
        self.send_post_of(order_id)
        affected: set[str] = set()
        for gig_id in self.gigs_of(order_id):
            if gig_id in self.booked:
                continue
            action = self.posts.get(gig_id)
            if action is not None:
                for stop in action["stops"]:
                    self.leg_gigs.get(stop["orderId"], {}).pop(stop["leg"], None)
                    affected.add(stop["orderId"])
            self.outbox.append({"type": "withdraw", "gigId": gig_id})
        self.leg_gigs.pop(order_id, None)
        return affected

    def change(self, order: Order) -> None:
        """A dealer changed the order: withdraw its unbooked gigs (a bundle takes the partner's car too) and post again."""
        if order["id"] in self.orders:
            self.orders[order["id"]] = order
            self.attempts[order["id"]] = self.attempts.get(order["id"], 0) + 1
            for oid in self._withdraw_gigs(order["id"]):
                other = self.orders.get(oid)
                if other is not None:
                    legs = undelivered(other)
                    if legs and not self.gigs_of(oid):
                        self.post(other, legs)

    def repost(self, gig_id: str) -> None:
        """A gig left the app before a driver booked it: post its cars again (as a pair when both are left)."""
        action = self.posts.get(gig_id)
        if action is None or not any(self.leg_gigs.get(s["orderId"], {}).get(s["leg"]) == gig_id for s in action["stops"]):
            return
        for stop in action["stops"]:
            self.leg_gigs.get(stop["orderId"], {}).pop(stop["leg"], None)
        oids = list(dict.fromkeys(stop["orderId"] for stop in action["stops"]))
        live = [self.orders[o] for o in oids if o in self.orders and self.orders[o]["status"] != "cancelled"
                and any(t is None for t in self.orders[o]["deliveredAt"])]
        if not live:
            return
        for order in live:
            self.attempts[order["id"]] = self.attempts.get(order["id"], 0) + 1
        if len(live) == 2 and chain is not None and all(len(o["legs"]) == 1 for o in live):
            ra, rb = self.requests.get(live[0]["id"]), self.requests.get(live[1]["id"])
            if ra and rb:
                posted = {"type": "post", "stops": without_windows(self._bundle_stops(live[0], live[1]), float("inf")),
                          "audience": action["audience"], "schedule": self._bundle_schedule(live[0], live[1], ra, rb)}
                self.outbox.append(posted)
                self.pending_bundles.append((posted, (live[0]["id"], live[1]["id"])))
                return
        order = live[0]
        self.outbox.append({**action,
                            "stops": without_windows([s for s in action["stops"] if s["orderId"] == order["id"]], float("inf")),
                            "schedule": self.fees_for(order, sum(1 for s in action["stops"] if s["kind"] == "pickup" and s["orderId"] == order["id"]))})

    def raise_fees(self, order_id: str) -> None:
        """The deadline warning came: the order's unbooked gigs go at steep fees (a pair's fee covers both cars)."""
        order = self.orders.get(order_id)
        if order is None:
            return
        self.urgent.add(order_id)
        self.send_post_of(order_id)
        for gig_id in self.gigs_of(order_id):
            if gig_id in self.booked:
                continue
            pair = self.bundle_of.get(gig_id)
            if pair is not None:
                ra, rb = self.requests.get(pair[0]), self.requests.get(pair[1])
                if ra and rb:
                    base = bundle_base_fee(ra, rb, self.p.bundle_base_ratio)
                    schedule = steep(self.now, base, self.level)
                    self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": schedule})
                    posted = self.posts.get(gig_id)
                    if posted is not None:
                        posted["schedule"] = schedule
                    continue
            schedule = self.fees_for(order, pickups(self.posts[gig_id]))
            self.outbox.append({"type": "reprice", "gigId": gig_id, "schedule": schedule})
            posted = self.posts.get(gig_id)
            if posted is not None:
                posted["schedule"] = schedule

    def rejoin(self, snapshot, history) -> None:
        super().rejoin(snapshot, history)
        # Dealer trust from the account: a revenue line with no late refund or penalty is an order on time.
        dealer_of: dict[str, str] = {}
        late: set[str] = set()
        for event in history:
            if event["kind"] == "order_won":
                dealer_of[event["order"]["id"]] = event["order"]["dealerId"]
            elif event["kind"] == "ledger":
                line = event["line"]
                if line["kind"] in ("late_refund", "undelivered_penalty") and line.get("orderId"):
                    late.add(line["orderId"])
        trust: dict[str, int] = {}
        for event in history:
            if event["kind"] == "ledger" and event["line"]["kind"] == "revenue":
                oid = event["line"].get("orderId")
                dealer = dealer_of.get(oid)
                if dealer is None:
                    continue
                trust[dealer] = trust.get(dealer, 0) // 2 if oid in late else min(25, trust.get(dealer, 0) + 1)
        self.trust = trust
        # Live gigs that carry two orders: remember the pair for later repricing/reposting.
        for gig in snapshot["gigs"]:
            oids = list(dict.fromkeys(stop["orderId"] for stop in gig["stops"]))
            if len(oids) == 2 and gig["status"] in ("open", "booked", "working"):
                self.bundle_of[gig["id"]] = (oids[0], oids[1])


# ---------------- runners ----------------

def play_practice(world, level: int, policy: Policy, road: Road) -> Dispatcher:
    snap = world.snapshot()
    desk, ends = Dispatcher(world, level, snap["now"], policy, road), snap["endsAt"]
    desk.init_supply(world.roster())
    while desk.now < ends:
        actions, desk.outbox = desk.outbox, []
        step = world.advance(until=WAKE_ON + ["order_lost"], max=min(ends, (desk.now // DAY + 1) * DAY, max(desk.now + 1, desk.next_timer())), actions=actions)
        desk.read_results(actions, step["results"])
        desk.now = step["now"]
        for event in step["events"]:
            desk.handle(event)
        desk.fire_timers()
        if desk.unposted:
            desk.pair_now()
    return desk


def run_seed(seed: str, level: int, policy: Policy, tag: str, keep: bool) -> dict:
    client = Client()
    road = Road(client.map())
    world = client.create(level=level, seed=seed)
    LIVE.add(world.id)
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
        import explore
        explore.save(client, world, f"{tag}-{seed}", level, [])
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
                  f"{r['onTimeShare']:6.1%} on time  comp≈{r['comp']}  pairs={r.get('pairs',0)}/{r.get('singles',0)} exp={r.get('expired',0)} urg={r.get('urgent',0)}  q/d={r['quoted']}/{r['declined']}  {r['secs']}s", flush=True)
    if out:
        m = sum(r["marginCents"] for r in out) / len(out) / 100
        ot = min(r["onTimeShare"] for r in out)
        ranked = sum(r["ranked"] for r in out)
        print(f"{tag:>6} MEAN margin €{m:,.0f}/week  worst on-time {ot:.1%}  ranked {ranked}/{len(out)}", flush=True)
        os.makedirs(os.path.join(ROOT, "runs"), exist_ok=True)
        with open(os.path.join(ROOT, "runs", f"{tag}.json"), "w") as fh:
            json.dump({"policy": asdict(policy), "results": out}, fh, indent=1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=8)
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--prefix", default="ev")
    ap.add_argument("--tag", default="v7")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--keep", action="store_true", help="save the reveal for refitting")
    ap.add_argument("--set", nargs="*", default=[], help="policy overrides, e.g. fee_open=0.9 min_margin=5")
    a = ap.parse_args()
    pol = Policy()
    for kv in a.set:
        k, v = kv.split("=", 1)
        pol = replace(pol, **{k: type(getattr(pol, k))(json.loads(v)) if not isinstance(getattr(pol, k), tuple) else tuple(map(tuple, json.loads(v)))})
    sweep([f"{a.prefix}-{i}" for i in range(1, a.seeds + 1)], a.level, pol, a.tag, a.parallel, a.keep)


if __name__ == "__main__":
    main()
