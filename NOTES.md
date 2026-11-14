# Spread level 8 - dispatcher v8 (branch deepseek-skdnfakldsfb)

Worktree: /Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb
Branch: deepseek-skdnfakldsfb (off main @ deb34f5). The parent worktree was never written to.

## 1. Where we are

Target: >97% of won orders on time AND mean margin >= EUR14k/week at level 8, ranked.
Leaderboard leader: EUR14,678/week at 98.0% on time (bob-the-builders, round 4). Our team
(alignas) round 4: EUR11,834/week at 95.6%, rank 7.

Best validated candidate on this branch (Dispatcher v8 defaults):

| panel | v7-lam50 | v8 (this branch) |
|---|---|---|
| six archived seeds ev-1..ev-6, mean margin | EUR11,275 | EUR12,573 (+11.5%) |
| mean on-time | 92.4% | 95.1% |
| ranked runs | 6/6 (worst 90.3%) | 6/6 (worst 90.7%) |
| four fresh seeds vb-1..vb-4, mean margin | EUR11,418 | EUR12,402 (+8.6%) |
| worst on-time on the fresh seeds | 82.1% | 85.1% (that seed is hard for both) |

Per seed (same deterministic world, so these are paired comparisons):
ev-1 13,365 -> 15,102 (91.0 -> 95.4%); ev-2 10,995 -> 12,199 (94.8 -> 94.9%);
ev-3 6,386 -> 9,623 (95.4 -> 98.0%); ev-4 14,308 -> 17,044 (90.3 -> 90.7%);
ev-5 13,789 -> 13,520 (94.5 -> 96.4%); ev-6 8,806 -> 7,948 (94.8 -> 94.9%).

That is a real ~10% improvement, but it is NOT yet 97% on time or EUR14k/week. Section 8 says
what is still missing and what to test next. The honest read: v8 closes about a third of the gap
to the leaders on margin and about half on punctuality.

## 2. What changed against v7 (each measured on the same deterministic worlds)

1. Post at the win, not 36 h before the last pickup (Policy.hold_hours 0). Worth
   +EUR1.7k-2.8k/week on ev-1/ev-4 and 1-2 points of on-time. The old hold was the single
   most expensive constant in v7.
2. Two-car bundling, conservatively (bundling.py). Two single-car orders whose chains fit
   (delivery->next pickup <= 35 min, total planned drive <= 280 min, deadlines within 18 h) are
   posted as ONE gig: pickupA, deliveryA, pickupB, deliveryB, fee ~0.79*(refA+refB) instead of
   0.90*ref each. The archive's best bundled worlds (w53d32d3: EUR15,653, 98.7% on time; 119 of
   270 gigs carry two orders) run this at fee 0.65-0.79 x (refA+refB). With loose caps (75 min
   link, 540 min drive) the same feature is catastrophic on ev-4 (EUR11.5k, 75% on time, 24-38%
   of bundles expire) because chains there are long; the tight caps are the working setting.
   The live A/B: ev-1 13,531 -> 15,077, ev-4 15,447 -> 17,017.
3. Read the dealer's note (notes_reader.py). Deterministic reader; 100% exact against the
   reveal's truth on all 28,503 won note orders (the kit starter reader gets 90.6-98.9% exact,
   0% recall on release/hours/manual/plates/tyres). Used in safe mode: manual-licence and
   transfer-plate audiences, a later-ready/handover pickup floor, and the moved deadline in
   pricing. Full window narrowing (note_mode=2) is available but cost ~EUR1k on ev-4.
4. Fitted lateness gate and expected driver fee (supply.py + data/fits/late2.json). P(late)
   from 24 supply/slack/backlog features (OOF AUC 0.789, calibrated) and the expected paid fee
   (MAE 0.17 x ref) replace v7's hand tables in the quote margin, and any option with
   P(late) > 0.30 is declined. The gate was close to free on ev-1/ev-4 and added +0.9 points of
   on-time on ev-2. (The lateness block shipped in data/fits/supply.json is degenerate - its
   standardisation was not saved - so fit_late2.py refits the same features; load it with
   late_model.SupplyLateModel.)
5. No fee inflation on failures. A v8 draft raised every reposted gig's fee by 18% and
   repriced on every driver drop; that one default took ev-4 from EUR17,119 to EUR12,780. It is
   off (retry_raise=drop_raise=0) and stays off unless A/B tested.
6. Direct offers to drivers near an at-risk pickup are implemented (offer_h>0) but measured
   neutral (ev-4 15,447 -> 15,425; ev-2 11,791 -> 11,710 with +0.9 point on-time), so off.
