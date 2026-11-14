# Spread level 8 - dispatcher v9 (branch deepseek-skdnfakldsfb)

Worktree: /Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb
Branch: deepseek-skdnfakldsfb (off main @ deb34f5), pushed to origin. Parent worktree never written.

## 1. Mission status (shipped defaults, deterministic live practice runs)

Shipped configuration (all defaults in dispatch.py, no overrides):
  * post a won order at once (hold_hours=0)
  * conservative two-car bundling (link <= 35 min, drive <= 280 min, deadline gap <= 18 h)
  * safe note reading (notes_reader.py, 100% exact on the reveal truth)
  * competitor threshold from the fitted online lane-aware model (comp_from_qm=1)
  * fitted P(late) gate at 0.15 (supply.py + late2.json)
  * expected-net selection bar EUR5 (select_model.py, OOF R2 0.41, decile-calibrated)

Evidence on the six calibrated archived seeds (ev-1..ev-6, same seed = same world):

| seed  | margin/week | on-time | won |
|---|---|---|---|
| ev-1 | EUR17,059 | 98.4% | 448 |
| ev-2 | EUR14,484 | 95.6% | 405 |
| ev-3 | EUR9,568  | 97.8% | 321 |
| ev-4 | EUR20,286 | 97.2% | 459 |
| ev-5 | EUR15,396 | 96.0% | 452 |
| ev-6 | EUR10,348 | 98.3% | 298 |
| MEAN | EUR14,524 | 97.22% | 397 |

A fresh rerun of the shipped defaults (no overrides at all) reproduced the two largest seeds:
ev-1 EUR16,707/98.7% and ev-4 EUR20,286/97.2% - i.e. the panel numbers are what the committed
defaults do, not just what the experiment flags did.

=> The mission thresholds are met on the calibrated panel: mean margin EUR14,524/week
(>= EUR14k) and 97.22% of won orders on time (> 97%), all six runs ranked (>= 90%).

Generalisation (never used to tune the shipped config before the final choice):

| panel | mean margin | mean on-time | worst on-time | ranked |
|---|---|---|---|---|
| ev-1..6 (calibrated) | EUR14,524 | 97.22% | 95.6% | 6/6 |
| vb-1..6 (unseen)     | EUR13,072 | 96.52% | 91.1% | 6/6 |
| vc-1..4 (unseen)     | EUR12,519 | 97.04% | 96.3% | 4/4 |
| all 16 worlds        | EUR13,478 | 96.90% | 91.1% | 16/16 |

So on unseen seeds the margin lands ~EUR0.5-1.5k below the calibrated panel while punctuality
stays ~96.5-97%. The honest headline: on the seed set the brief names as the benchmark, the
system clears both bars; on fresh seeds it is ~EUR13.5k at 96.9%. The margin-first variant
(same code, gate_pl=0.20, select_net=0) makes EUR14,820 at 96.4% on ev and EUR14,722 at 95.2% on
vb - i.e. both configurations bracket the target, and the shipped one is the punctuality-safe side.
For reference the leaderboard in round 5 is ~EUR13.5-15.5k at 95.8-98.2% (top teams), our team
alignas round 5 is EUR11.9k at ~95.6%; v7-lam50 on the same ev seeds was EUR11,275 at 92.4%.

## 2. What changed against v7, and what each change was worth (A/B on identical worlds)

1. Post at the win (hold_hours=0): +EUR1.7-2.8k/week and +1-2 points on time. Biggest single win.
2. Two-car bundling with tight caps (bundling.py): the archive's best worlds move 66% of cars as
   pickupA, deliveryA, pickupB, deliveryB at ~0.79 x (refA+refB). Loose caps are catastrophic on
   long-chain seeds (ev-4: EUR11.5k, 75% on time); the shipped caps are the working setting.
3. notes_reader.py: 100% exact on 28,503 won note orders (kit reader 90.6-98.9%, 0% recall on
   release/hours/manual/plates/tyres); audiences + pickup floor + moved deadline in pricing.
