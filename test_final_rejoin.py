"""Offline tests for final.py: Desk.rejoin on archived level-8 worlds, and the final loop run against
a recorded event list through a mocked world.

    ./.venv/bin/python test_final_rejoin.py                 # 5 diverse worlds, all checks
    ./.venv/bin/python test_final_rejoin.py --worlds 3      # fewer worlds
    ./.venv/bin/python test_final_rejoin.py --loop-world w..  # run the loop smoke test on this world

It never creates, forks, advances or deletes a world. The only server use is one free GET /v1/map
when data/map.json is missing (the map costs no token).

Per archived world (see data/L8/<worldId>/):
  1. replay  - a fresh Dispatcher is fed the world's whole event history with desk.now updated per
               event, held-order timers fired and the outbox sent to a fake world. Checks: no handler
               raised; every price request was quoted before its respondBy; every undelivered order
               has a gig at the end of the week and no timer is left behind.
  2. rejoin  - a fresh Dispatcher at the archived end snapshot calls Desk.rejoin(snapshot, history)
               and final.restore_state, exactly as final.py does after a restart. Checks: the same
               orders as the replay; deliveredAt matches the snapshot; every live gig is carried;
               every undelivered order is carried or posted; requests, the deadline-warning set and
               the level-8 dealer trust are restored; the rebuilt desk still handles a later event.
  3. loop    - final.play() is run once over a recorded event list through a fake stream in simulated
               time, with one injected Unreachable (a server restart) and one repeated event after the
               reconnect. Checks: every price request answered inside its 15-minute window, held-order
               timers fired, the repeated seq dropped, the books rebuilt, the heartbeat printed, no
               handler errors, and the loop reaches the end of the week.
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import os
import sys
import time
import traceback
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "L8")
ZERO_SCORE = {"marginCents": 0, "revenueCents": 0, "marginShare": 0.0, "wonOrders": 0,
              "onTimeShare": 1.0, "undeliveredShare": 0.0, "ranked": False}
LEVEL = 8

import final  # noqa: E402  (the runner under test lives next to this file)


def note(message: str) -> None:
    print(message, flush=True)


def read_gzip(*parts: str) -> Any:
    with gzip.open(os.path.join(*parts), "rt") as handle:
        return json.load(handle)


def load_world(world_id: str):
    """The archived snapshot, the whole event history and the roster of one finished world."""
    folder = os.path.join(DATA, world_id)
    return (read_gzip(folder, "snapshot.json.gz"),
            read_gzip(folder, "events.json.gz"),
            read_gzip(folder, "roster.json.gz"))


def load_map() -> dict:
    """The map: data/map.json when the team has cached it, else one free GET /v1/map."""
    path = os.path.join(HERE, "data", "map.json")
    if os.path.exists(path):
        with open(path) as handle:
            return json.load(handle)
    from spread.client import Client
    note("data/map.json is not cached: one free GET /v1/map")
    return Client().map()


# ---------------------------------------------------------------- the world archive

def world_stats() -> list[dict]:
    """One row per archived level-8 world, from the snapshot (cheap) and the size of its events file."""
    rows = []
    for world_id in sorted(os.listdir(DATA)):
        folder = os.path.join(DATA, world_id)
        if not os.path.isdir(folder):
            continue
        events_path = os.path.join(folder, "events.json.gz")
        if not all(os.path.exists(os.path.join(folder, name))
                   for name in ("snapshot.json.gz", "events.json.gz", "roster.json.gz")):
            continue  # a run that is still being written
        try:
            snapshot = read_gzip(folder, "snapshot.json.gz")
        except (OSError, ValueError):
            continue
        score = snapshot.get("score") or {}
        rows.append({
            "wid": world_id,
            "margin": int(score.get("marginCents", 0)),
            "on_time": float(score.get("onTimeShare", 0.0)),
            "won": int(score.get("wonOrders", 0)),
            "cancelled": sum(1 for order in snapshot.get("orders", []) if order.get("status") == "cancelled"),
            "orders": len(snapshot.get("orders", [])),
            "gigs": len(snapshot.get("gigs", [])),
            "events_bytes": os.path.getsize(events_path),
        })
    return rows


def pick(rows: list[dict], count: int) -> list[dict]:
    """Five diverse worlds, deterministically: best and worst margin, most orders won, most cancelled
    orders, and the largest event history (the stress case)."""
    picks: list[dict] = []

    def add(row: Optional[dict]) -> None:
        if row is not None and row["wid"] not in {p["wid"] for p in picks}:
            picks.append(row)

    add(max(rows, key=lambda r: (r["margin"], r["wid"])))
    add(min(rows, key=lambda r: (r["margin"], r["wid"])))
    add(max(rows, key=lambda r: (r["won"], r["wid"])))
    add(max(rows, key=lambda r: (r["cancelled"], r["wid"])))
    add(max(rows, key=lambda r: (r["events_bytes"], r["wid"])))
    for row in sorted(rows, key=lambda r: (r["margin"], r["wid"])):
        if len(picks) >= count:
            break
        add(row)
    return picks[:count]


# ---------------------------------------------------------------- fake worlds

class FakeClient:
    """Stands in for spread.client.Client: only the reads final.py makes."""

    def __init__(self, world):
        self.world = world

    def final(self) -> dict:
        return {"worldId": self.world.id, "startsAt": None, "now": int(self.world.clock)}

    def worlds(self) -> list[dict]:
        return [{"worldId": self.world.id, "kind": "final", "level": LEVEL, "parent": None,
                 "season": None, "boardRun": None, "ended": False}]


class FakeWorld:
    """Records every action; accepts every post with a fresh gig id. Enough to replay a desk."""

    def __init__(self, world_id: str, roster: list[dict], ends_at: int):
        self.id = world_id
        self.roster_data = roster
        self.ends_at = ends_at
        self.clock = 0
        self.log: list[tuple[int, dict]] = []   # (world minute, action)
        self.count = 0
        self.client = FakeClient(self)

    def roster(self) -> list[dict]:
        return self.roster_data

    def score(self) -> dict:
        return dict(ZERO_SCORE)

    def snapshot(self):  # pragma: no cover - a replayed desk never asks
        raise AssertionError("the replayed desk asked for a snapshot")

    def events(self, after: int = -1):  # pragma: no cover - a replayed desk never asks
        raise AssertionError("the replayed desk asked for events")

    def act(self, actions) -> list[dict]:
        results = []
        for action in actions:
            self.log.append((self.clock, action))
            if action.get("type") == "post":
                self.count += 1
                results.append({"ok": True, "id": f"F{self.count}"})
            else:
                results.append({"ok": True})
        return results


class LoopWorld(FakeWorld):
    """The same fake world, driven like the live final: a stream of the recorded events in simulated
    time, a snapshot rebuilt from what the fake world knows, and one injected server restart."""

    def __init__(self, world_id: str, snapshot: dict, history: list[dict], roster: list[dict], drop_at: Optional[int]):
        super().__init__(world_id, roster, snapshot["endsAt"])
        self.final_snapshot = snapshot
        self.history = history
        self.positions = [event["seq"] for event in history]
        self.pos = 0
        self.drop_at = drop_at
        self.repeat_pending = False
        self.gigs: list[dict] = []

    # ---- reads
    def events(self, after: int = -1) -> list[dict]:
        return [event for event in self.history[: self.pos] if event["seq"] > after]

    def snapshot(self) -> dict:
        if self.pos == 0:
            return {"worldId": self.id, "now": 0, "endsAt": self.ends_at, "openRequests": [],
                    "orders": [], "gigs": [], "driverPositions": [], "score": dict(ZERO_SCORE)}
        won: dict[str, dict] = {}
        delivered: dict[str, dict[int, int]] = {}
        cancelled: set[str] = set()
        for event in self.history[: self.pos]:
            kind = event["kind"]
            if kind == "order_won":
                won[event["order"]["id"]] = event["order"]
            elif kind == "order_cancelled":
                cancelled.add(event["orderId"])
            elif kind == "delivered":
                delivered.setdefault(event["orderId"], {})[event["leg"]] = event["at"]
        orders = []
        for order_id, won_order in won.items():
            if order_id in cancelled:
                continue
            base = dict((self.final_snapshot_orders().get(order_id) or won_order))
            legs = len(base.get("legs") or won_order["legs"])
            done = list(base.get("deliveredAt") or [None] * legs)
            for leg, at in delivered.get(order_id, {}).items():
                if leg < len(done):
                    done[leg] = at
            base["deliveredAt"] = done
            orders.append(base)
        return {"worldId": self.id, "now": self.clock, "endsAt": self.ends_at, "openRequests": [],
                "orders": orders, "gigs": [dict(gig) for gig in self.gigs],
                "driverPositions": [], "score": dict(ZERO_SCORE)}

    def final_snapshot_orders(self) -> dict:
        if not hasattr(self, "_by_id"):
            self._by_id = {order["id"]: order for order in self.final_snapshot.get("orders", [])}
        return self._by_id

    # ---- sending
    def act(self, actions) -> list[dict]:
        results = []
        for action in actions:
            self.log.append((self.clock, action))
            if action.get("type") == "post":
                self.count += 1
                gig_id = f"F{self.count}"
                self.gigs.append({"id": gig_id, "stops": action["stops"], "audience": action["audience"],
                                  "schedule": action["schedule"], "status": "open", "postedAt": self.clock,
                                  "feeCents": action["schedule"][0]["feeCents"], "driverId": None})
                results.append({"ok": True, "id": gig_id})
            else:
                results.append({"ok": True})
        return results

    # ---- the stream
    def stream(self, after: Optional[int] = None, idle: Optional[float] = None):
        index = bisect.bisect_right(self.positions, -1 if after is None else after)
        if self.repeat_pending and index > 0:
            self.repeat_pending = False
            yield dict(self.history[index - 1])  # a repeated event after a server restart: dropped by seq
        while index < len(self.history):
            if self.drop_at is not None and index >= self.drop_at:
                self.drop_at = None
                self.repeat_pending = True
                raise _unreachable("simulated server restart")
            event = self.history[index]
            index += 1
            self.pos = max(self.pos, index)
            self.clock = max(self.clock, event["at"])
            yield event
            if index % 500 == 0:
                self.clock = min(self.ends_at, self.clock + 5)
                yield None
        while self.clock < self.ends_at:
            self.clock = min(self.ends_at, self.clock + 30)
            yield None


def _unreachable(message: str):
    from spread.client import Unreachable
    return Unreachable(0, message)


# ---------------------------------------------------------------- check 1: replay a whole history

def replay(world_id: str, snapshot: dict, history: list[dict], roster: list[dict], ctx: dict, log=note):
    """Feeds one archived world's whole event history to a fresh Dispatcher, the way the live loop
    handles events: the desk's clock moves to each event, held orders fire, the outbox is sent."""
    world = FakeWorld(world_id, roster, snapshot["endsAt"])
    desk = final.build_desk(world, LEVEL, 0, ctx, log)
    final.init_supply(desk, world, log)
    errors: list[tuple] = []
    timers = 0
    for event in history:
        desk.now = max(desk.now, event["at"])
        world.clock = desk.now
        try:
            desk.handle(event)
        except Exception as error:  # noqa: BLE001 - the check reports it
            errors.append((event["seq"], event["kind"], f"{type(error).__name__}: {error}"))
        outbox = list(desk.outbox)
        desk.outbox.clear()
        if outbox:
            try:
                desk.read_results(outbox, world.act(outbox))
            except Exception as error:  # noqa: BLE001
                errors.append((event["seq"], "read_results", f"{type(error).__name__}: {error}"))
        world.clock = desk.now
        timers += final.fire_timers(desk, log)
        outbox = list(desk.outbox)
        desk.outbox.clear()
        if outbox:
            desk.read_results(outbox, world.act(outbox))
    return desk, world, errors, timers


