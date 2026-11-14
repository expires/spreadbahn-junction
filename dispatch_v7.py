"""Dispatcher v1: margin-maximising quotes with an online competitor model, slack-aware fee schedules,
and an SLA controller. Built on the kit's starter for bookkeeping (rejoin, reposts, notes, final loop).

    python dispatch.py --seeds 6 --prefix ev            # practice sweep at level 8
    python dispatch.py --board                          # next ranking run
    python dispatch.py --final                          # the live final
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
from spread.starter import DAY, HOUR, WAKE_ON, Desk, steep, undelivered
from spread.types import FeeStep, Order, PriceRequest

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
    hold_hours: float = 36.0     # post an order only this many hours before lastPickupAt
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
        self.p, self.road = policy, road
        self.log_comp = math.log(policy.comp0)
        self.day_of_comp = now // DAY
        self.quoted: dict[str, tuple[float, int]] = {}  # request id -> (log ratio of cheapest quote, day)
        self.won = self.late = self.on_time = 0
        self.stats = {"quoted": 0, "declined": 0, "won": 0, "lost": 0}
        self.timers: list[tuple[int, str]] = []  # (minute, order id): held orders to post later
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

    # ---------- quotes ----------
    def slack_hours(self, request: PriceRequest, deadline: int) -> float:
        """Hours from posting (at respondBy) to the last pickup that still makes the deadline."""
        drive = sum(self.road.minutes(l["pickup"]["placeId"], l["delivery"]["placeId"]) for l in request["legs"])
        last_pickup = deadline - drive - 30
        return (last_pickup - request["respondBy"]) / 60

    def quote(self, request: PriceRequest) -> None:
        self.requests[request["id"]] = request
        p = self.p
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
        comp = math.exp(self.log_comp) * boost
        prices: dict[str, int] = {}
        best_lr = None
        for o in request["options"]:
            lst = o["listPriceCents"] / 100
            slack = self.slack_hours(request, o["deadline"])
            sl = self.supply_late(request)
            fee = ref * step_lookup(p.exp_fee, slack) * (1.15 if request["note"] else 1.0) * (1 + p.sup_fee * max(0.0, sl - p.sup_base))
            pl = min(0.95, step_lookup(p.p_late, slack) + p.km_late * km + max(0.0, sl - p.sup_base) + pressure)
            best, best_x = -1e9, None
            for i in range(60):
                x = 0.85 + i * 0.02  # price as x list
                price = lst * x
                win = 1 / (1 + math.exp(p.slope * (math.log(x) - math.log(comp))))
                margin = price * (1 - pl * p.late_cost) - fee - pl * p.late_shadow * (1 + p.sla_gain * pressure * 10)
                if margin * win > best:
                    best, best_x, best_margin = margin * win, x, margin
            gated = slack < p.min_slack_h or sl > p.supply_gate
            if best_x is not None and best_margin >= bar and not gated:
                prices[o["kind"]] = int(lst * best_x * p.price_mult * 100)
                lr = math.log(best_x * p.price_mult)
                best_lr = lr if best_lr is None else min(best_lr, lr)
            else:
                prices[o["kind"]] = int(o["listPriceCents"] * p.decline_mult)
        if best_lr is None:
            self.stats["declined"] += 1
            self.stats["d_bar" if pressure > 0 else "d_margin"] = self.stats.get("d_bar" if pressure > 0 else "d_margin", 0) + 1
        else:
            self.stats["quoted"] += 1
            # Learn the competitor only where we offered the dealer's fastest option: without it,
            # dealers turn to the competitor at any price, and the estimate would sink.
            fastest = request["options"][0]
            if prices[fastest["kind"]] < fastest["listPriceCents"] * 3:
                self.quoted[request["id"]] = (best_lr - math.log(boost), self.now // DAY)
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})

    def learn(self, request_id: str, won: bool) -> None:
        """One SGD step of the logistic likelihood on log(comp)."""
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
        super().handle(event)
        if event["kind"] == "delivered":
            self.settle(event["orderId"])

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
        if legs:
            super().post(order, legs, retry)

    # ---------- holding orders until closer to pickup ----------
    def win(self, order: Order) -> None:
        if order["id"] in self.requests and order["id"] not in self.orders:
            self.orders[order["id"]] = order
            post_at = order["lastPickupAt"] - int(self.p.hold_hours * HOUR)
            if post_at > self.now + 30:
                self.timers.append((post_at, order["id"]))
            else:
                self.post(order, undelivered(order))

    def held(self) -> set[str]:
        return {oid for _, oid in self.timers}

    def next_timer(self) -> int:
        return min((t for t, _ in self.timers), default=10**9)

    def fire_timers(self) -> None:
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