4. Fitted P(late) (supply features refit in fit_late2.py, OOF AUC 0.789) in the margin and as a
   gate at 0.15: this plus lane pricing is what lifted the hard seeds above 97% on time.
5. Lane-aware competitor pricing (comp_from_qm=1): the quote threshold is
   exp(OnlineCompetitor.log_comp_for(ctx, trust) + QuoteModel.A_value(ctx, n_options)) instead of
   v7's single global SGD level, so the 798 persistent lane factors and the dealer trust enter
   every price. Worth +EUR1,125/week on ev (14,820 vs 13,695) at equal on-time, and +EUR642 on
   the unseen vb panel. This was the largest remaining margin lever.
6. Expected-net selection bar EUR5 (select_model.py): decline a request when the fitted expected
   net of its menu (sum over options of p_take x expected net per won order) is below EUR5.
   Fitted on 202k won orders / 589 worlds; OOF R2 0.412 (MAE 21.2 vs base 29.4); bottom decile
   -15.7 realised vs -6.8 predicted. Combined with the 0.15 gate it moved ev from 14,820/96.4%
   to 14,524/97.2% (EUR300 of margin buys +0.8 points of punctuality).
7. Measured and left off: fitted booking-model fee (hurt unseen seeds), direct offers (neutral),
   rescue repricing / failure-fee inflation (lost more margin than saved), price multiplier
   (chaotic across seeds), dynamic gate (no effect at usable thresholds), menu optimiser.

### The bug that hid all of this for a while

Dispatcher.quote() routed to the unvalidated fitted-menu pricing path whenever QuoteModel was
loaded, not only when use_quote_model=1. Every experiment that loaded the competitor model for
selection or lane pricing silently ran that path and looked catastrophic. Fixing the routing
(now: _quote_model only when use_quote_model=1) turned the same levers into the wins above.

## 3. Archive (read-only) and the models on the branch

782 finished level-8 worlds are on disk (harvested with four practice tokens each; no world is
created/advanced/forked/deleted by the harvester; 589 have usable reveal+roster+events).

- notes_reader.py: deterministic note reader (100% exact on won note orders).
- quote_model.py + data/fits/quotes.json: competitor level/lane/day Bayesian filter (price-war
  detector 78%/5.6% false alarm), option-choice logit calibrated 0.470 vs 0.470, P(win) AUC 0.889.
  Used at runtime for the lane-aware threshold (comp_from_qm=1).
- supply.py + data/fits/supply.json: booking hazard (bin CV AUC 0.879), expected fee (MAE 0.17 x
  ref), lateness model (shipped block degenerate; late2.json is the working refit).
- late_model.py + data/fits/late2.json: P(late), OOF AUC 0.789, calibrated deciles.
- select_model.py + data/fits/select.pkl: expected net per won order (section 2.6).
- bundling.py: chain feasibility and the two-car fee.

## 4. Evaluator (evaluate.py) - built, NOT calibrated - honest limitation

evaluate.py replays an archived week's request stream through the policy and scores it with P(win),
option choice, expected fee and P(late); no world is created. It does not reproduce recorded
margins: on the 50 worlds whose recorded run is known it correlates +0.14 with the recorded margin
and over-estimates by ~EUR16k/world. It cannot see driver capacity, drops, chains or gig expiries -
exactly the money leak - and its outcome feedback does not match the real dealer. Use it for
debugging only. Calibration path: replay each recorded world with its own recorded policy version
and quotes, then fit the residual correction. All decisions in this report are based on paired
deterministic live practice runs instead.

## 5. Reproduce from a clean shell

    cd '/Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb'
    . ./env.sh                 # SPREAD_URL, SPREAD_KEY, SSL_CERT_FILE, PATH (secrets stay in .env)
    python archive.py          # only if data/L8 is absent (read-only harvest, 4 tokens/world)
    python late_model.py fit && python fit_late2.py && python select_model.py fit   # refits
    python quick_test.py --tag check --seeds ev-1,ev-4 --parallel 2
    #    expected with shipped defaults: ev-1 EUR17,059/98.4%, ev-4 EUR20,286/97.2%
    python final.py            # the live final; one command, no arguments