def check_replay(world_id: str, snapshot: dict, history: list[dict], roster: list[dict], ctx: dict, log=note):
    started = time.time()
    desk, world, errors, timers = replay(world_id, snapshot, history, roster, ctx, log)
    requests = {event["request"]["id"]: event["request"] for event in history if event["kind"] == "price_request"}
    first_quote: dict[str, int] = {}
    for clock, action in world.log:
        if action.get("type") == "quote":
            first_quote.setdefault(action["requestId"], clock)
    unanswered = [request_id for request_id in requests if request_id not in first_quote]
    late = [request_id for request_id in requests
            if request_id in first_quote and first_quote[request_id] >= requests[request_id]["respondBy"]]
    missing = [order_id for order_id, order in desk.orders.items()
               if any(at is None for at in order["deliveredAt"]) and not desk.gigs_of(order_id)]
    left = getattr(desk, "timers", [])
    checks = [
        ("replay: no handler raised", not errors, f"{len(errors)} errors {errors[:2]}"),
        ("replay: every price request quoted", not unanswered, f"{len(unanswered)} unanswered of {len(requests)}"),
        ("replay: quotes inside the 15-minute window", not late, f"{len(late)} late"),
        # v8 posts a won order at once (Policy.hold_hours = 0), so there are no held-order timers to fire;
        # a held timer firing is still required whenever the policy uses them.
        ("replay: held-order timers fired", timers > 0 or getattr(getattr(desk, "p", None), "hold_hours", 0) <= 0,
         f"{timers} timer fires"),
        ("replay: no undelivered order without a gig", not missing, f"{len(missing)} missing {missing[:5]}"),
        ("replay: no timer left at the end", not left, f"{len(left)} left"),
    ]
    return checks, desk, round(time.time() - started, 2)


