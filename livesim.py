"""livesim: rehearse the final on a practice world, with the final's moving clock and the final's request limits.

One level-8 practice world. A clock thread in this process advances it in real time at the final's pace
(18 s per weekday hour 06-22, 6 s Saturday, 9 s last Monday, 3 s nights and Sunday), every ~0.5 s. The bot runs
in a separate process (`livesim.py bot`), plays `dispatch.run_final(policy, world=..., clock=...)` from the stream,
and talks to the server through a client that emulates the final's buckets (actions 20/s, at most 50; reads 50/s,
at most 50; 429 at once when over). The harness can kill -9 the bot mid-week and restart it, and the bot can
inject faults (stream drops, a silent dead stream, malformed actions, an unreachable server on act).

    python livesim.py run --stop 2880                         # Monday-Tuesday at the final's pace (~10.5 min)
    python livesim.py run --speed 4                           # the whole week, 4x faster (~8.6 min)
    python livesim.py run --stop 2880 --kill-at 900,1700      # kill -9 the bot at Mon 15:00 and Tue 04:20, restart it
    python livesim.py run --speed 4 --faults drop=45,stall=200,bad=60,down=150
    python livesim.py run --impl old_dispatch.py ...          # the same rehearsal for another version of dispatch.py
    python livesim.py report runs/livesim/<tag>               # the numbers again, from the logs
    python livesim.py cleanup                                 # delete practice worlds that livesim created and left

It never creates ranking runs and never touches the final. It deletes the one world it creates when it ends (also on
Ctrl-C), and records the ids it created in runs/livesim/worlds.txt, so `cleanup` deletes only those.
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "runs", "livesim")
REGISTRY = os.path.join(OUT, "worlds.txt")
DAY, HOUR = 1440, 60


def load_env() -> None:
    """SPREAD_URL and SPREAD_KEY from the environment, else from the nearest .env up the tree (export K=V lines)."""
    if os.environ.get("SPREAD_KEY"):
        return
    d = ROOT
    while True:
        path = os.path.join(d, ".env")
        if os.path.exists(path):
            for line in open(path):
                line = line.strip().removeprefix("export ").strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
            return
        if os.path.dirname(d) == d:
            return
        d = os.path.dirname(d)


load_env()
from spread import client as kit_client  # noqa: E402
from spread.client import Client, SpreadError, Unreachable  # noqa: E402


# ---------------- the final's pace ----------------

def secs_per_hour(minute: float) -> float:
    """Real seconds per world hour in the final (API.md, The final). Day 0 is Monday; day 7 is the last Monday."""
    day, hour = int(minute // DAY), int(minute % DAY // HOUR)
    if not 6 <= hour < 22:
        return 3.0
    return 18.0 if day <= 4 else 6.0 if day == 5 else 9.0 if day == 7 else 3.0


def minute_after(minute: float, secs: float) -> float:
    """The world minute reached `secs` real seconds after `minute`, at the final's pace."""
    while secs > 0:
        per_min = secs_per_hour(minute) / 60
        to_hour = (math.floor(minute / 60) + 1) * 60 - minute
        if secs >= to_hour * per_min:
            minute, secs = minute + to_hour, secs - to_hour * per_min
        else:
            minute, secs = minute + secs / per_min, 0.0
    return minute


def real_secs(a: float, b: float) -> float:
    """Real seconds the final takes from minute a to minute b."""
    total, m = 0.0, a
    while m < b:
        nxt = min(b, (math.floor(m / 60) + 1) * 60)
        total += (nxt - m) * secs_per_hour(m) / 60
        m = nxt
    return total


def fmt_min(m: float) -> str:
    m = int(m)
    return f"{['Mon','Tue','Wed','Thu','Fri','Sat','Sun','Mon2','Tue2','Wed2'][min(9, m // DAY)]} {m % DAY // 60:02d}:{m % 60:02d}"


