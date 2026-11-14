# Final-week runbook (Sunday, level 8)

Everything below is on branch deepseek-skdnfakldsfb (pushed to origin, latest commit in git log).

## What runs

```sh
cd "/Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb"
. ./env.sh                       # loads .env credentials (never committed) and the venv
python final.py                  # THE ENTRY POINT - one command, no arguments
```

- final.py polls GET /v1/final every 10 s until the organisers set the final (07:30), then plays
  the live stream. It never creates, forks, advances or deletes a world.
- It rebuilds from snapshot+events after any restart or network loss (drop repeated events by seq);
  restart it at once if it stops - it rejoins the same week.
- A heartbeat is printed every 60 s: world time, orders, margin, on-time share, seq, rebuilds,
  errors. Leave it running until the week ends (~34 minutes after 08:00).
- Ctrl-C prints the score and leaves the world running.

## Pre-flight (do it by 07:45)

```sh
. ./env.sh && python -c "import dispatch; p=dispatch.Policy(); print(p.gate_pl, p.select_net, p.comp_from_qm)"
# expect: 0.3 0.0 1.0   (margin-first v9 shipped defaults)
python test_final_rejoin.py --worlds 1 --skip-loop     # expect ALL CHECKS PASSED
python final.py                                         # expect: pre-flight PASS, then "no final set; waiting"
```

If the final is already set when you start final.py, it begins immediately; do not restart it while
it is playing unless it has stopped.

## Configuration choice (already made, recorded here)

Shipped defaults = margin-first v9: post-at-win, conservative bundling, safe notes, lane-aware
competitor threshold (comp_from_qm=1), fitted P(late) gate 0.30, selection bar off.
- Measured on 16 deterministic practice worlds: EUR14,488/week mean, 95.45% on time, 16/16 ranked
  (worst world vb-2: 90.3%).
- Safe alternative if you prefer a lower variance week: set gate_pl=0.15, select_net=5.0
  (ev EUR14,524 @ 97.2%, vb 13,072 @ 96.5%, vc 12,519 @ 97.0%); costs ~EUR300-500/week.
- Change it only by editing the Policy defaults in dispatch.py and re-running the pre-flight
  checks above; the final runner reads the same file.

## If something goes wrong

- Server unreachable / stream closed: final.py retries and rebuilds by itself; waiting 20 s is
  normal. Only restart the process if it has actually stopped.
- Wrong/frozen clock: the runner reads the authoritative clock from every advance and event.
- No final set by 08:00: keep it running; the organisers set it at 07:30 but a late set is handled.
- Presenting afterwards: PRESENTATION.md has the slide outline, NOTES.md the full evidence,
  NOTES-decisions.md the decision log with each idea's measured effect and status.
