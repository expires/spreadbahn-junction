"""Play the live Spread final from the event stream: one command, no arguments, no intervention.

    ./.venv/bin/python final.py              # play the team's copy of the final
    ./.venv/bin/python final.py --level 8    # optional: the level of the world (also read from the server)

What it does, in order:
  * prints a banner (server, dispatch.py version used, map size) and a pre-flight check that builds
    the Dispatcher and answers one synthetic price request, so a broken dispatch.py is visible in the
    log at launch; then polls GET /v1/final every 10 s until a final is set and its world's snapshot
    shows the week is not over (status line every 10-30 s);
  * reads the world's snapshot, its full event history and the driver roster, builds the Dispatcher
    from dispatch.py (imported lazily, so a change there cannot break this file at import time) and
    calls Desk.rejoin(snapshot, history) so a restart keeps our orders, gigs, urgent flags and the
    level-8 dealer trust rebuilt from the ledger;
  * follows GET /v1/worlds/{id}/stream?after=<last seq> (WebSocket). Every price request is quoted in
    the same action batch as the event, so a quote is sent seconds at most after the request arrives,
    well inside the 15 world-minute reply window. Pending actions are flushed in one world.act()
    call right after each event batch (at most 50 actions per call, the server's batch limit);
  * fires the dispatcher's held-order timers whenever the clock advances (stream idle tick, read from
    GET /v1/final, the authoritative clock; or the "at" of a stream event);
  * prints a heartbeat line every 60 s: world time, orders, margin, on-time share, sequence number,
    actions sent, rebuilds and handler errors, so you can verify it is alive.

Ctrl-C (and SIGTERM/SIGHUP): prints the score so far and one line saying the world keeps running.
    The world keeps moving; start the very same command again and it rebuilds from the server.

Server restart (502/503/504, a reset WebSocket or a closed stream): sleeps 5 s, re-reads snapshot,
    events and roster, builds a fresh Dispatcher, calls Desk.rejoin(snapshot, history), reconnects the
    stream with after=<last seq> and drops repeated events by seq. It heals as many times as needed
    (the rebuild count is in the heartbeat).

Network loss: the kit stream reconnects by itself with after=; after 60 failures in a row the runner
    catches Unreachable and rebuilds exactly as after a server restart. A single malformed action is
    dropped with a line in the log instead of killing the week; a handler that raises on one event is
    logged and the loop continues (a safety quote is sent if a price request was left unanswered).

How to verify it is alive: the heartbeat line every minute (world time, orders, margin, on-time
    share, seq, sent, rebuilds, errors); "final world ... is set" before the start; and at the end
    "the world has ended" with the End line and Ranked/Not ranked. The runner plays one final and
    exits when that world ends (relaunch it for a later test final).

Safety: it never creates, forks, advances or deletes a world. It only reads snapshot/events/roster/
    score/final/stream and POSTs action batches, which is all a final world allows (advance, fork and
    delete are 403 in the final). It never deletes the final on Ctrl-C: the world keeps running.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from spread.client import Client, SpreadError, Unreachable
from spread.starter import DAY, HOUR, Desk, report

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
IDLE = 0.5            # seconds the stream waits for an event before yielding None (a clock tick)
HEARTBEAT = 60.0      # seconds between heartbeat lines
RETRY = 5.0           # seconds to sleep before rebuilding after a server restart or network loss
POLL = 10.0           # seconds between GET /v1/final polls before the final is set
DECLINE_MULT = 4.0    # a quote this far above the list price always loses and costs nothing
MAX_ACTIONS = 50      # the API's batch limit for one actions call


# ---------------------------------------------------------------- small helpers

def label(minute: Optional[int]) -> str:
    """A world minute as 'Tue 09:12'. World minute 0 is Monday 00:00."""
    if minute is None:
        return "?"
    day, rest = divmod(max(0, int(minute)), DAY)
    return f"{WEEKDAYS[day % 7]} {rest // HOUR:02d}:{rest % HOUR:02d}"


def status(message: str) -> None:
    print(message, flush=True)


def warning(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Road:
    """Road minutes and km between places, from GET /v1/map. The same little interface as dispatch.Road:
    .r[(from, to)] -> road, .minutes(a, b) -> int, .km(a, b) -> float."""

    def __init__(self, world_map: dict):
        self.map = world_map
        self.roads = world_map["roads"]
        self.places = [place["id"] for place in world_map["places"]]
        self.r = {(road["from"], road["to"]): road for road in self.roads}

    def minutes(self, a: str, b: str) -> int:
        return self.r[(a, b)]["minutes"]

    def km(self, a: str, b: str) -> float:
        return self.r[(a, b)]["km"]


# ---------------------------------------------------------------- the dispatch backend

class DryWorld:
    """Only for the startup checks: a world the dispatcher may hold but must not read from or act on."""

    id = "startup-self-check"

    def act(self, actions):
        raise AssertionError("the startup self-check must not send actions")

    def snapshot(self):
        raise AssertionError("the startup self-check does not read the world")

    def events(self, after: int = -1):
        raise AssertionError("the startup self-check does not read the world")

    def score(self):
        raise AssertionError("the startup self-check does not read the score")


def prepare_backend(road: Road, level: int, log=status) -> dict:
    """Imports dispatch.py and validates the desk contract, before the final starts. When it cannot
    be used, falls back to the kit's starter Desk so the week is still played (a poor score, not a
    lost week) and says so loudly."""
    ctx: dict[str, Any] = {"backend": "starter", "dispatch": None, "road": road}
    try:
        import dispatch  # noqa: F401  (lazy: dispatch.py is owned by the lead and may change)
        module = dispatch
        policy = module.Policy()
        desk = module.Dispatcher(DryWorld(), level, 0, policy, road)  # dry build: the constructor only stores state
        for name in ("handle", "read_results", "rejoin", "outbox"):
            if not hasattr(desk, name):
                raise AttributeError(f"Dispatcher has no {name}")
        ctx["backend"], ctx["dispatch"] = "dispatch", module
        log(f"dispatch backend: {getattr(module, '__file__', 'dispatch.py')} "
            f"(Dispatcher + Policy built at level {level})")
        return ctx
    except Exception:
        warning("dispatch.py could not be used; falling back to the kit's starter Desk.\n"
                "The week will still be played, but the score will be poor. Traceback:")
        warning(traceback.format_exc(limit=8))
        ctx["backend"], ctx["dispatch"] = "starter", None
        return ctx


def build_desk(world, level: int, now: int, ctx: dict, log=status):
    """One fresh desk for a snapshot at 'now': dispatch.Dispatcher when it works, else the kit Desk."""
    if ctx.get("backend") == "dispatch":
        module = ctx["dispatch"]
        try:
            return module.Dispatcher(world, level, now, module.Policy(), ctx["road"])
        except Exception:
            log("WARNING: dispatch.Dispatcher could not be built for this rebuild; using the kit's Desk.")
            warning(traceback.format_exc(limit=8))
            ctx["backend"] = "starter"
    return Desk(world, level, now)


def init_supply(desk, world, log=status) -> None:
    """Fills dispatch's driver-supply map from the roster. The supply model drives every quote, so a
    roster read that fails is retried here rather than playing on without it."""
    function = getattr(desk, "init_supply", None)
    if function is None:
        return
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            roster = world.roster()
            if not roster:
                raise SpreadError(0, "the driver list is empty; retrying before quoting without it")
            function(roster)
            return
        except (SpreadError, OSError) as error:
            last = error
            log(f"driver list read failed ({error}); retrying in {attempt + 1}s")
            time.sleep(attempt + 1)
    if last is not None:
        raise last


def fire_timers(desk, log=status) -> int:
    """Fires the dispatcher's held-order timers for the current minute; returns how many were due."""
    function = getattr(desk, "fire_timers", None)
    if function is None:
        return 0
    timers = getattr(desk, "timers", None)
    due = 0
    if isinstance(timers, list):
        due = sum(1 for item in timers if isinstance(item, tuple) and item and item[0] <= desk.now)
    try:
        function()
    except Exception:
        log("a held-order timer failed; clearing its list")
        warning(traceback.format_exc(limit=6))
        if isinstance(timers, list):
            timers.clear()
        return 0
    return due