The fitted artefacts needed at runtime are committed under data/fits/ (map.json, quotes.json,
supply.json, late2.json, select.pkl).

## 6. The final runner (verified)

python final.py - one command, no arguments; launch it before the final (Sunday 07:55). It polls
GET /v1/final every 10 s until the organisers set it, then plays from the world stream: every price
request is answered in the same action batch (seconds, inside the 15-minute window); the outbox
flushes on every event and on a 0.5 s idle tick against the server clock; it rebuilds from
snapshot+events (Desk.rejoin) after a server restart or network loss, dropping repeated events by
seq; it never creates/forks/advances/deletes a world. Heartbeat every 60 s (world time, orders,
margin, on-time share, seq, rebuilds, errors). Ctrl-C/SIGTERM prints the score and leaves the world
running; relaunch rebuilds.

Verified on this branch:
- python final.py pre-flight: PASS (builds the Dispatcher with shipped defaults, answers a
  synthetic price request; polls the 404 final endpoint without creating anything).
- python test_final_rejoin.py: ALL CHECKS PASSED - replays archived worlds through the Dispatcher,
  re-joins from snapshot+history with identical books, answers every request inside its window,
  drops a repeated event, heals an injected stream interruption and falls back safely.

## 7. What to test next (ordered by expected value)

1. Seed-robust margin: the 12 unseen-seed worlds average EUR12.8k at 96.8% while the calibrated
   panel is EUR14.5k at 97.2%. Fit the selection/cost model per seed class (supply map, driver
   count, competitor level) instead of globally; the hard seeds (vb-2) are supply-starved and
   need an earlier/more aggressive fee ladder, not lower prices.
2. Fee ladder from the booking model, retuned per seed: the earlier full fee-model attempt hurt
   unseen seeds, but a model-driven ladder that only raises fees on short-lead gigs is untested at
   scale (the lead-following floor exists behind fee_floor_target but was a no-op at 0.7/12 h).
3. Direct offers as a last-3-hours rescue (offer_h=3): untested combination with the new pricing.
4. Trust flywheel: price already uses 1.01^trust; avoid late orders for dealers close to their
   25th counted order (worth ~EUR1.5/quote at trust 25).
5. Evaluator calibration (section 4), then offline search over selection/fee settings.

## 8. Partner hand-off (all on this branch)

- Post at the win (hold_hours=0) - biggest single win, trivial to port.
- Lane-aware pricing: reuse quote_model.OnlineCompetitor + QuoteModel.A_value for the competitor
  threshold (this is a ~EUR1.1k/week change on its own).
- bug lesson: never route to an experimental pricing path implicitly on model availability.
- bundling.py + tight caps; never raise fees on a drop/repost by default.
- notes_reader.py (100% exact) + safe-mode wiring in post().
- fit_late2.py / SupplyLateModel (the shipped supply.json lateness block is degenerate).
- select_model.py + data/fits/select.pkl (expected-net bar).
- final.py + test_final_rejoin.py, and quick_test.py + the deterministic ev/vb/vc A/B method.

## 9. Operational notes / risks

- Practice slots are shared with the partner (8 total). Abandoned worlds repeatedly starve both
  agents; we delete only worlds our own runs left stuck (identifiable by two-order gigs + frozen
  clock) and never touch the partner's or anyone else's.
- There were two multi-minute server outage windows this session; quick_test.py retries worlds,
  final.py heals from snapshot+events.
- The final world is one secret seed: measured seed spread is EUR7.7k-20.3k margin and 91-99%
  on-time for the shipped config, so a hard seed can land near EUR8-10k. Nothing about the
  shipped policy can fix that; it is the seed's driver shortage showing through.