# ---------------------------------------------------------------- check 2: rejoin from a snapshot

def expected_trust(history: list[dict], orders_by_id: dict[str, dict]) -> tuple[dict[str, int], set[str]]:
    """The level-8 dealer trust as the ledger implies it: +1 per order delivered on time (up to 25), a
    late one halves the dealer's count."""
    late: set[str] = set()
    for event in history:
        if event["kind"] != "ledger":
            continue
        line = event["line"]
        if line.get("orderId") and line["kind"] in ("late_refund", "undelivered_penalty"):
            late.add(line["orderId"])
    counts: dict[str, int] = {}
    settled: set[str] = set()
    for event in history:
        if event["kind"] != "ledger":
            continue
        line = event["line"]
        order_id = line.get("orderId")
        if not order_id or line["kind"] != "revenue":
            continue
        order = orders_by_id.get(order_id)
        if order is None:
            continue
        dealer = order["dealerId"]
        settled.add(order_id)
        counts[dealer] = counts.get(dealer, 0) // 2 if order_id in late else min(25, counts.get(dealer, 0) + 1)
    return counts, settled


def check_rejoin(world_id: str, snapshot: dict, history: list[dict], roster: list[dict], ctx: dict,
                 replay_desk, log=note):
    """Builds a fresh Dispatcher at the archived end snapshot and rejoins, exactly as final.py does
    after the system (or the server) comes back."""
    started = time.time()
    world = FakeWorld(world_id, roster, snapshot["endsAt"])
    desk = final.build_desk(world, LEVEL, snapshot["now"], ctx, log)
    final.init_supply(desk, world, log)
    checks: list[tuple[str, bool, str]] = []
    try:
        desk.rejoin(snapshot, history)
        final.restore_state(desk, snapshot, history, log)
        final.fire_timers(desk, log)
        outbox = list(desk.outbox)
        desk.outbox.clear()
        if outbox:
            desk.read_results(outbox, world.act(outbox))
    except Exception:  # noqa: BLE001 - reported as a failed check
        detail = traceback.format_exc(limit=6).strip().splitlines()[-1]
        return [("rejoin: Desk.rejoin did not raise", False, detail)], None, round(time.time() - started, 2)
    checks.append(("rejoin: Desk.rejoin did not raise", True, "ok"))
    requests = {event["request"]["id"] for event in history if event["kind"] == "price_request"}
    by_id = {order["id"]: order for order in snapshot["orders"]}
    expected_orders = {order_id for order_id, order in by_id.items()
                       if order["status"] != "cancelled" and order_id in requests}
    checks.append(("rejoin: orders match the snapshot",
                   set(desk.orders) == expected_orders, f"{len(desk.orders)} vs {len(expected_orders)}"))
    if replay_desk is not None:
        checks.append(("rejoin: same orders as the replay",
                       set(desk.orders) == set(replay_desk.orders),
                       f"{len(desk.orders)} vs {len(replay_desk.orders)}"))
    bad_delivered = [order_id for order_id in expected_orders
                     if desk.orders[order_id]["deliveredAt"] != by_id[order_id]["deliveredAt"]]
    checks.append(("rejoin: deliveredAt matches the snapshot", not bad_delivered, f"{len(bad_delivered)} differ"))
    live = [gig for gig in snapshot["gigs"] if gig["status"] in ("open", "booked", "working")]
    bad_gigs = [(gig["id"], stop["orderId"], stop["leg"]) for gig in live for stop in gig["stops"]
                if desk.leg_gigs.get(stop["orderId"], {}).get(stop["leg"]) != gig["id"]]
    checks.append(("rejoin: every live gig carried", not bad_gigs,
                   f"{len(bad_gigs)} of {len(live)} live gigs not carried {bad_gigs[:3]}"))
    posted = {action["stops"][0]["orderId"] for _, action in world.log if action.get("type") == "post"}
    posted |= {action["stops"][0]["orderId"] for action in desk.outbox if action.get("type") == "post"}
    needing = [order_id for order_id, order in desk.orders.items() if any(at is None for at in order["deliveredAt"])]
    unposted = [order_id for order_id in needing if not desk.gigs_of(order_id) and order_id not in posted]
    checks.append(("rejoin: every undelivered order carried or posted", not unposted,
                   f"{len(unposted)} of {len(needing)} not carried {unposted[:5]}"))
    checks.append(("rejoin: price requests restored", set(desk.requests) == requests,
                   f"{len(desk.requests)} vs {len(requests)}"))
    urgent_expected = {event["orderId"] for event in history if event["kind"] == "deadline_close"}
    checks.append(("rejoin: deadline warnings restored",
                   set(getattr(desk, "urgent", set())) == urgent_expected,
                   f"{len(getattr(desk, 'urgent', set()))} vs {len(urgent_expected)}"))
    trust_expected, settled_expected = expected_trust(history, by_id)
    checks.append(("rejoin: dealer trust rebuilt from the ledger",
                   getattr(desk, "trust", None) == trust_expected,
                   f"{len(getattr(desk, 'trust', {}) or {})} vs {len(trust_expected)} dealers"))
    checks.append(("rejoin: settled orders restored",
                   set(getattr(desk, "settled", set())) == settled_expected,
                   f"{len(getattr(desk, 'settled', set()))} vs {len(settled_expected)}"))
    later = next((event for event in reversed(history) if event["kind"] in ("forecast", "release", "gig_viewed")), None)
    usable, detail = True, "ok"
    if later is not None:
        try:
            desk.now = snapshot["now"]
            desk.handle(dict(later))
            outbox = list(desk.outbox)
            desk.outbox.clear()
            if outbox:
                desk.read_results(outbox, world.act(outbox))
        except Exception:  # noqa: BLE001
            usable, detail = False, traceback.format_exc(limit=6).strip().splitlines()[-1]
    checks.append(("rejoin: still handles a later event", usable, detail))
    return checks, desk, round(time.time() - started, 2)


