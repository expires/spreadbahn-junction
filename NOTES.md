# Spread level 8 - dispatcher v8 (branch deepseek-skdnfakldsfb)

Worktree: /Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb
Branch: deepseek-skdnfakldsfb (off main @ deb34f5). The parent worktree was never written to.

## 1. Where we are (shipped defaults on this branch)

Shipped configuration: post at the win, conservative two-car bundling, safe note reading,
fitted supply P(late) gate at 0.15, fitted booking-model opening fee, price multiplier 1.04.
Every number below is a deterministic live practice run (same seed = same world).

| panel | v7-lam50 (before) | v8 shipped (after) |
|---|---|---|
| six archived seeds ev-1..ev-6, mean margin | EUR11,275 | EUR13,457 (+19.3%) |
| mean on-time | 92.4% | 97.48% |
| worst on-time | 90.3% | 96.6% |
| ranked runs | 6/6 | 6/6 |
| six fresh seeds vb-1..vb-6 (never tuned on) | EUR11,418 (vb-1..4) | see below |

Per seed on the archived panel (v7 -> v8):
ev-1 13,365 -> 14,902 (91.0 -> 96.6%); ev-2 10,995 -> 14,956 (94.8 -> 97.8%);
ev-3 6,386 -> 8,640 (95.4 -> 97.8%); ev-4 14,308 -> 18,663 (90.3 -> 97.2%);
ev-5 13,789 -> 14,524 (94.5 -> 97.6%); ev-6 8,806 -> 9,058 (94.8 -> 97.8%).

So the punctuality half of the mission is met (>97% mean, worst 96.6%); the margin half is not
yet: EUR13.46k vs the EUR14k target (-3.9%), and the leader sits at EUR14.68k. On the same
leaderboard round our team's five runs averaged EUR11,834 at 95.6% on time; v8 is a ~14% margin
and +2 point on-time improvement over that.

## 2. What changed against v7, and what each change was worth

All changes are A/B tested on the same deterministic worlds (full table in NOTES-run.md).

1. Post a won order at once (hold_hours=0) instead of 36 h before the last pickup: +EUR1.7k-2.8k
   per week on ev-1/ev-4 and +1-2 points on-time. Biggest single change.
2. Two-car bundling, conservative (bundling.py): two single-car orders whose chain fits
   (delivery->next pickup <= 35 min, total planned drive <= 280 min, deadlines within 18 h) go
   in ONE gig: pickupA, deliveryA, pickupB, deliveryB, opening fee ~0.79*(refA+refB) instead of
   0.90*ref each. The archive's best bundle worlds run this at 0.65-0.79 x (refA+refB) (.gb
   w53d32d3: EUR15,653, 98.7% on time, 119 of 270 gigs carry two orders). Loose caps (75/540)
   are catastrophic on ev-4 (EUR11.5k, 75% on time): the tight caps are the working setting.
3. Read the dealer's note (notes_reader.py, 100% exact against the reveal truth on all 28,503
   won note orders) in safe mode: manual-licence / transfer-plate audiences, later-ready pickup
   floor, moved deadline in pricing.
4. Fitted P(late) gate (supply.py features refit in fit_late2.py -> data/fits/late2.json, OOF
   AUC 0.789, calibrated) used both in the quote margin and to decline options with P(late) >
   0.15. This is what lifted ev-4 from 90.3% to 97.2% on time.
5. Opening fee from the booking model: supply.booking.fee_for_target(target 0.85 within 8 h)
   replaces the flat 0.9 x ref, so easy lanes get cheaper and hard lanes get dearer. Worth
   ~+EUR150-220/week and up to +2 points on time (ev-2 96.4 -> 97.8%).
6. Price multiplier 1.04 on every quote: +EUR150-300/week at neutral on-time on the panel.
7. Off after A/B: direct offers (neutral), rescue repricing and failure fee inflation (lost more
   margin than they saved), fitted-menu pricing (worse than v7 pricing), qm-based request
   selection (inverted the order set), lane gate.

## 3. Archive: what the leaders do (and the road that is left)

