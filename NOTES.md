# Spread level 8 - dispatcher v8 (branch deepseek-skdnfakldsfb)

Worktree: /Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb
Branch: deepseek-skdnfakldsfb (off main @ deb34f5). The parent worktree was never written to.

## 1. Where we are (shipped defaults on this branch)

Shipped configuration: post a won order at once; conservative two-car bundling (link <= 35 min,
total drive <= 280 min, deadlines within 18 h); safe note reading; fitted supply P(late) gate at
0.20; v7 hand fee ladders (the fitted booking-model fee is available but did not generalise).
Every number is a deterministic live practice run (same seed = same world).

| panel | v7-lam50 (before) | v8 shipped (after) |
|---|---|---|
| six archived seeds ev-1..6, mean margin | EUR11,275 | EUR13,695 (shipped defaults) |
| six unseen seeds vb-1..6, mean margin | EUR11,418 (4 seeds) | EUR14,080 |
| four more unseen seeds vc-1..4, mean margin | n/a | EUR12,467 |
| on-time, ev panel | 92.4% | 96.4% (worst 95.5%) |
| on-time, vb panel | 91.3% (4 seeds) | 95.2% |
| on-time, vc panel | n/a | 95.2% |
| worst on-time across all tested worlds | 82.1% | 89.1% (one hard seed, vb-2) |
| ranked | 6/6 | 15/16 tested worlds (the 89.1% one is vb-2) |

Per-seed on the archived panel (v7 -> shipped v8):
ev-1 13,365 -> 15,784 (91.0 -> 96.7%); ev-2 10,995 -> 13,537 (94.8 -> 96.1%);
ev-3 6,386 -> 9,328 (95.4 -> 97.1%); ev-4 14,308 -> 19,334 (90.3 -> 95.5%);
ev-5 13,789 -> 14,801 (94.5 -> 96.8%); ev-6 8,806 -> 9,383 (94.8 -> 96.2%).
Six unseen seeds vb-1..6: EUR14,080 mean at 95.2% on time (worst 89.1%).
Four further unseen seeds vc-1..4: EUR12,467 mean at 95.2% (worst 92.7%).
Across all 16 test worlds: EUR13,532 mean, 95.7% mean on time, 15/16 ranked.

Honest verdict: this does NOT yet meet the joint mission (97% on time AND EUR14k/week). The
margin is ~EUR14.0k on the unseen panel and ~EUR13.6k on the archived panel; the on-time rate is
~95%, not 97%. The variant frontier is explicit in NOTES-run.md: ~EUR13.5k at 96.1-96.9%, or
~EUR12.7-12.9k at 97.3-97.5%, or EUR14.3k at 94.5% with a worse ranking tail. The leaders
(EUR14.7k at 98.0%) do both because they win ~30% fewer orders at ~50% higher margin per order;
the measured missing piece is the selection/cost model, not another constant.

## 2. What changed against v7 (each A/B tested on identical deterministic worlds)

1. Post a won order at once (hold_hours=0) instead of 36 h before the last pickup: +EUR1.7k-2.8k
   per week on ev-1/ev-4 and +1-2 points of on-time. The biggest single change.
2. Two-car bundling, conservative (bundling.py): two single-car orders whose chain fits are posted
   as ONE gig (pickupA, deliveryA, pickupB, deliveryB) at opening fee ~0.79*(refA+refB) instead of
   0.90*ref each. The archive's best bundle worlds (w53d32d3: EUR15,653, 98.7% on time) run 66% of
   their cars this way. Loose caps (75 min link / 540 min drive) are catastrophic on some seeds
   (ev-4: EUR11.5k, 75% on time, 24-38% of bundles expire); the tight caps are the working setting.
   Worth ~+EUR450-650/week on the panels that accept it.
3. Read the dealer's note (notes_reader.py: 100% exact against the reveal truth on all 28,503 won
   note orders) in safe mode: manual-licence / transfer-plate audiences, later-ready pickup floor,
   moved deadline in pricing.
4. Fitted P(late) (supply.py features + data/fits/late2.json, OOF AUC 0.789, calibrated) in the
   quote margin and as a gate: decline options with P(late) > 0.20. This lifted the hard seeds and
   cut the late tail (ev-1 91.0 -> 96.7%, ev-4 90.3 -> 95.5%).