def carry_learning(new_desk, old_desk, log=status) -> None:
    """Keeps the online competitor estimate (and the in-flight quote bookkeeping) across a rebuild:
    the same week, the same market, so the estimate learned before the restart is still true."""
    if old_desk is None or new_desk is old_desk:
        return
    try:
        if hasattr(new_desk, "log_comp") and hasattr(old_desk, "log_comp"):
            new_desk.log_comp = old_desk.log_comp
        quoted = getattr(old_desk, "quoted", None)
        target = getattr(new_desk, "quoted", None)
        if isinstance(quoted, dict) and isinstance(target, dict):
            target.update(quoted)
    except Exception:
        warning(traceback.format_exc(limit=4))


def restore_state(desk, snapshot: dict, history: list[dict], log=status) -> None:
    """Restores the dispatch-specific state that the base Desk.rejoin does not know about, guarded so
    a renamed field in a later dispatch.py cannot break the runner:
      * level-8 dealer trust, rebuilt from the ledger: a revenue line without a late refund is an
        order on time (+1 up to 25), a late one halves the dealer's count;
      * the set of settled orders, so a delivered event cannot count twice."""
    trust = getattr(desk, "trust", None)
    if not isinstance(trust, dict):
        return
    try:
        late: set[str] = set()
        revenue: list[str] = []
        for event in history:
            if event.get("kind") != "ledger":
                continue
            line = event.get("line") or {}
            order_id = line.get("orderId")
            if not order_id:
                continue
            if line.get("kind") in ("late_refund", "undelivered_penalty"):
                late.add(order_id)
            elif line.get("kind") == "revenue":
                revenue.append(order_id)
        counts: dict[str, int] = {}
        settled: set[str] = set()
        for order_id in revenue:
            order = desk.orders.get(order_id)
            if order is None:
                continue
            dealer = order["dealerId"]
            settled.add(order_id)
            counts[dealer] = counts.get(dealer, 0) // 2 if order_id in late else min(25, counts.get(dealer, 0) + 1)
        trust.clear()
        trust.update(counts)
        done = getattr(desk, "settled", None)
        if isinstance(done, set):
            done.update(settled)
        log(f"restored dealer trust for {len(counts)} dealers ({len(settled)} settled orders)")
    except Exception:
        warning("dealer-trust restore skipped:\n" + traceback.format_exc(limit=6))


