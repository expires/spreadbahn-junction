# Final runner (final.py) + rejoin tests

## Files

- `final.py` — the live-final runner. One command, no arguments:
  `./.venv/bin/python final.py` (optional `--level 8`).
- `test_final_rejoin.py` — offline tests: full-history replay and Desk.rejoin on 5 diverse archived
  level-8 worlds, plus a mocked stream-loop smoke test with an injected server restart.
- `reports/final.md` — this file.

`final.py` imports `dispatch.py` lazily (inside a function), never imports it at module import time,
and never creates, forks, advances or deletes a world. It only reads snapshot/events/roster/score/
`/v1/final` and POSTs action batches, which is all a final world allows.

## How to launch

    ./.venv/bin/python final.py        # start now: waits for the final, plays it, prints the score, exits

It prints a banner (server, map, dispatch.py path), a **pre-flight** check (builds the Dispatcher and
answers one synthetic price request through `Desk.rejoin`), then polls `GET /v1/final` every 10 s
("no final is set yet" -> "final world set: startsAt ..."). At the end it prints `End` (margin,
revenue, orders, on-time share), `Ranked`/`NOT RANKED`, and how the week was played.

Ctrl-C / SIGTERM / SIGHUP prints the score so far and a message; the world keeps running, and
relaunching the same command rebuilds from the server. Server restart / network loss: sleeps 5 s,
re-reads snapshot + events + roster, `Desk.rejoin`, reconnects the stream with `after=<last seq>`,
drops repeated events by seq. Heartbeat every 60 s: world time, seq, orders, margin, on-time share,
actions sent, rebuilds, errors, timers.

## What is tested and its results (all PASS, `./.venv/bin/python test_final_rejoin.py`, 4.2 s)

5 diverse worlds from the 222 archived at test time (best margin EUR 16,682 ... worst -72,613; 462-949
won orders; 1-11 cancellations; 18k-179k events):

- replay of the **whole event history** per world, `desk.now` updated per event, timers fired and the
  outbox sent: 0 handler errors, every price request (about 1,036/world) quoted before its
  `respondBy`, held-order timers fired, no undelivered order left without a gig, no timer left at the
  end;
- **rejoin** from a fresh Dispatcher + the archived end snapshot + the same history: same order set as
  the replay, `deliveredAt` exactly the snapshot, every live gig carried, every undelivered order
  carried or posted, requests/deadline warnings restored, level-8 **dealer trust rebuilt from the
  ledger** (37-40 dealers), settled set restored, and the rebuilt desk still handles a later event
  (replay 0.3-1.1 s, rejoin 0.01 s per world);
- mocked **stream loop** on an 18,225-event world with one injected `Unreachable` at 60% and a repeated
  event after the reconnect: all 1,036 requests answered inside the 15-minute window, timers fired,
  repeated seq dropped, books rebuilt, heartbeat printed, 0 handler errors, reaches the end (0.19 s);
- runner safety nets: pre-flight quote PASS; 404 polling until a final is set; an ended final is not
  played; an unanswered price request gets a decline quote; a malformed action is dropped alone; a
  repeated event is dropped; a dispatch.py rejoin bug falls back to the kit Desk.

Also smoke-tested against the live server's waiting path: banner + pre-flight PASS, `GET /v1/final`
404 polling, SIGINT/SIGTERM/SIGHUP exit with a message, no world created.

## Remaining risks (for the lead)

1. The runner has not yet been run against a *live* final (none set); the WebSocket transport is the
   kit's `world.stream`, the same one the starter's `--final` uses, and the loop is tested on a mocked
   stream built from archived events.
2. The desk contract from the task is what the runner uses: `Dispatcher(world, level, now, policy,
   road)`, `init_supply(roster)`, `handle`, `outbox`, `read_results`, `rejoin`, `fire_timers`, `now`.
   Optional fields restored if present: `timers`, `trust`, `settled`, `quoted`, `log_comp`. If v8
   renames a required one, launch is caught by the pre-flight / a kit-Desk fallback (score hit, week
   still played). **Run `python final.py` once after finalising v8 and confirm `pre-flight: PASS` in
   the first lines.**
3. After a restart the base `Desk.rejoin` posts orders whose holding timer had not fired yet — those
   orders are posted earlier than the hold policy would. Only affects a restart, not a clean run.
4. `dispatch.py` installs its own SIGTERM/SIGHUP handlers at import (silent `exit(1)`); `final.py`
   re-installs its own after the import, so Ctrl-C/SIGTERM are graceful. Tested.
5. Launch before 08:00: a late start only answers requests still inside their 15-minute window; the
   rest already went to the competitor.