5. Off after A/B: price multiplier on top of the gate (seed-dependent, chaotic), the fitted
   booking-model fee (hurt generalisation), direct offers (neutral), rescue repricing and
   failure fee inflation (lost more margin than they saved), fitted-menu pricing and qm-based
   request selection (worse / inverted). Details and dead-end numbers: NOTES-run.md.

## 3. Archive: what the leaders do (the road that is left)

210 finished level-8 worlds were harvested read-only (archive.py: four practice tokens each, no
world created/forked/deleted; 74,731 won orders; 27 seeds). The best worlds on our own seeds:

| world | won | on time | margin/week | fee/order | 2-order gigs |
|---|---|---|---|---|---|
| w53d32d3 | 373 | 98.7% | EUR15,653 | EUR53.4 | 119/270 |
| w892edb9 | 376 | 97.9% | EUR14,733 | EUR56.3 | 117/275 |
| wb6b6c12 | 398 | 98.0% | EUR15,390 | EUR56.5 | 80/329 |
| wbd07835 | 462 | 93.9% | EUR16,682 | EUR76.1 | 0/469 (selective) |
| v7 ev-4 | 517 | 90.3% | EUR14,308 | EUR81.6 | 0/522 |

What still separates the leaders: selection and price per order. bob-the-builders wins 287-316
orders/run at ~EUR45-50 net/order; v8 wins ~390-430 at ~EUR33-35. The measured path to close it
is a cost/selection model good enough to decline the third of requests that lose money to a
shortage, then price the survivors higher (see section 9).

## 4. Fitted models on the branch (world-held-out)

- notes_reader.py - deterministic note reader, 100% exact on 28.5k won note orders.
- quote_model.py + data/fits/quotes.json - competitor level/lane/day Bayesian filter (price-war
  detector: 78% of war days after 8.5 quotes, 5.6% false alarm), calibrated option choice, held-out
  P(win) AUC 0.889. Not used by the shipped pricing (use_quote_model=0).
- supply.py + data/fits/supply.json - booking hazard (bin CV AUC 0.879, fee elasticity on 2,357
  random-fee gigs), expected driver fee (MAE 0.17 x ref), lateness model (shipped block is
  degenerate - its standardisation is missing - late2.json refits the same features).
- fit_late2.py + data/fits/late2.json - P(late): OOF AUC 0.789, deciles 0.279 predicted vs 0.297
  actual at the top. Used for the gate and in the quote margin.
- bundling.py - chain feasibility (stop order, windows, 12 h drive limit) and the two-car fee.

## 5. Evaluator - built, NOT calibrated (honest limitation)

evaluate.py replays an archived week's request stream through the policy and scores it with
P(win), option choice, expected fee and P(late); no world is created. It does NOT reproduce
recorded margins: on the 50 worlds with a recorded run it correlates +0.14 with the recorded
margin and over-estimates by ~EUR16k/world. Reasons: (i) it cannot see driver capacity, drops,
chains or gig expiries - exactly v7's money leak; (ii) sampled-outcome feedback into the policy's
online estimator and the multi-option win correction do not match the real dealer. Treat it as a
debugging tool. Calibration path: replay each recorded world with its own recorded policy version
and quotes, then fit the residual correction.

## 6. Live-A/B method (how every number was produced)

The six archived seed strings ev-1..ev-6 reproduce the recorded worlds exactly (v7-lam50 on ev-1:
EUR13,365 / 455 / 91.0%, identical to runs/v7-lam50.json). quick_test.py plays one seed, waits for
a free practice slot, keeps the reveal and writes runs/<tag>.json. Two practice slots at a time
were explicitly approved by the user. ~70 deterministic A/B runs: NOTES-run.md.