def self_check(road: Road, level: int, ctx: dict, log=status) -> bool:
    """Pre-flight before the final starts: builds a Dispatcher and rejoins it with one synthetic price
    request, so a dispatch.py that cannot answer a request shows up at launch, not at the first request.
    A failure does not change the backend: it is reported loudly, and the runner still plays the week."""
    if ctx.get("backend") != "dispatch":
        log("pre-flight: skipped (the kit's Desk is the backend)")
        return True
    try:
        places = [place["id"] for place in road.map["places"]]
        pickup, delivery = places[0], places[1]
        request = {
            "id": "SELFCHECK-1", "dealerId": "D01", "batchId": None, "arrivedAt": 480, "respondBy": 495,
            "legs": [{"car": {"premium": False, "electric": False, "chargePercent": None},
                      "pickup": {"placeId": pickup, "privateAddress": False,
                                 "window": {"from": 480, "to": 1200}, "hours": None},
                      "delivery": {"placeId": delivery, "privateAddress": False,
                                   "window": {"from": 480, "to": 1200}, "hours": None}}],
            "options": [{"kind": "next_day", "deadline": 2640, "listPriceCents": 20000}],
            "referenceFeeCents": 9000, "note": None,
        }
        snapshot = {"worldId": "startup-self-check", "now": 480, "endsAt": 14280,
                    "openRequests": [request], "orders": [], "gigs": [], "driverPositions": [],
                    "score": {"marginCents": 0, "revenueCents": 0, "marginShare": 0.0, "wonOrders": 0,
                              "onTimeShare": 1.0, "undeliveredShare": 0.0, "ranked": False}}
        desk = build_desk(DryWorld(), level, 480, ctx, log)
        roster = [{"id": f"selfcheck-{index}", "homePlaceId": place, "rating": 4.9, "licence": "manual",
                   "completedGigs": 100, "drops": 0} for index, place in enumerate(places[:5])]
        supply = getattr(desk, "init_supply", None)
        if supply is not None:
            supply(roster)
        desk.rejoin(snapshot, [{"seq": 0, "at": 480, "kind": "price_request", "request": request}])
        quotes = [action for action in desk.outbox if action.get("type") == "quote"]
        if not quotes or quotes[0].get("requestId") != request["id"]:
            raise AssertionError(f"no quote action for a synthetic request (outbox: {desk.outbox[:2]})")
        prices = quotes[0].get("prices") or {}
        if not prices or not all(isinstance(value, int) and value > 0 for value in prices.values()):
            raise AssertionError(f"the quoted prices are not whole positive cents: {prices}")
        log(f"pre-flight: PASS, dispatch.py answered a synthetic price request: {prices}")
        return True
    except Exception:  # noqa: BLE001 - reported, never fatal
        warning("pre-flight: FAILED. dispatch.py could not quote a synthetic price request through "
                "Desk.rejoin; price requests would fall back to a decline quote. Check dispatch.py "
                "before the final. Traceback:")
        warning(traceback.format_exc(limit=10))
        return False