210 finished level-8 worlds were harvested read-only (archive.py: four practice tokens each, no
world created/forked/deleted; 74,731 won orders; 27 seeds). The best worlds on our own seeds:

| world | won | on time | margin/week | fee/order | 2-order gigs |
|---|---|---|---|---|---|
| w53d32d3 | 373 | 98.7% | EUR15,653 | EUR53.4 | 119/270 |
| w892edb9 | 376 | 97.9% | EUR14,733 | EUR56.3 | 117/275 |
| wb6b6c12 | 398 | 98.0% | EUR15,390 | EUR56.5 | 80/329 |
| wbd07835 | 462 | 93.9% | EUR16,682 | EUR76.1 | 0/469 (selective) |
| v7 ev-4 | 517 | 90.3% | EUR14,308 | EUR81.6 | 0/522 |

What still separates the leaders from v8 is selection and price per order: bob-the-builders wins
287-316 orders/run at ~EUR45-50 net/order, while v8 wins ~390-430 at ~EUR32-35. Selection needs a
cost model better than the fitted expected fee (see section 9).

## 4. Fitted models on the branch (world-held-out validation)

- notes_reader.py - deterministic note reader, 100% exact on 28.5k won note orders.
- quote_model.py + data/fits/quotes.json - competitor level/lane/day Bayesian filter (price-war
  detector: 78% of war days detected after 8.5 quotes, 5.6% false alarm), dealer option-choice
  conditional logit calibrated (P(take|won) 0.470 vs 0.470), held-out P(win) AUC 0.889. Not used
  by the shipped pricing (use_quote_model=0) because the menu optimiser lost to v7 pricing in A/B.
- supply.py + data/fits/supply.json - booking hazard (bin CV AUC 0.879, fee elasticity fitted on
  2,357 random-fee gigs), expected driver fee (MAE 0.17 x ref), lateness model (shipped block is
  degenerate: its standardisation is missing; late2.json refits the same features).
- fit_late2.py + data/fits/late2.json - P(late), OOF AUC 0.789, deciles 0.279 predicted vs 0.297
  actual at the top. Used for the gate and the margin.
- bundling.py - chain feasibility (stop order, windows, 12 h drive limit) and the two-car fee.

## 5. Evaluator - built, NOT calibrated (honest limitation)

evaluate.py replays an archived week's request stream through the policy and scores it with
P(win), option choice, expected fee and P(late); no world is created. It does NOT reproduce
recorded margins: on the 50 worlds with a recorded run it correlates +0.14 with the recorded
margin and over-estimates by ~EUR16k/world. Reasons: (i) it cannot see driver capacity, drops,
chains or gig expiries - exactly v7's money leak; (ii) sampled-outcome feedback into the policy's
online estimator and the multi-option win correction do not match the real dealer. Treat it as a
debugging tool. Calibration path: replay each recorded world with its own recorded policy
version and quotes, then fit the residual correction.

## 6. Live-A/B method (how every number was produced)

The six archived seed strings ev-1..ev-6 reproduce the recorded worlds exactly (v7-lam50 on ev-1:
EUR13,365 / 455 / 91.0%, identical to runs/v7-lam50.json). quick_test.py plays one seed, waits
for a free practice slot, keeps the reveal and writes runs/<tag>.json. Two practice slots at a
time were explicitly approved by the user. ~60 deterministic A/B runs: NOTES-run.md.

## 7. Reproduce from a clean shell

    cd '/Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb'
    . ./env.sh                   # SPREAD_URL, SPREAD_KEY, SSL_CERT_FILE, PATH (secrets stay in .env)
    python archive.py            # one-off harvest if data/L8 is absent (~200 worlds, 4 tokens each)
    # the fitted models ship under data/fits/*.json; refit with:
    python late_model.py fit && python fit_late2.py
    # deterministic A/B on the six known seeds (2 live practice worlds at a time):
    python quick_test.py --tag check --seeds ev-1,ev-2 --parallel 2
    #    expected (shipped defaults): ev-1 EUR14,902 / 388 / 96.6%; ev-2 EUR14,956 / 418 / 97.8%
    # the live final (one command, no arguments; polls until the organisers set it):
    python final.py