## 7. Reproduce from a clean shell

    cd '/Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb'
    . ./env.sh                   # SPREAD_URL, SPREAD_KEY, SSL_CERT_FILE, PATH (secrets stay in .env)
    python archive.py            # one-off harvest if data/L8 is absent (~200 worlds, 4 tokens each)
    # fitted models ship under data/fits/*.json; refit with:
    python late_model.py fit && python fit_late2.py
    # deterministic A/B on the known seeds (2 live practice worlds at a time):
    python quick_test.py --tag check --seeds ev-1,ev-2 --parallel 2
    #    shipped defaults: ev-1 15,784/450/96.7% (gate 0.20 variant measured on ev-1)
    # the live final (one command, no arguments; polls until the organisers set it):
    python final.py

## 8. The final runner

python final.py - one command, no arguments; the user launches it Sunday 07:55; it takes ~34 real
minutes once the world starts. It imports dispatch.py lazily, prints a pre-flight that builds the
Dispatcher and answers a synthetic price request (verified PASS on the live server), polls
GET /v1/final every 10 s, then plays from the world stream: every price request is answered in the
same action batch (seconds, inside the 15-minute window); outbox flushes on every event and on a
0.5 s idle tick using the server clock; it rebuilds from snapshot+events (Desk.rejoin) after a
server restart or network loss, dropping repeated events by seq; never creates/forks/advances/
deletes a world. Heartbeat every 60 s (world time, orders, margin, on-time share, seq, rebuilds,
errors). Ctrl-C/SIGTERM prints the score and leaves the world running; relaunch rebuilds.

Offline tests: python test_final_rejoin.py - replays archived worlds through the Dispatcher and
re-joins from the end snapshot + history, checking the books match, every request was answered
inside its window, repeats are dropped and an interruption is healed. Latest run on the promoted
dispatch.py: ALL CHECKS PASSED (held-timer check skipped because Policy.hold_hours=0).

## 9. What to test next (ordered by expected value / cost)

1. Selection to ~300 orders at a higher price per order: the leaders' profile. The EV/selection
   drafts with the fitted win model plus the hand fee estimate inverted the order set; the right
   cost model is supply.booking.expected_fee_cents plus a lane-level competitor factor, validated
   against realised margins before it is trusted.
2. Lane-level competitor pricing (quote_model's 798 persistent lane factors, sd 0.139 log) inside
   legacy single-comp pricing: worth ~+EUR0.65/quote in model terms. The comp_from_qm draft
   inverted selection and was abandoned; scale comp by exp(lane) only, keep v7's online level.
3. Direct offers as a rescue with an EV trigger (tuned offers were +EUR460/+1 point on ev-4,
   -EUR270/-0.2 on ev-2).
4. Trust flywheel: price already uses dealer trust (1.01^trust); avoiding a late order near a
   dealer's 25 counted orders is worth ~EUR1.5/quote at trust 25.
5. Evaluator calibration (section 5), then offline search over selection/pricing.
6. Price-war day: the detector is good; winning the good lanes at the discounted competitor price
   is an untested margin play.
7. Close the joint target: the measured frontier is ~EUR13.5k at 96.1-96.9% or ~EUR12.7-12.9k at
   97.3-97.5%; the leaders get both via selection, not constants.

## 10. Partner hand-off (worth porting by hand)

- Post at the win (hold_hours=0) - the biggest single win, trivial to port.
- bundling.py + the tight caps (35/280/18); do not loosen them in one go.
- The failure-fee lesson: never raise fees on a drop/repost by default.
- notes_reader.py (100% exact) + the safe-mode wiring in post().
- fit_late2.py / SupplyLateModel: the shipped supply.json lateness block is broken; use late2.json.
- final.py + test_final_rejoin.py (both validated).
- quick_test.py + the deterministic ev-1..ev-6 A/B method.

## 11. Operational notes / risks

- Practice slots: the team key has 8; the partner sweeps and 4-7 abandoned worlds repeatedly
  stopped advancing, starving both agents. Abandoned worlds our own runs left behind (identifiable
  by two-order gigs and a frozen clock) were deleted; four we do not own were left alone. The
  final world is separate, so this does not affect it.
- The server had two multi-minute outage windows; quick_test.py retries and final.py heals.
- The final world is one secret seed: expect the same spread measured here (ev-3/ev-6 style hard
  seeds earn ~EUR8-9k even when everything works; the worst tested world here was 89.1% on time).