# ---------------------------------------------------------------- the live player

class Player:
    """Holds one Dispatcher in step with the live final: rebuild, answer events, fire timers, flush."""

    def __init__(self, world, level: int, road: Road, ctx: dict, log=status):
        self.world, self.level, self.road, self.ctx, self.log = world, level, road, ctx, log
        self.desk = None
        self.ends_at: Optional[int] = None
        self.last_seq = -1
        self.quoted: set[str] = set()   # request ids whose quote the server accepted
        self.sent = 0
        self.dropped = 0
        self.heals = 0
        self.errors = 0
        self.fallbacks = 0
        self.timers_fired = 0
        self.consecutive_errors = 0   # handler errors in a row; a rebuild recovers what the desk missed
        self.started = time.time()
        self.last_beat = 0.0

    # ---- reads
    def clock(self) -> Optional[int]:
        """GET /v1/final: the authoritative world minute, or None when it cannot be read right now."""
        try:
            return int(self.world.client.final()["now"])
        except (SpreadError, KeyError, TypeError, ValueError):
            return None

    def score(self) -> Optional[dict]:
        try:
            return self.world.score()
        except Exception:  # noqa: BLE001 - a heartbeat must never stop the week
            return None

    # ---- building the books (start and every rebuild)
    def _rejoin(self, snapshot: dict, history: list[dict]):
        """Builds a desk and rejoins it from the snapshot and history. A dispatch.py bug in rejoin
        falls back to the kit's starter Desk once, so a broken rebuild never costs the week."""
        for attempt in range(2):
            desk = build_desk(self.world, self.level, snapshot["now"], self.ctx, self.log)
            init_supply(desk, self.world, self.log)
            try:
                desk.rejoin(snapshot, history)
                return desk
            except Exception:
                if attempt == 0 and self.ctx.get("backend") == "dispatch":
                    self.log("WARNING: Desk.rejoin raised with dispatch.py; retrying the rebuild with the kit's Desk")
                    warning(traceback.format_exc(limit=8))
                    self.ctx["backend"] = "starter"
                    continue
                raise
        raise RuntimeError("no desk could be built")

    def _open(self) -> dict:
        snapshot = self.world.snapshot()
        self.ends_at = snapshot["endsAt"]
        old = self.desk
        if snapshot["now"] >= snapshot["endsAt"]:
            self.desk = None
            return snapshot
        history = self.world.events()
        if history:
            self.last_seq = max(self.last_seq, history[-1]["seq"])
        desk = self._rejoin(snapshot, history)
        restore_state(desk, snapshot, history, self.log)
        carry_learning(desk, old, self.log)
        self.desk = desk
        self.timers_fired += fire_timers(desk, self.log)
        self.flush()
        return snapshot

    def start(self, tries: int = 10) -> dict:
        """Rebuilds the books from the server, retrying while it is unreachable."""
        for attempt in range(tries):
            try:
                snapshot = self._open()
                self.log(f"books rebuilt: {label(snapshot['now'])} (minute {snapshot['now']}), "
                         f"{len(self.desk.orders) if self.desk else 0} orders, {self.sent} actions sent, "
                         f"seq {self.last_seq}, rebuilds {self.heals}")
                return snapshot
            except Unreachable as error:
                self.log(f"the server did not answer ({error}); retrying the rebuild in {min(30, 2 ** attempt)}s")
                time.sleep(min(30, 2 ** attempt))
            except SpreadError as error:
                if error.status in (401, 403, 404):
                    raise
                self.log(f"could not rebuild yet ({error}); retrying in {min(30, 2 ** attempt)}s")
                time.sleep(min(30, 2 ** attempt))
            except Exception:
                # A bug in dispatch.py must not cost the week: fall back to the kit's Desk and retry.
                self.errors += 1
                warning("the rebuild failed; traceback follows")
                warning(traceback.format_exc(limit=8))
                if self.ctx.get("backend") == "dispatch":
                    self.log("WARNING: falling back to the kit's starter Desk after a dispatch.py failure")
                    self.ctx["backend"] = "starter"
                time.sleep(min(30, 2 ** attempt))
        raise Unreachable(0, f"could not rebuild the books for world {self.world.id} in {tries} attempts")

    # ---- events
    def ensure_quote(self, event: dict) -> None:
        """Safety net: a price request that the dispatcher left unanswered gets a quote far above the
        list price, which always loses and costs nothing, instead of the goodwill of no quote at all."""
        request = event["request"]
        request_id = request["id"]
        if request_id in self.quoted:
            return
        outbox = self.desk.outbox
        if any(a.get("type") == "quote" and a.get("requestId") == request_id for a in outbox):
            return
        outbox.append({"type": "quote", "requestId": request_id,
                       "prices": {o["kind"]: int(o["listPriceCents"] * DECLINE_MULT) for o in request["options"]}})
        self.fallbacks += 1
        self.log(f"price request {request_id} was left unanswered: sent a decline quote instead")

    def on_event(self, event: dict) -> bool:
        """Handles one stream event. Returns False when the dispatcher raised on it."""
        desk = self.desk
        if desk is None:
            return True
        seq = event["seq"]
        if seq <= self.last_seq:  # a server restart can repeat events: drop by seq
            self.dropped += 1
            return True
        self.last_seq = seq
        at = event.get("at")
        if isinstance(at, int) and at > desk.now:
            desk.now = at
        handled = True
        try:
            desk.handle(event)
        except Exception:
            handled = False
            self.errors += 1
            self.consecutive_errors += 1
            if self.errors <= 20 or self.errors % 100 == 0:
                self.log(f"event {seq} ({event.get('kind')}) raised; the loop continues")
                warning(traceback.format_exc(limit=6))
        else:
            self.consecutive_errors = 0
        if event.get("kind") == "price_request":
            self.ensure_quote(event)
        self.timers_fired += fire_timers(desk, self.log)
        self.flush()
        return handled

    def tick(self, now: Optional[int]) -> None:
        """Clock tick from the stream's idle timeout: set the authoritative minute, fire timers, flush."""
        desk = self.desk
        if desk is None:
            return
        if isinstance(now, int) and now > desk.now:
            desk.now = now
        self.timers_fired += fire_timers(desk, self.log)
        self.flush()
        self.beat()

    # ---- sending
    def flush(self) -> None:
        """Sends every pending action in one world.act() call (the client splits at 50). One malformed
        action is dropped on its own instead of failing the whole batch."""
        desk = self.desk
        if desk is None:
            return
        actions = list(desk.outbox)
        desk.outbox.clear()
        if not actions:
            return
        try:
            results = self.world.act(actions)
        except SpreadError as error:
            if isinstance(error, Unreachable) or error.status != 400:
                raise  # a restart: let the loop rebuild (the snapshot shows what was applied)
            self.log(f"a batch of {len(actions)} actions was refused ({error}); sending them one by one")
            for action in actions:
                try:
                    results = self.world.act([action])
                except SpreadError as one:
                    if isinstance(one, Unreachable) or one.status != 400:
                        raise
                    self.errors += 1
                    self.log(f"dropped an invalid {action.get('type')} action: {one}")
                    continue
                self._sent([action], results)
            return
        self._sent(actions, results)

    def _sent(self, actions: list[dict], results: list) -> None:
        try:
            self.desk.read_results(actions, results)
        except Exception:
            self.errors += 1
            warning("read_results failed:\n" + traceback.format_exc(limit=6))
        self.sent += len(actions)
        for action in actions:
            if action.get("type") == "quote":
                self.quoted.add(action["requestId"])

    # ---- heartbeat
    def beat(self, force: bool = False) -> Optional[str]:
        """One line a minute, so a human can see the run is alive and doing well."""
        now = time.monotonic()
        if not force and now - self.last_beat < HEARTBEAT:
            return None
        self.last_beat = now
        desk = self.desk
        score = self.score()
        minute = desk.now if desk is not None else self.ends_at
        if score is not None:
            body = (f"margin €{score['marginCents'] / 100:,.2f}  {score['wonOrders']} orders  "
                    f"{score['onTimeShare']:.1%} on time  {score['undeliveredShare']:.1%} not delivered  "
                    f"{'ranked' if score['ranked'] else 'NOT YET RANKED'}")
        else:
            body = f"{len(desk.orders) if desk is not None else 0} orders (score unavailable)"
        line = (f"heartbeat {label(minute)} minute {minute}  seq {self.last_seq}  {body}  "
                f"sent {self.sent}  dropped {self.dropped}  rebuilds {self.heals}  "
                f"errors {self.errors}  timers {self.timers_fired}  up {(time.time() - self.started) / 60:.1f} min")
        self.log(line)
        return line