7. The fitted competitor/P(win) model (quote_model.py, held-out AUC 0.889, calibrated option
   choice, 798 lane factors, price-war detector) is on the branch but the first live test of the
   full menu optimiser was worse than v7 pricing (ev-1 13,531 vs 14,946; ev-2 11,465 vs 13,414),
   so use_quote_model=0. The model is good; the EV optimiser needs the true cost model.

## 3. Archive: what the best worlds actually do (this is the roadmap that is left)

210 finished level-8 worlds were harvested (archive.py, four practice tokens each, no world
created/forked/deleted; 74,731 won orders; 27 seeds). runs/ history plus the unrecorded
worlds show several earlier policy variants on the same seeds, so decisions are directly
comparable. The best worlds:

| world | won | on time | margin/week | fee/order | 2-order gigs |
|---|---|---|---|---|---|
| w53d32d3 | 373 | 98.7% | EUR15,653 | EUR53.4 | 119/270 |
| w892edb9 | 376 | 97.9% | EUR14,733 | EUR56.3 | 117/275 |
| wb6b6c12 | 398 | 98.0% | EUR15,390 | EUR56.5 | 80/329 |
| wbd07835 | 462 | 93.9% | EUR16,682 | EUR76.1 | 0/469 (selective, no bundles) |
| v7 ev-4 | 517 | 90.3% | EUR14,308 | EUR81.6 | 0/522 |

Three things separate them from v7: (a) bundled cars cut the driver fee; (b) they win ~25-30%
fewer orders than v7 and price slightly higher (median x +0.02-0.04 of list), so the cars they
do win are the ones drivers can actually move; (c) v7's late orders come almost entirely from
gigs that never found a driver (419 of 657 late orders had an expired gig; 81% of the orders
that reach the 3-hour deadline warning are late). The leaderboard teams behave like (b):
bob-the-builders wins 287-316 orders/run at ~EUR45-50 net/order while we win 376-397 at
~EUR27-33. That is the remaining lever: selection + higher price per order, not more volume.

## 4. Fitted models on the branch (all validated world-held-out)

- notes_reader.py - deterministic note reader, 100% exact on 28.5k won note orders.
- quote_model.py + data/fits/quotes.json - competitor level/lane/day model with an online
  Gaussian filter and a price-war detector (78% of war days detected after 8.5 quotes, 5.6%
  false alarm), dealer option-choice conditional logit (P(take|won) calibrated 0.470 vs 0.470),
  held-out P(win) AUC 0.889. Pricing rule table is in reports/quotes.md.
- supply.py + data/fits/supply.json - booking hazard (bin CV AUC 0.879, fee elasticity fitted
  on 2,357 random-fee gigs), expected driver fee (R2 0.58 on log, MAE 0.17 x ref), and the
  lateness model (CV AUC 0.786; see the late2 refit below).
- late_model.py + data/fits/late2.json - P(late) on the 24 supply features, refit with the
  standardisation kept: OOF AUC 0.789, deciles 0.279 predicted vs 0.297 actual at the top.
- bundling.py - chain feasibility (stop order, windows, 12 h drive limit) and the two-car fee.

## 5. Evaluator - built, NOT yet calibrated

evaluate.py replays an archived week's request stream through the policy and scores it with
P(win), option choice, expected fee and P(late), so no world is created. It currently does NOT
reproduce recorded margins: on the 50 worlds whose recorded run we know it correlates +0.14
with the recorded margin and over-estimates by ~EUR16k/world, and on ev-1..ev-6 it
over-estimates by ~EUR7k. Two known reasons: (i) it cannot see driver capacity, drops, chains
or gig expiries, which are exactly v7's money leak; (ii) the sampled-outcome feedback into the
policy's online estimator and the model's multi-option win correction do not match what the
real dealer did. It is therefore a debugging tool, not yet a decision instrument. The next step
(cheapest first) is to replay each recorded world with its own recorded policy version and its
own quotes and only then fit the residual correction; the current replay uses today's policy on
all worlds, which mixes policies and cannot calibrate. See 'next tests'.

## 6. Live-A/B method (how every number above was produced)

The six archived seeds are reproduced exactly by the seed strings ev-1..ev-6: running v7-lam50
on ev-1 gives EUR13,365 / 455 orders / 91.0%, identical to runs/v7-lam50.json. quick_test.py
plays one seed, one world at a time, waits for a free practice slot, keeps the reveal in
data/L8/ and writes runs/<tag>.json. Two practice slots at a time were explicitly approved by
the user; every world was deleted on failure, and three abandoned worlds our own runs left
behind (identified by two-order gigs) were cleaned up once they were proven stuck. Full log of
the ~40 A/B runs: NOTES-run.md.