# ---------------------------------------------------------------- check 3: the live loop, mocked

def check_loop(row: dict, snapshot: dict, history: list[dict], roster: list[dict], ctx: dict, road, log=note):
    """Runs final.play() over a recorded event list through a fake stream in simulated time, with one
    injected Unreachable (a server restart) and one repeated event after the reconnection."""
    started = time.time()
    drop_at = max(50, len(history) * 3 // 5)
    world = LoopWorld(row["wid"], snapshot, history, roster, drop_at)
    lines: list[str] = []
    previous_retry = final.RETRY
    final.RETRY = 0.0  # do not sleep 5 s per rebuild in the test
    try:
        player = final.play(world, LEVEL, road, ctx, log=lines.append)
    except Exception:  # noqa: BLE001 - reported as a failed check
        final.RETRY = previous_retry
        detail = traceback.format_exc(limit=8).strip().splitlines()[-1]
        return [("loop: final.play did not raise", False, detail)], round(time.time() - started, 2)
    finally:
        final.RETRY = previous_retry
    requests = {event["request"]["id"]: event["request"] for event in history if event["kind"] == "price_request"}
    quotes: dict[str, int] = {}
    for clock, action in world.log:
        if action.get("type") == "quote":
            quotes.setdefault(action["requestId"], clock)
    unanswered = [request_id for request_id in requests if request_id not in quotes]
    late = [request_id for request_id in requests
            if request_id in quotes and quotes[request_id] >= requests[request_id]["respondBy"]]
    ended = player.desk is not None and player.desk.now >= (player.ends_at or 0)
    rebuilt = player.heals >= 1 and any("stream lost" in line or "books rebuilt" in line for line in lines)
    checks = [
        ("loop: reached the end of the week", ended, f"desk.now {None if player.desk is None else player.desk.now} of {player.ends_at}"),
        ("loop: every price request answered", not unanswered, f"{len(unanswered)} unanswered of {len(requests)}"),
        ("loop: all quotes inside the 15-minute window", not late, f"{len(late)} late"),
        ("loop: held-order timers fired", player.timers_fired > 0 or not uses_hold_timers,
         f"{player.timers_fired} timer fires"),
        ("loop: repeated event dropped by seq", player.dropped >= 1, f"{player.dropped} dropped"),
        ("loop: rebuilt after the injected interruption", rebuilt, f"{player.heals} rebuilds"),
        ("loop: heartbeat printed", any(line.startswith("heartbeat") for line in lines),
         f"{sum(1 for line in lines if line.startswith('heartbeat'))} heartbeats"),
        ("loop: no handler errors", player.errors == 0, f"{player.errors} errors {lines[-2:] if player.errors else ''}"),
    ]
    return checks, round(time.time() - started, 2)


# ---------------------------------------------------------------- check 4: waiting for the final

class WaitWorld:
    def __init__(self, clock: int, ends_at: int):
        self.id = "wfakefinal"
        self.clock, self.ends_at = clock, ends_at

    def snapshot(self) -> dict:
        return {"worldId": self.id, "now": self.clock, "endsAt": self.ends_at, "openRequests": [],
                "orders": [], "gigs": [], "driverPositions": [], "score": dict(ZERO_SCORE)}


class WaitClient:
    """A fake Client for wait_for_final: GET /v1/final answers 404 for a while, then the final is set."""

    def __init__(self, target, not_set: int = 0):
        self.target, self.not_set, self.calls = target, not_set, 0

    def final(self) -> dict:
        from spread.client import SpreadError
        self.calls += 1
        if self.calls <= self.not_set:
            raise SpreadError(404, "No final is scheduled for this team")
        return {"worldId": self.target.id, "startsAt": None, "now": self.target.clock}

    def world(self, world_id: str):
        return self.target

    def worlds(self) -> list[dict]:
        return [{"worldId": self.target.id, "kind": "final", "level": LEVEL, "parent": None,
                 "season": None, "boardRun": None, "ended": False}]


class InterruptingWaitClient(WaitClient):
    """Keeps answering with the same (ended) final; the test stops it with KeyboardInterrupt."""

    def __init__(self, target, stop_after: int = 3):
        super().__init__(target)
        self.stop_after = stop_after

    def final(self) -> dict:
        self.calls += 1
        if self.calls > self.stop_after:
            raise KeyboardInterrupt
        return {"worldId": self.target.id, "startsAt": None, "now": self.target.clock}


def check_wait() -> list[tuple[str, bool, str]]:
    """The pre-start polling: 404 until a final is set, an ended last final is not played."""
    checks: list[tuple[str, bool, str]] = []
    previous_poll = final.POLL
    final.POLL = 0.0
    try:
        world, lines = WaitWorld(0, 14280), []
        client = WaitClient(world, not_set=2)
        got_world, level, snapshot = final.wait_for_final(client, LEVEL, log=lines.append)
        checks.append(("wait: polls through 404 until a final is set",
                       got_world is world and snapshot["now"] == 0 and level == LEVEL,
                       f"{client.calls} polls, level {level}"))
        checks.append(("wait: says it is waiting, then says the final is set",
                       any("no final is set" in line for line in lines) and any("is set" in line for line in lines),
                       f"{lines[:2]}"))
        ended, ended_lines = WaitWorld(14280, 14280), []
        try:
            final.wait_for_final(InterruptingWaitClient(ended), LEVEL, log=ended_lines.append)
            kept_waiting = False
        except KeyboardInterrupt:
            kept_waiting = True
        checks.append(("wait: an ended final is not played, it keeps waiting",
                       kept_waiting and any("has ended" in line for line in ended_lines),
                       f"{ended_lines[:2]}"))
    finally:
        final.POLL = previous_poll
    return checks


# ---------------------------------------------------------------- check 5: the safety nets

class StubDesk:
    """The smallest desk the Player needs, for testing its own safety nets."""

    def __init__(self):
        self.outbox: list[dict] = []
        self.now = 0
        self.received: list[tuple] = []
        self.fail = False

    def handle(self, event: dict) -> None:
        if self.fail:
            raise RuntimeError("simulated dispatcher bug")

    def read_results(self, actions, results) -> None:
        self.received.append((list(actions), list(results)))

    def gigs_of(self, order_id: str) -> list[str]:
        return []


class SafetyWorld(FakeWorld):
    """A world that refuses actions of some types with a 400, like the server refuses an invalid action."""

    def __init__(self):
        super().__init__("wfakesafety", [], 14280)
        self.bad_types = {"post"}

    def act(self, actions) -> list[dict]:
        from spread.client import SpreadError
        actions = list(actions)
        if any(action.get("type") in self.bad_types for action in actions):
            raise SpreadError(400, "the server refused an action", None)
        return super().act(actions)


def check_safety(ctx: dict) -> list[tuple[str, bool, str]]:
    """The runner's own safety nets: an unanswered price request still gets a decline quote; one
    malformed action is dropped on its own while the rest of the batch still goes out; a repeated seq
    is dropped."""
    quiet = lambda message: None  # noqa: E731
    checks: list[tuple[str, bool, str]] = []
    world = SafetyWorld()
    player = final.Player(world, LEVEL, None, ctx, log=quiet)
    desk = StubDesk()
    desk.fail = True
    player.desk = desk
    request = {"id": "R99999", "dealerId": "D01", "batchId": None, "arrivedAt": 0, "respondBy": 15,
               "legs": [], "options": [{"kind": "same_day", "deadline": 1200, "listPriceCents": 10000}],
               "referenceFeeCents": 5000, "note": None}
    event = {"seq": 0, "at": 0, "kind": "price_request", "request": request}
    player.on_event(event)
    quotes = [action for _, action in world.log if action.get("type") == "quote"]
    expected = int(request["options"][0]["listPriceCents"] * final.DECLINE_MULT)
    checks.append(("safety: an unanswered price request still gets a decline quote",
                   bool(quotes) and quotes[0]["prices"].get("same_day") == expected,
                   f"{quotes[0]['prices'] if quotes else 'no quote'}"))
    checks.append(("safety: the handler error is counted, the loop lives on", player.errors == 1, f"{player.errors}"))
    player.on_event(event)  # the same seq again: a repeated event after a restart
    checks.append(("safety: a repeated event is dropped by seq", player.dropped == 1, f"{player.dropped}"))

    world2 = SafetyWorld()
    world2.bad_types = {"post"}
    player2 = final.Player(world2, LEVEL, None, ctx, log=quiet)
    desk2 = StubDesk()
    player2.desk = desk2
    desk2.outbox = [
        {"type": "post", "stops": [{"orderId": "R1", "leg": 0, "kind": "pickup"}],
         "audience": {"experienced": False, "manualLicence": False},
         "schedule": [{"at": 0, "feeCents": 1000}]},
        {"type": "quote", "requestId": "R1", "prices": {"same_day": 1000}},
    ]
    player2.flush()
    sent_quotes = [action for _, action in world2.log if action.get("type") == "quote"]
    checks.append(("safety: a malformed action is dropped alone, the rest of the batch still goes out",
                   len(sent_quotes) == 1 and player2.errors == 1 and player2.sent == 1,
                   f"{len(sent_quotes)} sent, {player2.errors} errors"))
    return checks


# ---------------------------------------------------------------- check 6: the dispatch fallback

def check_preflight(road, ctx: dict) -> list[tuple[str, bool, str]]:
    """final.py's own pre-flight: the Dispatcher must answer one synthetic price request."""
    lines: list[str] = []
    ok = final.self_check(road, LEVEL, ctx, log=lines.append)
    return [("pre-flight: dispatch.py quotes a synthetic price request", ok, "; ".join(lines)[:200] or "no output")]



def check_fallback(ctx: dict) -> list[tuple[str, bool, str]]:
    """A dispatch.py bug during a rebuild must not cost the week: the runner retries with the kit Desk."""
    quiet = lambda message: None  # noqa: E731
    from spread.starter import Desk as KitDesk

    original = final.build_desk
    calls = {"n": 0}

    def flaky(world, level, now, context, log=note):
        calls["n"] += 1
        if context.get("backend") == "dispatch":
            class Broken:
                outbox: list = []

                def rejoin(self, snapshot, history):
                    raise RuntimeError("simulated dispatch.py bug in rejoin")

            return Broken()
        return original(world, level, now, context, log)

    empty = {"worldId": "wfakefallback", "now": 0, "endsAt": 14280, "openRequests": [],
             "orders": [], "gigs": [], "driverPositions": [], "score": dict(ZERO_SCORE)}
    world = LoopWorld("wfakefallback", empty, [], [], None)
    local_ctx = dict(ctx)
    ok, detail = True, "ok"
    final.build_desk = flaky
    try:
        player = final.Player(world, LEVEL, None, local_ctx, log=quiet)
        snapshot = player.start(tries=1)
        ok = (local_ctx.get("backend") == "starter" and isinstance(player.desk, KitDesk)
              and snapshot["now"] == 0 and calls["n"] >= 2)
        detail = f"backend {local_ctx.get('backend')}, desk {type(player.desk).__name__}, {calls['n']} builds"
    except Exception:  # noqa: BLE001
        ok, detail = False, traceback.format_exc(limit=6).strip().splitlines()[-1]
    finally:
        final.build_desk = original
    return [("fallback: a dispatch.py rejoin bug rebuilds with the kit Desk", ok, detail)]


# ---------------------------------------------------------------- the report

def report_check(name: str, ok: bool, detail: str = "") -> bool:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  [{detail}]"), flush=True)
    return ok


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Offline rejoin and loop tests for final.py.")
    parser.add_argument("--worlds", type=int, default=5, help="how many archived worlds to test (default 5)")
    parser.add_argument("--loop-world", default=None, help="the world for the stream-loop test (default: the smallest of the picks)")
    parser.add_argument("--skip-loop", action="store_true", help="skip the stream-loop smoke test")
    args = parser.parse_args(argv)

    runner = final  # the production runner under test
    road = runner.Road(load_map())
    ctx = runner.prepare_backend(road, LEVEL)

    rows = world_stats()
    if not rows:
        note(f"no archived worlds under {DATA}; nothing to test")
        return 1
    picks = pick(rows, max(1, args.worlds))
    note(f"testing final.py on {len(picks)} of {len(rows)} archived level-8 worlds:")
    for row in picks:
        note(f"  {row['wid']}  margin EUR {row['margin'] / 100:>10,.0f}  {row['won']:>4} won  "
             f"{row['cancelled']:>2} cancelled  {row['orders']:>4} orders  {row['gigs']:>4} gigs  "
             f"events {row['events_bytes'] / 1024:>7,.0f} KiB")

    failures = 0
    loop_choice = None
    for index, row in enumerate(picks):
        world_id = row["wid"]
        try:
            snapshot, history, roster = load_world(world_id)
        except Exception:  # noqa: BLE001
            note(f"[{index + 1}/{len(picks)}] {world_id}: could not load the archive")
            note(traceback.format_exc(limit=4))
            failures += 1
            continue
        note(f"\n[{index + 1}/{len(picks)}] {world_id}  margin EUR {row['margin'] / 100:,.0f}  "
             f"{len(history)} events  {len(snapshot['orders'])} orders  {len(roster)} drivers")
        replay_checks, replay_desk, replay_secs = check_replay(world_id, snapshot, history, roster, ctx, log=note)
        rejoin_checks, _, rejoin_secs = check_rejoin(world_id, snapshot, history, roster, ctx, replay_desk, log=note)
        checks = replay_checks + rejoin_checks
        for name, ok, detail in checks:
            failures += 0 if report_check(name, ok, detail) else 1
        note(f"  world {world_id}: {'PASS' if all(ok for _, ok, _ in checks) else 'FAIL'}  "
             f"(replay {replay_secs}s, rejoin {rejoin_secs}s)")
        if loop_choice is None or (args.loop_world and world_id == args.loop_world):
            loop_choice = (row, snapshot, history, roster, rejoin_secs)
        if loop_choice is not None and args.loop_world is None and row["events_bytes"] < loop_choice[0]["events_bytes"]:
            loop_choice = (row, snapshot, history, roster, rejoin_secs)

    note("\nrunner safety nets (waiting for the final, unanswered requests, malformed actions, repeats):")
    previous_warning = final.warning
    final.warning = lambda message: None  # the safety nets deliberately provoke errors; keep the output readable
    try:
        safety_checks = check_wait() + check_safety(ctx) + check_preflight(road, ctx) + check_fallback(ctx)
    finally:
        final.warning = previous_warning
    for name, ok, detail in safety_checks:
        failures += 0 if report_check(name, ok, detail) else 1

    if not args.skip_loop and loop_choice is not None:
        row, snapshot, history, roster, _ = loop_choice
        note(f"\nstream-loop smoke test on {row['wid']} ({len(history)} events, one injected server restart):")
        loop_checks, loop_secs = check_loop(row, snapshot, history, roster, ctx, road, log=note)
        for name, ok, detail in loop_checks:
            failures += 0 if report_check(name, ok, detail) else 1
        note(f"  loop: {'PASS' if all(ok for _, ok, _ in loop_checks) else 'FAIL'}  ({loop_secs}s)")

    note("")
    if failures:
        note(f"{failures} CHECK(S) FAILED")
        return 1
    note("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