# ---------------------------------------------------------------- the loop

class _Rebuild(Exception):
    """Raised inside the loop when the books should be rebuilt from the server."""


def _follow(player: Player, log=status) -> None:
    """Follows the stream until the world ends. Any interruption reconnects with after=last seq and
    rebuilds the books from the server."""
    world = player.world
    while True:
        try:
            for event in world.stream(after=player.last_seq, idle=IDLE):
                if event is None:
                    player.tick(player.clock())
                else:
                    handled = player.on_event(event)
                    player.beat()
                    if not handled and player.consecutive_errors >= 3:
                        player.consecutive_errors = 0
                        raise _Rebuild("three events in a row raised")
                desk = player.desk
                if desk is None or desk.now >= (player.ends_at or 0):
                    log("the world has ended")
                    return
        except _Rebuild as reason:
            player.heals += 1
            log(f"{reason}; rebuilding the books to recover what the dispatcher missed")
            time.sleep(0.2)  # the connection is fine: rebuild fast so the next requests are still in time
            snapshot = player.start()
            player.beat(force=True)
            if snapshot["now"] >= snapshot["endsAt"]:
                return
            continue
        except (Unreachable, SpreadError) as error:
            if isinstance(error, SpreadError) and not isinstance(error, Unreachable) and error.status in (401, 403, 404):
                player.heals += 1
                log(f"the world is gone ({error})")
                raise
            player.heals += 1
            log(f"stream lost ({error}); rebuilding in {RETRY:.0f}s")
            time.sleep(RETRY)
            snapshot = player.start()
            player.beat(force=True)
            if snapshot["now"] >= snapshot["endsAt"]:
                return
            continue
        except Exception:  # a bug or a surprise in the loop: log it, rebuild, carry on
            player.heals += 1
            warning("the stream loop failed; traceback follows")
            warning(traceback.format_exc(limit=8))
            time.sleep(RETRY)
            snapshot = player.start()
            player.beat(force=True)
            if snapshot["now"] >= snapshot["endsAt"]:
                return
            continue
        # The stream returned without an error: the server closed it (a restart) or the world ended.
        player.heals += 1
        log(f"stream closed; checking the world and rebuilding in {RETRY:.0f}s")
        time.sleep(RETRY)
        snapshot = player.start()
        player.beat(force=True)
        if snapshot["now"] >= snapshot["endsAt"]:
            return