# ---------------- the final's request limits, emulated ----------------

class TokenBucket:
    def __init__(self, rate: float, cap: float):
        self.rate, self.cap, self.tokens, self.at = rate, cap, cap, time.monotonic()
        self.lock = threading.Lock()

    def take(self, cost: int) -> tuple[bool, float]:
        """(taken, ms until the cost is available). The final never waits: over the limit, 429 and no tokens taken."""
        with self.lock:
            now = time.monotonic()
            self.tokens, self.at = min(self.cap, self.tokens + (now - self.at) * self.rate), now
            if self.tokens >= cost:
                self.tokens -= cost
                return True, 0.0
            return False, (cost - self.tokens) / self.rate * 1000

    def headers(self, name: str, retry_ms: float = 0.0) -> dict:
        with self.lock:
            tokens = min(self.cap, self.tokens + (time.monotonic() - self.at) * self.rate)
        reset = 0 if tokens >= self.cap else math.ceil((1 - (tokens % 1)) / self.rate * 1000)
        h = {"RateLimit-Policy": f'"{name}";q={int(self.rate)};w=1', "RateLimit-Limit": str(int(self.rate)),
             "RateLimit-Remaining": str(int(tokens)), "RateLimit-Reset": str(reset)}
        if retry_ms:
            h["Retry-After"] = str(math.ceil(retry_ms))
        return h


class SimClient(Client):
    """The kit client, with the final's buckets emulated in front of a practice world, and optional faults."""

    def __init__(self, rec, limits: bool = True, faults: dict | None = None):
        super().__init__()
        self.rec, self.limits, self.faults = rec, limits, faults or {}
        self.acts, self.reads = TokenBucket(20, 50), TokenBucket(50, 50)
        self.act_calls = 0

    def _send(self, method, path, payload, headers):
        base = path.split("?")[0]
        bucket = name = None
        cost = 1
        if base.startswith("/v1/worlds/") and base.endswith("/actions"):
            bucket, name, cost = self.acts, "final-actions", len(json.loads(payload))
            self.act_calls += 1
            down = self.faults.get("down")
            if down and self.act_calls % down == 0:  # the server restarts: the proxy answers 502, nothing applied
                self.rec(ev="fault", kind="down")
                raise Unreachable(0, "The server is restarting (HTTP 502) [injected]")
        elif method == "GET" and (base.startswith("/v1/worlds/") or base == "/v1/final"):
            bucket, name = self.reads, "final-reads"
        if bucket is not None and self.limits:
            ok, wait_ms = bucket.take(cost)
            if not ok:
                self.rec(ev="429", bucket=name, cost=cost, wait_ms=round(wait_ms))
                return 429, bucket.headers(name, wait_ms), b'{"error":"over the limit (emulated final bucket)"}'
        status, answer, data = super()._send(method, path, payload, headers)
        if bucket is not None and self.limits:
            answer = bucket.headers(name)  # the client tracks the emulated final bucket, not the practice one
        return status, answer, data


def faulty_websocket(rec, faults: dict, reads: TokenBucket):
    """Wraps the kit's WebSocket reader: `drop=S` breaks the connection every ~S seconds (the kit reconnects with
    after=); `stall=S` makes it go silent once after S seconds without closing, as a dead TCP connection does."""
    real = kit_client.websocket
    t0, state = time.monotonic(), {"stalled": False, "next_drop": None}

    def ws(url, path, key, idle):
        reads.take(1)  # a stream opened on a final world costs one read
        rec(ev="ws_open", path=path.split("?")[1] if "?" in path else "")
        if faults.get("drop") and state["next_drop"] is None:
            state["next_drop"] = time.monotonic() + random.uniform(0.5, 1.5) * faults["drop"]
        gen = real(url, path, key, idle)
        try:
            for msg in gen:
                now = time.monotonic()
                if faults.get("stall") and not state["stalled"] and now - t0 > faults["stall"]:
                    state["stalled"] = True
                    rec(ev="fault", kind="stall")
                    while True:  # never yields an event again, never fails: only a watchdog gets out
                        time.sleep(idle or 1.0)
                        yield None
                if state["next_drop"] and now > state["next_drop"]:
                    state["next_drop"] = now + random.uniform(0.5, 1.5) * faults["drop"]
                    rec(ev="fault", kind="drop")
                    raise ConnectionError("the stream broke [injected]")
                yield msg
        finally:
            gen.close()
    return ws