## 7. Reproduce from a clean shell

    cd '/Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb'
    . ./env.sh                    # SPREAD_URL, SPREAD_KEY, SSL_CERT_FILE, PATH (secrets stay in .env)
    # 1. the archive (data/ is gitignored; harvest ~200 worlds, four practice tokens each,
    #    no slot, no fork/advance/delete)
    python archive.py
    # 2. the fitted models are committed under data/fits/*.json; refit with:
    python late_model.py fit && python fit_late2.py
    # 3. deterministic A/B on the six known seeds (2 live practice worlds at a time):
    python quick_test.py --tag check --seeds ev-1,ev-2 --parallel 2
    #    expected: ev-1 EUR15,102 / 434 orders / 95.4% on time; ev-2 EUR12,199 / 435 / 94.9%
    # 4. the live final (one command, no arguments; polls until the organisers set the final):
    python final.py

## 8. The final runner

python final.py (the user launches it Sunday 07:55; ~34 real minutes once the world starts).
It imports dispatch.py lazily, prints a pre-flight that builds the Dispatcher and answers a
synthetic price request (verified PASS on this branch), polls GET /v1/final every 10 s, then
plays from the world stream: every price request is answered in the same action batch (seconds,
inside the 15-minute window), held-order timers and the outbox are flushed on every event and
on a 0.5 s idle tick using the server clock, and it rebuilds from snapshot+events (Desk.rejoin)
after a server restart or a network loss, dropping repeated events by seq. It never
creates/forks/advances/deletes a world. A heartbeat every 60 s shows world time, orders, margin,
on-time share, seq, rebuilds and errors. Ctrl-C/TERM prints the score and leaves the world
running; relaunching rebuilds.

Offline tests: python test_final_rejoin.py replays 5 diverse archived worlds through the
Dispatcher and then re-joins from the end snapshot + history, asserting the books are identical
and that the stream loop answers every request inside its window with an injected interruption
and a repeated event. It passes except the (now expected) 'held-order timers fired' check, which
v8 does not need because it posts at the win; the test knows that when hold_hours=0.

## 9. What to test next (ordered by expected value / cost)

1. Selection to ~300 orders and higher price per order (the leaderboard teams' profile).
   Reuse quote_model.OnlineCompetitor + the supply models to rank requests by expected margin
   and set a request-level bar; the drafts tried (ev_bar on the legacy EV estimate) were too
   crude - they picked long expensive lanes with high driver cost. The right cost model for the
   bar is supply.booking.expected_fee_cents. Target: 280-330 orders, net EUR45+/order.
2. Fee policy from the booking model. supply.booking.fee_for_target() gives the fee ratio
   that books an order with probability 0.8-0.9 within 8 h. Setting the opening fee per lane
   (instead of the flat 0.9 x ref) should cut the fee on easy lanes and fix the expiries on hard
   ones. This is the most direct attack on the 419-expired-gig late orders.
3. Direct offers as a rescue, tuned. Offer earlier (10-12 h before the last pickup), at a fee
   clearly above the app fee, to drivers who viewed one of our gigs in the last 2 h (we see
   views with driver ids). The draft implementation is on the branch, off by default.
4. Trust flywheel. At level 8 dealer trust raises the accepted price by 1% per on-time order
   (up to 25); the live price is only 1.01^trust in the fitted model. Make sure the price uses
   the dealer's current trust (it does) and that late orders are avoided for dealers close to
   their 25th counted order. That is worth ~EUR1.5/quote at trust 25.
5. Evaluator calibration as in section 5 (replay recorded policies, fit the residual).
6. War day: quote_model's detector is good; currently the policy just prices from its own
   online level. Cutting prices on detected war days to win the good lanes (the competitor is
   20% under everywhere) is an untested margin play.

## 10. Partner hand-off (worth porting by hand, all on this branch)

- Post at the win (hold_hours=0) - biggest single win, trivial.
- The bundling module + the tight caps; do not turn the caps loose in one go.
- The fee-inflation lesson: never raise fees on a failure by default.
- notes_reader.py (100% exact) + safe-mode wiring in post().
- fit_late2.py / SupplyLateModel: the shipped supply.json lateness block is broken, use late2.json.
- final.py + test_final_rejoin.py.
- quick_test.py + the deterministic ev-1..ev-6 A/B method.