def play(world, level: int, road: Road, ctx: dict, log=status) -> Player:
    """Plays one final world from where it is to its end, healing from any interruption."""
    player = Player(world, level, road, ctx, log)
    player.beat(force=True)
    failures = 0
    while True:
        try:
            snapshot = player.start()
            if snapshot["now"] >= snapshot["endsAt"]:
                log("this world has already ended")
                return player
            log(f"playing final world {world.id} from {label(snapshot['now'])} to {label(snapshot['endsAt'])} "
                f"at level {level}; watching the stream from seq {player.last_seq}")
            player.beat(force=True)
            failures = 0
            _follow(player, log)
            return player
        except (Unreachable, SpreadError) as error:
            if isinstance(error, SpreadError) and not isinstance(error, Unreachable) and error.status in (401, 403, 404):
                raise
            failures += 1
            log(f"connection problem ({error}); rebuilding in {RETRY:.0f}s")
            time.sleep(RETRY)
        except Exception:
            failures += 1
            warning("the runner hit an unexpected error; traceback follows")
            warning(traceback.format_exc(limit=8))
            if failures > 50:
                raise
            time.sleep(RETRY)


# ---------------------------------------------------------------- the final's clock and start

def parse_start(value: Any) -> Optional[datetime]:
    """The final's startsAt as an aware datetime, or None when it does not parse."""
    try:
        text = str(value).replace("Z", "+00:00")
        when = datetime.fromisoformat(text)
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def wait_for_final(client: Client, level: int, log=status):
    """Waits politely for the final to be set: GET /v1/final until a world is set and its snapshot shows
    the week is not over, printing a status line every 10-30 s. Returns (world, level, snapshot)."""
    said_none = False
    while True:
        try:
            info = client.final()
        except Unreachable as error:
            log(f"no answer from the server yet ({error}); trying again in {POLL:.0f}s")
            time.sleep(POLL)
            continue
        except SpreadError as error:
            if error.status != 404:
                raise
            if not said_none:
                log("no final is set yet (GET /v1/final answers 404). The organisers set it before the start; "
                    f"waiting, status every {POLL:.0f}s.")
                said_none = True
            else:
                log("still no final set; waiting")
            time.sleep(POLL)
            continue
        world_id = info["worldId"]
        world = client.world(world_id)
        try:
            snapshot = world.snapshot()
        except Unreachable as error:
            log(f"the final world {world_id} did not answer yet ({error}); retrying in {POLL:.0f}s")
            time.sleep(POLL)
            continue
        except SpreadError as error:
            if error.status in (403, 404):
                log(f"the final world {world_id} is not readable yet ({error}); retrying in {POLL:.0f}s")
                time.sleep(POLL)
                continue
            raise
        if snapshot["now"] < snapshot["endsAt"]:
            level = discover_level(client, world_id, level, log)
            when = parse_start(info.get("startsAt"))
            if when is not None:
                seconds = (when - datetime.now(timezone.utc)).total_seconds()
                phase = f"starts in {seconds / 60:.1f} min" if seconds > 0 else "has started"
                log(f"final world {world_id} is set: startsAt {info.get('startsAt')} ({phase}); "
                    f"world now {label(snapshot['now'])} (minute {snapshot['now']}) of {label(snapshot['endsAt'])}; "
                    f"level {level}")
            else:
                log(f"final world {world_id} is set: world now {label(snapshot['now'])} "
                    f"(minute {snapshot['now']}) of {label(snapshot['endsAt'])}; level {level}")
            return world, level, snapshot
        log(f"the last final world {world_id} has ended (minute {snapshot['now']} of {snapshot['endsAt']}); "
            "waiting for the next final to be set")
        time.sleep(POLL)