# ---------------- the bot process ----------------

def cmd_bot(a) -> None:
    log = open(os.path.join(a.out, "bot.jsonl"), "a", buffering=1)
    lock = threading.Lock()

    def rec(**kw):
        kw["t"], kw["pid"] = time.time(), os.getpid()
        with lock:
            log.write(json.dumps(kw) + "\n")

    rec(ev="start", impl=a.impl)
    spec = importlib.util.spec_from_file_location("dispatch", a.impl)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dispatch"] = mod
    spec.loader.exec_module(mod)
    faults = parse_faults(a.faults)
    client = SimClient(rec, limits=not a.no_limits, faults=faults)
    if faults.get("drop") or faults.get("stall"):
        kit_client.websocket = faulty_websocket(rec, faults, client.reads)
    else:
        real = kit_client.websocket

        def counted(url, path, key, idle):
            client.reads.take(1)
            rec(ev="ws_open", path=path.split("?")[1] if "?" in path else "")
            yield from real(url, path, key, idle)
        kit_client.websocket = counted
    world = client.world(a.world)

    def clock() -> int:
        t = time.time()
        now = world.snapshot()["now"]
        rec(ev="clock", now=now, lat=round(time.time() - t, 4))
        return now
    client.final = lambda: {"worldId": world.id, "now": clock()}  # what an older play_live reads on idle ticks

    # Instrumentation: every event handled, every batch of actions sent.
    orig_handle = mod.Dispatcher.handle

    def handle(self, event):
        r = {"ev": "event", "seq": event["seq"], "kind": event["kind"], "at": event["at"]}
        if event["kind"] == "price_request":
            r.update(rid=event["request"]["id"], respondBy=event["request"]["respondBy"])
        rec(**r)
        return orig_handle(self, event)
    mod.Dispatcher.handle = handle

    orig_act = world.act

    def act(actions):
        actions = list(actions)
        client.act_calls_seen = getattr(client, "act_calls_seen", 0) + 1
        sent = actions
        bad = faults.get("bad")
        if bad and client.act_calls_seen % bad == 0 and actions:  # a bug makes one action malformed
            i = random.randrange(len(actions))
            sent = list(actions)
            if actions[i]["type"] == "quote" and random.random() < 0.5:
                sent[i] = {**actions[i], "prices": {k: v + 0.5 for k, v in actions[i]["prices"].items()}}  # stopped by the client
            else:
                sent[i] = {**actions[i], "type": "nonsense"}  # refused by the server with 400
            rec(ev="fault", kind="bad", index=i, type=actions[i]["type"])
        t = time.time()
        try:
            res = orig_act(sent)
        except SpreadError as e:
            rec(ev="act_error", n=len(actions), lat=round(time.time() - t, 4), status=e.status, field=e.field, error=str(e)[:200])
            raise
        quotes = [(x["requestId"], r.get("ok") is True, None if r.get("ok") is True else r.get("reason"))
                  for x, r in zip(actions, res) if x["type"] == "quote"]
        kinds: dict = {}
        for x in actions:
            kinds[x["type"]] = kinds.get(x["type"], 0) + 1
        fails = [r.get("reason") for r in res if r.get("ok") is not True][:5]
        rec(ev="act", t0=t, n=len(actions), lat=round(time.time() - t, 4), kinds=kinds, quotes=quotes,
            nfail=sum(1 for r in res if r.get("ok") is not True), fails=fails)
        return res
    world.act = act

    burst = faults.get("burst")
    if burst:  # every ~S seconds, 120 filler actions land in the outbox at once (withdraws of no gig: refused, harmless; like a burst of posts)
        orig_fire = mod.Dispatcher.fire_timers
        next_burst = [time.monotonic() + burst]

        def fire_timers(self):
            if time.monotonic() >= next_burst[0]:
                next_burst[0] = time.monotonic() + burst
                rec(ev="fault", kind="burst")
                self.outbox += [{"type": "withdraw", "gigId": f"FILL{k}"} for k in range(120)]
            return orig_fire(self)
        mod.Dispatcher.fire_timers = fire_timers

    orig_rebuild = mod.rebuild

    def rebuild(*args, **kw):
        t = time.time()
        rec(ev="rebuild_start")
        out = orig_rebuild(*args, **kw)
        desk = out[0]
        rec(ev="rebuild_done", dur=round(time.time() - t, 3), now=desk.now, orders=len(desk.orders), outbox=len(desk.outbox),
            timers=len(getattr(desk, "timers", [])), posts_in_outbox=sum(1 for x in desk.outbox if x["type"] == "post"))
        return out
    mod.rebuild = rebuild

    policy = mod.Policy()
    import inspect
    if "world" in inspect.signature(mod.run_final).parameters:
        mod.run_final(policy, world=world, clock=clock)
    else:  # an older dispatch.py: its own play_live, under the retry loop that its run_final had
        road = mod.Road(client.map())
        while True:
            try:
                mod.play_live(world, policy, road)
                break
            except Unreachable as e:
                rec(ev="loop_error", error=str(e)[:200])
                time.sleep(5)
            except SpreadError as e:
                rec(ev="loop_error", error=str(e)[:200])
                time.sleep(2)
    rec(ev="exit")