## 8. The final runner

python final.py - the user launches it Sunday 07:55; it takes ~34 real minutes once the world
starts. It imports dispatch.py lazily, prints a pre-flight that builds the Dispatcher and answers
a synthetic price request (verified PASS on the live server), polls GET /v1/final every 10 s, then
plays from the world stream: every price request is answered in the same action batch (seconds,
inside the 15-minute window); outbox flushes on every event and on a 0.5 s idle tick using the
server clock; it rebuilds from snapshot+events (Desk.rejoin) after a server restart or network
loss, dropping repeated events by seq; never creates/forks/advances/deletes a world. Heartbeat
every 60 s (world time, orders, margin, on-time share, seq, rebuilds, errors). Ctrl-C/SIGTERM
prints the score and leaves the world running; relaunch rebuilds.

Offline tests: python test_final_rejoin.py - replays archived worlds through the Dispatcher and
re-joins from the end snapshot + history, checks the books match, that every request was answered
inside its window, that a repeated event is dropped and that a stream interruption is healed.
Latest run on the promoted dispatch.py: ALL CHECKS PASSED (the held-order-timer check is skipped
when Policy.hold_hours=0, which is the shipped setting).

## 9. What to test next (ordered by expected value / cost)

1. Selection to ~300 orders at a higher price per order. The leaderboard profile is ~EUR45-50
   net/order; v8 is ~EUR33. The EV/selection attempts so far used the fitted win model with the
   hand fee estimate and inverted the order set. The right cost model is
   supply.booking.expected_fee_cents plus a lane-level competitor factor, and the selection should
   be validated against realised margins before being trusted.
2. Lane-level competitor pricing (quote_model's 798 persistent lane factors, sd 0.139 log) inside
   the legacy single-comp pricing: worth ~+EUR0.65/quote (~EUR670/week) in model terms. The
   comp_from_qm draft inverted selection and was abandoned; a safer route is to scale comp by
   exp(lane_factor) only and keep v7's online level.
3. Direct offers as a rescue: tuned offers were worth +EUR460/+1 point on ev-4 but -EUR270/-0.2 on
   ev-2; they need a per-order expected-value trigger (use supply.booking.p_book on the gig).
4. Trust flywheel: the price already uses the dealer's trust (1.01^trust, fitted); avoiding a late
   order for a dealer near 25 counted orders is worth EUR1.5/quote at trust 25.
5. Evaluator calibration as in section 5, then use it to search selection/pricing offline.
6. Price-war day: the detector is good; winning the good lanes at the 20%-discounted competitor
   price is an untested margin play.
7. Raise margin at fixed punctuality: the measured frontier is ~EUR13.5k at 96.1-96.9% on time or
   ~EUR12.7-12.9k at 97.3-97.5%; closing both needs the selection work, not another constant.

## 10. Partner hand-off (worth porting by hand)

- Post at the win (hold_hours=0) - biggest single win, trivial to port.
- bundling.py + the tight caps (35/280/18); do not loosen them in one go.
- The failure-fee lesson: never raise fees on a drop/repost by default.
- notes_reader.py (100% exact) + the safe-mode wiring in post().
- fit_late2.py / SupplyLateModel: the shipped supply.json lateness block is broken; use late2.json.
- final.py + test_final_rejoin.py (both validated).
- quick_test.py + the deterministic ev-1..ev-6 A/B method.

## 11. Operational notes / risks

- Practice slots: the team key has 8; the partner sweeps and there were repeatedly 4-7 abandoned
  worlds that stopped advancing, which starved both agents. Three abandoned worlds our own runs
  left behind (identifiable by two-order gigs) were deleted after being proven stuck; four more
  that we do not own were left alone. This does not affect the final (separate world).
- The server had two multi-minute outage windows this session; quick_test.py retries worlds and
  final.py heals. The final runner must be started before 08:00 on Sunday.
- The final world is one secret seed: expect the same seed spread measured here (ev-3/ev-6 style
  hard seeds earn ~EUR8-9k even when everything works). The shipped config is ranked on all 6
  archived seeds and all 6 fresh seeds tested.