def discover_level(client: Client, world_id: str, fallback: int, log=status) -> int:
    """The world's level from GET /v1/worlds; the final is level 8, so a failed read keeps the default."""
    try:
        for world in client.worlds():
            if world["worldId"] == world_id:
                return int(world["level"])
    except (SpreadError, KeyError, TypeError):
        pass
    return fallback


# ---------------------------------------------------------------- the end of the week

def finish(player: Optional[Player], world, log=status, stopped: bool = False) -> None:
    """Prints the final score, how the week was played, and whether it is ranked."""
    score = None
    if world is not None:
        try:
            score = world.score()
        except Exception as error:  # noqa: BLE001 - a stopped run must still print what it can
            log(f"could not read the final score: {error}")
    if score is not None:
        report("End", score)
        if score["ranked"]:
            log("Ranked: the delivery minimum is met.")
        elif score["wonOrders"] == 0:
            log("Not ranked: no order was won.")
        else:
            log(f"NOT RANKED: {score['onTimeShare']:.1%} of the orders on time, the minimum is 90%.")
    if player is not None:
        log(f"week played: {player.sent} actions sent, {player.dropped} repeated events dropped, "
            f"{player.heals} rebuilds, {player.errors} handler errors, {player.fallbacks} safety quotes, "
            f"{player.timers_fired} held-order timers fired, {(time.time() - player.started) / 60:.1f} min up")
    if stopped:
        log("Stopped by hand. The final world keeps running without your system; start "
            "'.venv/bin/python final.py' again and it rebuilds from the server and plays on.")