def parse_faults(spec: str) -> dict:
    out = {}
    for kv in filter(None, (spec or "").split(",")):
        k, v = kv.split("=")
        out[k] = float(v) if k in ("drop", "stall", "burst") else int(v)
    return out


# ---------------- the harness ----------------

def register(world_id: str, add: bool) -> None:
    os.makedirs(OUT, exist_ok=True)
    ids = set(open(REGISTRY).read().split()) if os.path.exists(REGISTRY) else set()
    ids = ids | {world_id} if add else ids - {world_id}
    with open(REGISTRY, "w") as fh:
        fh.write("\n".join(sorted(ids)) + ("\n" if ids else ""))


def cmd_run(a) -> None:
    tag = a.tag or time.strftime("%m%d-%H%M%S")
    out = os.path.join(OUT, tag)
    os.makedirs(out, exist_ok=True)
    hlog = open(os.path.join(out, "harness.jsonl"), "a", buffering=1)

    def hrec(**kw):
        kw["t"] = time.time()
        hlog.write(json.dumps(kw) + "\n")

    client = Client()
    waited = time.time()
    while True:
        try:
            world = client.create(level=8, seed=a.seed)  # a plain practice world: never board=True
            break
        except SpreadError as e:  # the team's 8 slots are full: wait for one, never delete another's world
            if e.status != 409 or time.time() - waited > a.wait_slot:
                raise
            print(f"  all practice slots are taken; waiting ({e.status})", flush=True)
            time.sleep(20)
    register(world.id, True)
    print(f"livesim {tag}: practice world {world.id} (seed {a.seed}); logs in {out}", flush=True)
    bots: list[subprocess.Popen] = []
    stop_clock = threading.Event()

    def on_signal(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    try:
        snap = world.snapshot()
        ends = snap["endsAt"]
        start, stop = a.start, min(a.stop or ends, ends)
        if start > snap["now"]:
            world.advance(to=start)  # nobody at the desk: requests until then go to the competitor
        hrec(ev="config", world=world.id, seed=a.seed, start=start, stop=stop, ends=ends, speed=a.speed, kills=a.kill_at,
             faults=a.faults, impl=a.impl, limits=not a.no_limits)
        expected = real_secs(start, stop) / a.speed
        print(f"  {fmt_min(start)} -> {fmt_min(stop)} at {a.speed}x: about {expected / 60:.1f} min", flush=True)

        def spawn():
            cmd = [sys.executable, os.path.abspath(__file__), "bot", "--world", world.id, "--out", out, "--impl", a.impl]
            if a.faults:
                cmd += ["--faults", a.faults]
            if a.no_limits:
                cmd += ["--no-limits"]
            fh = open(os.path.join(out, f"bot-{len(bots)}.log"), "w")
            p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT)
            bots.append(p)
            hrec(ev="spawn", pid=p.pid)
            return p

        state = {"minute": float(start)}
        clog = open(os.path.join(out, "clock.jsonl"), "w", buffering=1)
        tick = min(0.5, 0.5 / a.speed)

        def run_clock():
            t0, cur = time.monotonic(), start
            while not stop_clock.is_set() and cur < stop:
                target = min(stop, int(minute_after(start, (time.monotonic() - t0) * a.speed)))
                if target > cur:
                    ts = time.time()
                    try:
                        world.advance(to=target)
                    except SpreadError as e:
                        print(f"clock: advance failed: {e}", file=sys.stderr, flush=True)
                        time.sleep(0.2)
                        continue
                    te = time.time()
                    clog.write(json.dumps([round(ts, 4), round(te, 4), cur, target]) + "\n")
                    cur = target
                    state["minute"] = cur
                time.sleep(max(0.0, tick - (time.monotonic() - t0) % tick))
            state["done"] = True

        th = threading.Thread(target=run_clock, daemon=True)
        bot = spawn()
        time.sleep(a.bot_head_start)
        th.start()
        kills = sorted(int(x) for x in a.kill_at.split(",")) if a.kill_at else []
        last_print = 0.0
        while not state.get("done"):
            if kills and state["minute"] >= kills[0]:
                kills.pop(0)
                bot.kill()  # kill -9
                bot.wait()
                hrec(ev="kill", pid=bot.pid, minute=state["minute"])
                print(f"  kill -9 bot {bot.pid} at {fmt_min(state['minute'])}", flush=True)
                time.sleep(a.restart_delay)
                bot = spawn()
                hrec(ev="restart", pid=bot.pid, minute=state["minute"])
            if bot.poll() is not None and not state.get("done"):
                hrec(ev="bot_exit", pid=bot.pid, code=bot.returncode, minute=state["minute"])
                print(f"  bot {bot.pid} exited with {bot.returncode} at {fmt_min(state['minute'])}; restarting", flush=True)
                bot = spawn()
            if time.time() - last_print > 60:
                last_print = time.time()
                print(f"  clock at {fmt_min(state['minute'])}", flush=True)
            time.sleep(0.1)
        hrec(ev="clock_done", minute=state["minute"])
        if bot.poll() is None:
            try:
                bot.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bot.terminate()
                bot.wait(timeout=10)
        hrec(ev="bot_stopped")
        score = world.score()
        json.dump(score, open(os.path.join(out, "score_at_stop.json"), "w"))
        if world.snapshot()["now"] < ends:
            world.advance(to=ends)  # to read the reveal; outcomes after `stop` are not the bot's
        json.dump(world.reveal(), open(os.path.join(out, "reveal.json"), "w"))
        print(report(out), flush=True)
    finally:
        stop_clock.set()
        for p in bots:
            if p.poll() is None:
                p.kill()
        try:
            world.delete()
            print(f"deleted practice world {world.id}", flush=True)
        except SpreadError as e:
            print(f"could not delete {world.id}: {e}; run `livesim.py cleanup`", file=sys.stderr, flush=True)
        else:
            register(world.id, False)


def cmd_cleanup(a) -> None:
    if not os.path.exists(REGISTRY):
        print("nothing to clean")
        return
    client = Client()
    for wid in open(REGISTRY).read().split():
        try:
            client.world(wid).delete()
            print(f"deleted {wid}")
        except SpreadError as e:
            print(f"{wid}: {e}")
            if e.status != 404:
                continue
        register(wid, False)


# ---------------- the report ----------------

def pct(xs: list, q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def report(out: str) -> str:
    rows = [json.loads(l) for l in open(os.path.join(out, "bot.jsonl"))]
    clk = [json.loads(l) for l in open(os.path.join(out, "clock.jsonl"))]
    har = [json.loads(l) for l in open(os.path.join(out, "harness.jsonl"))]
    cfg = next(h for h in har if h["ev"] == "config")
    reveal = json.load(open(os.path.join(out, "reveal.json"))) if os.path.exists(os.path.join(out, "reveal.json")) else None
    ends_t = [c[1] for c in clk]
    to_min = [c[3] for c in clk]
    stop_min = clk[-1][3] if clk else cfg["start"]

    def minute_at(t: float) -> float:
        i = bisect.bisect_right(ends_t, t) - 1
        return to_min[i] if i >= 0 else cfg["start"]

    def reached(minute: float):
        """(wall time the advance that reached `minute` started, ended)."""
        i = bisect.bisect_left(to_min, minute)
        return (clk[i][0], clk[i][1]) if i < len(clk) else (None, None)

    # Bot down windows: from a kill (or a start) to its desk's first rebuild_done.
    downs = []
    starts = sorted([h["t"] for h in har if h["ev"] == "spawn"])
    for s in starts:
        done = [r["t"] for r in rows if r["ev"] == "rebuild_done" and r["t"] >= s]
        downs.append((s, done[0] if done else float("inf")))
    kills = [h for h in har if h["ev"] == "kill"]
    restarts = []
    for k in kills:
        rb = [r for r in rows if r["ev"] == "rebuild_done" and r["t"] > k["t"]]
        st = [r for r in rows if r["ev"] == "start" and r["t"] > k["t"]]
        fq = [r for r in rows if r["ev"] == "act" and r["t"] > k["t"] and r["quotes"]]
        if rb:
            restarts.append({"at": fmt_min(k["minute"]), "python_start_s": round(st[0]["t"] - k["t"], 2) if st else None,
                             "rebuild_s": rb[0]["dur"], "recovered_s": round(rb[0]["t"] - k["t"], 2),
                             "first_quote_s": round(fq[0]["t"] - k["t"], 2) if fq else None,
                             "posts_in_rebuild_outbox": rb[0].get("posts_in_outbox"), "timers": rb[0].get("timers")})
    other_rebuilds = [r for r in rows if r["ev"] == "rebuild_done"]

    # Quotes: the first accepted quote of each request.
    sent: dict = {}
    refused: dict = {}
    for r in rows:
        if r["ev"] != "act":
            continue
        for rid, ok, reason in r["quotes"]:
            if rid.startswith("FILL"):  # burst filler
                continue
            if ok and rid not in sent:
                sent[rid] = r["t"]
            elif not ok:
                refused.setdefault(rid, reason)
    recv = {}
    for r in rows:
        if r["ev"] == "event" and r["kind"] == "price_request":
            recv.setdefault(r["rid"], r["t"])
    reqs = []
    if reveal:
        for q in reveal["requests"]:
            req = q["request"]
            reqs.append((req["id"], req["respondBy"] - 15, req["respondBy"], q["outcome"]))
    else:
        for r in rows:
            if r["ev"] == "event" and r["kind"] == "price_request":
                reqs.append((r["rid"], r["at"], r["respondBy"], None))
    in_run = [x for x in reqs if cfg["start"] <= x[1] and x[2] <= stop_min]
    head_min, head_s, react = [], [], []
    missed, missed_down = [], []
    for rid, at, rb, outcome in in_run:
        t_arr, t_rb = reached(at)[0] or 0.0, reached(rb)[1] or float("inf")
        down = any(t_arr <= e and t_rb >= s for s, e in downs)  # its 15 minutes overlap a time without a live desk
        if rid in sent:
            head_min.append(rb - minute_at(sent[rid]))
            t_rb = reached(rb)[0]
            if t_rb:
                head_s.append(t_rb - sent[rid])
            if rid in recv:
                react.append(sent[rid] - recv[rid])
        if (outcome == "unquoted") or (outcome is None and rid not in sent):
            (missed_down if down else missed).append((rid, fmt_min(at), refused.get(rid)))
    acts = [r for r in rows if r["ev"] == "act"]
    lat = [r["lat"] for r in acts]
    sizes = [r["n"] for r in acts]
    r429 = {}
    for r in rows:
        if r["ev"] == "429":
            r429[r["bucket"]] = r429.get(r["bucket"], 0) + 1
    # Stream lag: when the bot handled an event vs when the clock advance that published it started/ended.
    lag_lo, lag_hi = [], []
    for r in rows:
        if r["ev"] == "event":
            ts, te = reached(r["at"])
            if ts and r["t"] >= ts - 1:
                lag_hi.append(r["t"] - ts)
                lag_lo.append(r["t"] - te)
    adv = [c[1] - c[0] for c in clk]
    behind_min = []
    for r in rows:
        if r["ev"] == "event":
            behind_min.append(minute_at(r["t"]) - r["at"])
    clocks = [r["lat"] for r in rows if r["ev"] == "clock"]
    faults = {}
    for r in rows:
        if r["ev"] == "fault":
            faults[r["kind"]] = faults.get(r["kind"], 0) + 1
    ws_opens = sum(1 for r in rows if r["ev"] == "ws_open")
    act_errors = [r for r in rows if r["ev"] == "act_error"]
    stall_recovery = []
    for r in rows:
        if r["ev"] == "fault" and r["kind"] == "stall":
            nxt = [x["t"] for x in rows if x["ev"] == "ws_open" and x["t"] > r["t"] and x["pid"] == r["pid"]]
            stall_recovery.append(round(nxt[0] - r["t"], 1) if nxt else None)
    score = json.load(open(os.path.join(out, "score_at_stop.json"))) if os.path.exists(os.path.join(out, "score_at_stop.json")) else {}

    f = lambda xs, k=1: f"p50 {pct(xs, .5):.{k}f}  p95 {pct(xs, .95):.{k}f}  max {max(xs) if xs else float('nan'):.{k}f}"  # noqa: E731
    lines = [
        f"== livesim {os.path.basename(out)}: world {cfg['world']}  {fmt_min(cfg['start'])} -> {fmt_min(stop_min)}  speed {cfg['speed']}x  "
        f"impl {os.path.basename(cfg['impl'])}  limits {'final' if cfg['limits'] else 'none'}  faults {cfg['faults'] or '-'}  kills {cfg['kills'] or '-'}",
        f"requests in run {len(in_run)}  quoted in time {sum(1 for x in in_run if x[0] in sent)}  "
        f"MISSED {len(missed)} while up, {len(missed_down)} while the bot was down/rebuilding  "
        f"(late quotes dropped/refused {sum(1 for v in refused.values() if v)})",
        f"quote headroom before respondBy, world min: {f(head_min)}  min {min(head_min) if head_min else float('nan'):.1f}",
        f"quote headroom before respondBy, real s:    {f(head_s, 2)}  min {min(head_s) if head_s else float('nan'):.2f}",
        f"reaction (event received -> quote acked), s: {f(react, 3)}",
        f"act calls {len(acts)}  actions {sum(sizes)}  max batch {max(sizes) if sizes else 0}  latency s: {f(lat, 3)}",
        f"429 answers: {r429 or 0}   act errors: {len(act_errors)} {sorted(set(e['status'] for e in act_errors))}",
        f"stream lag s (handled - advance start): {f(lag_hi, 2)}   (- advance end): {f(lag_lo, 2)}",
        f"stream behind the clock, world min: {f(behind_min)}   advance latency s: {f(adv, 3)}",
        f"clock reads {len(clocks)} latency s: {f(clocks, 3)}   stream opens {ws_opens}   faults {faults or '-'}   stall recovery s {stall_recovery or '-'}",
        f"rebuilds {len(other_rebuilds)}: " + ", ".join(f"{r['dur']}s@{fmt_min(r['now'])}(outbox {r['outbox']}, posts {r.get('posts_in_outbox')})" for r in other_rebuilds),
    ]
    for r in restarts:
        lines.append(f"restart at {r['at']}: python up {r['python_start_s']} s, rebuild {r['rebuild_s']} s, desk live {r['recovered_s']} s after kill, "
                     f"first quote {r['first_quote_s']} s after kill; posts in rebuild outbox {r['posts_in_rebuild_outbox']}, timers {r['timers']}")
    if score:
        lines.append(f"score at stop: margin €{score['marginCents']/100:,.0f}  {score['wonOrders']} orders  {score['onTimeShare']:.1%} on time")
    if missed:
        lines.append("missed while up: " + ", ".join(f"{rid}@{m} ({why})" for rid, m, why in missed[:12]))
    summary = {"missed_up": len(missed), "missed_down": len(missed_down), "requests": len(in_run), "r429": r429,
               "act_lat_p50": pct(lat, .5), "act_lat_p95": pct(lat, .95), "react_p95": pct(react, .95),
               "headroom_min_s": min(head_s) if head_s else None, "restarts": restarts, "lag_p95": pct(lag_hi, .95)}
    json.dump(summary, open(os.path.join(out, "summary.json"), "w"), indent=1)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="one rehearsal on a new practice world")
    r.add_argument("--seed", default="livesim-1")
    r.add_argument("--start", type=int, default=0, help="world minute to start the clock (fast-forwarded to, unattended)")
    r.add_argument("--stop", type=int, default=0, help="world minute to stop (default: the end of the week)")
    r.add_argument("--speed", type=float, default=1.0, help="x the final's pace")
    r.add_argument("--kill-at", default="", help="comma-separated world minutes: kill -9 the bot there and restart it")
    r.add_argument("--restart-delay", type=float, default=0.0)
    r.add_argument("--faults", default="", help="drop=S,stall=S,bad=N,down=N,burst=S (see faulty_websocket, SimClient, cmd_bot)")
    r.add_argument("--impl", default=os.path.join(ROOT, "dispatch.py"), help="the dispatch.py to rehearse")
    r.add_argument("--no-limits", action="store_true", help="do not emulate the final's buckets")
    r.add_argument("--bot-head-start", type=float, default=3.0, help="seconds the bot gets to rebuild before the clock starts")
    r.add_argument("--wait-slot", type=float, default=1800, help="seconds to wait for a free practice slot")
    r.add_argument("--tag", default="")
    b = sub.add_parser("bot", help="(internal) the bot process")
    b.add_argument("--world", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--impl", required=True)
    b.add_argument("--faults", default="")
    b.add_argument("--no-limits", action="store_true")
    p = sub.add_parser("report", help="the numbers of a finished rehearsal, from its logs")
    p.add_argument("out")
    sub.add_parser("cleanup", help="delete the practice worlds that livesim created and did not delete")
    a = ap.parse_args()
    if a.cmd == "run":
        cmd_run(a)
    elif a.cmd == "bot":
        cmd_bot(a)
    elif a.cmd == "report":
        print(report(a.out))
    else:
        cmd_cleanup(a)


if __name__ == "__main__":
    main()