def finish_safely(player: Optional[Player], world, log=status, stopped: bool = False) -> None:
    """finish() that never raises: a stopped or failed run must still print its score and exit cleanly."""
    try:
        finish(player, world, log, stopped)
    except Exception:  # noqa: BLE001
        warning("could not print the final score:")
        warning(traceback.format_exc(limit=6))


# ---------------------------------------------------------------- main

def _stop(signum, frame):  # noqa: ARG001  (SIGTERM and SIGHUP stop the runner the way Ctrl-C does)
    raise KeyboardInterrupt


def install_stop_signals() -> None:
    """SIGTERM and SIGHUP stop the runner with a score line, like Ctrl-C. Installed again after
    dispatch.py is imported: dispatch.py installs its own SIGTERM and SIGHUP handlers at import time
    (they exit(1) silently), so the runner must claim them back after that import."""
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Play the live Spread final from its event stream.")
    parser.add_argument("--level", type=int, choices=range(1, 9), default=8,
                        help="the level of the final world (default 8; also read from the server)")
    args = parser.parse_args(argv)
    install_stop_signals()

    status(f"final runner: {time.strftime('%Y-%m-%d %H:%M:%S')} starting; "
           "no world is created, advanced or deleted")
    try:
        client = Client()
    except SpreadError as error:
        status(f"SPREAD_URL/SPREAD_KEY are not usable: {error}")
        return 2
    status(f"server {client.url}")
    try:
        world_map = client.map()  # free: it costs no token
    except SpreadError as error:
        status(f"could not read the map from the server ({error}); waiting for the server")
        while True:
            try:
                world_map = client.map()
                break
            except SpreadError as again:
                status(f"map still unavailable ({again}); trying again in {POLL:.0f}s")
                time.sleep(POLL)
    road = Road(world_map)
    status(f"map: {len(world_map['places'])} places, {len(road.roads)} roads")
    ctx = prepare_backend(road, args.level)
    install_stop_signals()  # dispatch.py claimed SIGTERM/SIGHUP when it was imported above: take them back
    if not self_check(road, args.level, ctx):
        warning("WARNING: the pre-flight check failed; the runner will still play the week, but if "
                "dispatch.py cannot quote, no order can be won. Fix dispatch.py and restart the runner "
                "before the start if you can.")

    world = None
    player = None
    try:
        world, level, snapshot = wait_for_final(client, args.level)
        player = play(world, level, road, ctx)
        finish_safely(player, world)
        return 0
    except KeyboardInterrupt:
        status("interrupted")
        if world is not None:
            finish_safely(player, world, stopped=True)
        else:
            status("Stopped before any final was played; nothing was created or deleted.")
        return 130
    except SystemExit:
        # Only reachable if a module imported later installed its own SIGTERM/SIGHUP handler; report the
        # score before letting the exit through, so a signal never ends the run without a final line.
        status("stopped by a signal handler installed by an imported module")
        if world is not None:
            finish_safely(player, world, stopped=True)
        return 130
    except SpreadError as error:
        if world is not None:
            finish_safely(player, world, stopped=True)
        status(f"spread: {error}")
        return 1
    except Exception:
        warning("the runner failed; traceback follows")
        warning(traceback.format_exc())
        if world is not None:
            finish_safely(player, world, stopped=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
