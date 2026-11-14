# Decision log (level 8, branch deepseek-skdnfakldsfb)

A running record of every decision, the evidence behind it, and its status. Written so the final
presentation can explain each big idea and show its measured effect. Numbers are deterministic
practice-run results (same seed = same world) unless stated otherwise.

## Ground rules that shape every decision

- Score = gross margin in euros. On-time is a **per-week qualification threshold (90%)**, not a
  score term: "If fewer than 90% of the orders you win arrive on time, you are not ranked."
- Ranking rounds: a team is ranked when it has played all five weeks, each at >= 90% on time;
  round margin = sum of five weeks; rounds do **not** count for the final.
- The final: one secret level-8 week on Sunday 08:00 (set at 07:30). Result = margin. The 10 ranked
  teams with the highest margin present to the jury; the jury then judges elegance, clarity,
  creativity and robustness. So the practical goal is: **rank (>=90% on time), make as much margin
  as possible, and be able to explain the system clearly.**
- Mission bars used here: > 92% on time (safety over the 90% cut) and mean >= EUR14k/week.

## Shipped baseline (v9 margin-first) - status SHIPPED

Defaults in dispatch.py: post a won order at once; conservative two-car bundling (link <= 35 min,
drive <= 280 min, deadline gap <= 18 h); safe note reading; lane-aware competitor threshold from
the fitted online model (comp_from_qm=1); fitted P(late) gate at 0.30; expected-net selection bar
off; hand fee ladders opening at 0.9 x reference.

Measured with no overrides (paired panels):
  ev-1..6  EUR15,042/wk  mean on-time 95.92%  worst 94.6
  vb-1..6  EUR14,874     mean on-time 95.27%  worst 90.3 (ranked)
  vc-1..4  EUR13,079     mean on-time 95.01%  worst 94.3
  all 16   EUR14,488     95.45%               16/16 ranked
  (v7 reference on ev: EUR11,275 @ 92.4%.)

## Decisions

### D1. Post a won order immediately - SHIPPED
Evidence: +EUR1.7-2.8k/week and +1-2 points on-time on identical worlds versus holding until 36 h
before the last pickup. Table: NOTES-run.md.
Effect: part of the v8->v9 jump; kept through every later change.

### D2. Two-car bundling, tight caps - SHIPPED
Evidence: archive best worlds move ~66% of cars as pickupA-deliveryA-pickupB-deliveryB at ~0.79 x
(refA+refB). Loose caps (75 min link / 540 min drive) are catastrophic on long-chain seeds
(ev-4: EUR11.5k, 75% on time); the shipped caps were the working setting. Loosening them further
(link 42 / drive 320) measured -EUR1,195 (ev-1) and -EUR2,549 (ev-4) -> REJECTED (D9).
Status: shipped caps 35/280/18.

### D3. Safe note reading - SHIPPED
Deterministic parser, 100% exact against the reveal truth on 28,503 won note orders (kit reader:
90.6-98.9%, 0% recall on release/hours/manual/plates/tyres). Used for audiences, pickup floor and
moved deadlines. Kept.

### D4. Lane-aware competitor threshold (comp_from_qm) - SHIPPED, biggest margin lever
Evidence: fixes a routing bug and replaces v7's single global price level with the fitted online
model's lane+day+trust threshold. ev panel 13,695 -> 14,820 (+EUR1,125, same on-time); vb panel
14,080 -> 14,722 (+EUR642).
Status: comp_from_qm=1 default.

### D5. Fitted P(late) gate at 0.30 for this mission - SHIPPED
Evidence (ev panel): gate 0.15 + selection bar EUR5 = EUR14,524 @ 97.22%; gate 0.20 = EUR14,820 @
96.43%; gate 0.30 = EUR15,042 @ 95.92%. New mission only requires >92%, so the margin-maximising
0.30 is shipped; the selection bar stays implemented as the punctuality knob if the bar tightens.
Gate 0.40 measured EUR14,992 @ 95.92% -> no improvement, rejected (D10).

### D6. Fitted expected-net selection bar - SHIPPED OFF (available)
Model: select_model.py, 202k won orders / 589 worlds, OOF R2 0.41, decile-calibrated
(bottom decile realised -15.7 EUR/order vs -6.8 predicted). At gate 0.15 it buys +0.8 points of
on-time for EUR300/week; at gate 0.30 it is not worth it. Kept as a documented knob, default 0.

### D7. Fitted booking-model opening fee - available, OFF
Model-driven fee (book with P=0.85 within 8 h) gave +221/+2.0 points on ev-2 and +139/+0.2 on
ev-4 in an early test, but hurt unseen seeds in a later combined test; not shipped. The
fee_step_scale knob and supply-tier opening fee (fee_open_tiers) were added for a controlled retest.

### D8. Cheap fees alone (fee_open=0.82) - MIXED, NOT SHIPPED (pending validation)
Paired six-seed ev panel: +EUR568 mean, +0.14 pt mean on-time, but 2 of 6 seeds negative
(ev-1 -944, ev-4 -1,524 and -3.1 points) and 4 positive (up to +2,212). Euro decomposition:
revenue/order +0.9, fee/order -0.4, penalties/order -0.1. Since the mean-improvement rule is not
enough, it needs the unseen panels (vb/vc) before any ship decision; those runs were paused under
the "comparable baseline and safety first" instruction.

### D9. Looser bundle caps alone - REJECTED
link 42 / drive 320: ev-1 EUR16,592/95.6 (base 17,787/96.5), ev-4 EUR21,215/92.5 (base
23,764/96.7). Long chains expire; both margin and punctuality fall.

### D10. Gate 0.40 - REJECTED (no improvement)
Full ev panel EUR14,992 @ 95.92% (gate 0.30: EUR15,042 @ 95.92%).

### D11. Fitted-menu pricing (use_quote_model=1) - REJECTED
Offline model-internal probe suggested +EUR6-9.5k EV, but the live screen was catastrophic:
ev-1 -EUR1,103 @ 81.2% (44 expired gigs), ev-4 EUR6,382 @ 81.6% (76 expired gigs). The model
overprices against real driver behaviour (fee/order EUR115-132). Keep use_quote_model=0. Lesson:
model-internal EV is not evidence; only paired live runs are.

### D12. Rescue repricing / failure fee inflation - REJECTED earlier
retry_raise/drop_raise 0: every failure-triggered fee change measured lost more margin than it
saved (ev-4 17,119 -> 12,780 in one draft). Kept off.

### D13. Direct offers - NEUTRAL, OFF
Tuned offer_h=12/fee x1.15: ev-4 +461/+1.0 pt, ev-2 -267/-0.2 pt -> no robust gain; off.

### D14. Safety controller for the 90% cut - IN PROGRESS (round 6)
Motivation: vb-2 nearly failed at 90.3% on time. Diagnosis of that week (archive events):
40 late/undelivered of 397 won; 25 lates due Friday with 23 expired gigs that day; 57 deadline
warnings fired and 52 of those orders still went late. Late orders span same_day (22/203) and
next_day (17/184); accepting lead times were similar (31.9 h late vs 33.2 h on-time). The fitted
per-order P(late) at quote was ~0 for all won orders (AUC 0.67 for late vs on-time), so this was an
aggregate Friday capacity crunch, not a set of individually bad quotes.
Controller implemented (risk_control): projects (already late + sum over unbooked orders of
1 - P(book by deadline from the booking model)) / orders won; when above risk_target (default 0.05)
it raises the quote margin bar and tightens the P(late) gate, and above risk_hard (0.09) it demands
extra margin on same_day/next_day options. Default off so frozen v9 is untouched. Screen on
vb-2 + ev-4 running.
Also to test as the alternative response: earlier fee escalation on at-risk orders (rescue_h=18,
mult 1.3) - the user's instruction is to test the response, not assume raising fees is safer.

### D15. Partner-bank comparison - PLANNED (pilot first)
Partner's main@732ed3c (their 12:00 round: #11, EUR79,069 over 5 weeks = EUR15,814/wk @96.8%;
their own README claims ~EUR13.4k/wk on their 24-week bank). Their bank seeds (bt-1..bt-6, ho-3,
st-25) are new to our policy but were tuned on by their policy, so they are a *paired comparison
bank*, not a clean holdout. Plan: one-seed pilot to check simulator/accounting conventions match,
then paired v9-vs-candidate results on the bank, reported per seed.

### D16. On-time 90.3% week is the biggest risk - OPEN
A ~95% mean does not protect against disqualification on one bad week. If the safety controller
does not fix the Friday-crunch failure mode, the fallback is the punctuality knob (gate 0.15 +
selection bar EUR5: 97.2% on the ev panel) at a known cost of ~EUR300-500/week.

## Open questions

- Does the safety controller fix vb-2 without hurting ev-4/ev-1? (screen running)
- Does fee_open=0.82 survive the unseen panels (vb/vc) - i.e. is it a real +EUR568 or ev-panel luck?
- On the partner bank, does frozen v9 beat their cached baseline seed-by-seed? (pilot queued)
- Is a non-final ranking round worth entering for out-of-panel evidence? (needs coordination with
  the partner's auto-round script; do not disturb the current runner.)

### D14 update (round 6, controller experiments)

- Controller v1 (projected late share target 0.05/0.03, gain 8/30): NO measurable effect. Instrumented
  run shows why: the booking-model projection of unbooked orders is optimistic (risk_max 2.0-2.6%),
  so the projected share never reaches the thresholds while quoting; ev-4 and vb-2 results were
  identical to frozen v9. The model's p_book says "these will book", and they do not.
- Rescue response (rescue_h=24, rescale 1.35) tested on vb-2 and ev-1: vb-2 5,947 @ 91.0%
  (+0.7 pt, -EUR4,226), ev-1 13,667 @ 96.8% (-EUR4,120, +0.3 pt). The fee response buys almost no
  punctuality for a large margin cost. REJECTED as a general response.
- Realised late share timeline is the useful signal, not the model projection: vb-2 0.078 by Tue
  (14 late), 0.099 by Thu; ev-4 never above 0.038. New response under test: risk_off_short - once
  the realised late share exceeds 5% (with >=30 decided orders), quote only within_3_days options
  (decline same_day/next_day with a far-above-list price, so the request is still answered and the
  dealer relationship is not damaged). This spends the deadline budget instead of the fee budget.
- If risk_off_short works on vb-2 without hurting ev-4, it becomes the safety fallback; if not,
  the documented safe setting (gate 0.15 + select bar EUR5: 97.2% on ev) remains the alternative
  with its known ~EUR300-500/week margin cost.

### D15 update (partner-bank pilot, one seed) - VERIFIED MECHANICS

Pilot: frozen v9 (shipped defaults) on seed bt-1 (first seed of the partner's backtest bank),
level 8, run through our own quick_test/Dispatcher. Result: margin EUR13,579, 408 won, 98.8%
on time, ranked, 62 s. Accounting identity verified exactly: sum of the event ledger lines
(revenue 35,711.08 - driver_fee 21,830.79 - failed_visit 128.72 - waiting 60.00 - late_refund 87.32
- release_fee 25.39) = EUR13,578.86 = the score's margin. So the simulator version, level-8 world
settings and the margin accounting match what our system expects.
Limitation: the partner's cached per-seed baseline numbers are not present in the shared repo
(their runs/ directory is gitignored and empty here), so we cannot diff against their cached
baseline directly; a fair comparison needs either their per-seed numbers or both policies run on
the same bank. The bank is also not a clean holdout for their policy (they tuned on it).

### D14 update 2 (safety responses actually tested on the hard week vb-2)

- projection-based controller (target 0.02-0.05, gain up to 120): no effect. Instrumented: the
  model's projected late share never exceeded 2.6% because the booking model believes unbooked
  orders will book (risk_max 0.0205 ev-4, 0.0261 vb-2). The realised late share is the useful
  signal (vb-2 7.8% by Tuesday), but by then some damage is done.
- realised-share "quote only within_3_days" response at 5%: never triggered during quoting
  (vb-2 realised share was 4.9% at the last moment quotes were still flowing), so identical to v9.
  Retested at 4% threshold on vb-2.
- fee-rescue response (rescue_h=24, mult 1.35): vb-2 5,947 @91.0% (-EUR4,226 for +0.7 pt),
  ev-1 13,667 @96.8% (-EUR4,120 for +0.3 pt) -> REJECTED as a general response.
- Interim conclusion: the 90.3% week is largely intrinsic to that seed for this policy family.
  The punctuality-safe alternative remains gate 0.15 + select bar EUR5 (ev 14,524 @97.2%,
  vb 13,072 @96.5%, vc 12,519 @97.0%) at a measured margin cost; choosing between it and the
  shipped margin-first v9 is the final risk decision.

## Round-7 decisions (final configuration, runbook, presentation)

### D16. Final configuration: keep margin-first v9 - DECIDED (branch lead, evidence-based)
All 16 deterministic validation worlds rank with the shipped defaults; worst is 90.3% (vb-2) and the
average is 95.45% at EUR14,488/week (ev calibration panel EUR15,042 @ 95.92%). The safe alternative
(gate 0.15 + select bar EUR5) reaches 97.2% on the calibration panel but costs ~EUR300-500/week and
~EUR1k on the 16-world mean. Ranking is by margin among teams that clear the 90% cut, so the
margin-first setting is the better expected top-10 play; the safe knob is documented in NOTES.md and
FINAL.md as a two-field change if the team prefers lower variance on the day.

### D8 (fee_open=0.82) - PAUSED, not promoted
Mixed paired evidence (+568 mean, 2/6 seeds negative, worst ot 93.5%). Never validated on vb/vc
because practice slots were saturated by the partner's round automation; the default stays off.
If time allows it is a one-field retest on the validation panels.

### D14. Safety controller - CLOSED: no effective response found within the time
Projection controller, realised-share short-off, and fee-rescue were each tested against frozen v9
on identical worlds; none improved the hard week without a larger margin cost. The honest conclusion
is recorded above (Friday-capacity crunch; the booking model is too optimistic to give early warning).

### D15. Partner-bank comparison - PILOT DONE, full table OPEN
bt-1 pilot: frozen v9 EUR13,579 / 98.8% / ranked; accounting identity exact. The full 8-seed paired
table needs either the partner's cached per-seed numbers or two practice slots while their
automation is paused.

### D17. Deliverables for the final - DONE
- FINAL.md: exact launch/verification checklist for Sunday (one command: python final.py).
- PRESENTATION.md: 5-10 slide outline with the big ideas and their measured effects.
- NOTES.md: full evidence and the risk decision; NOTES-decisions.md: this log.
- Runner re-verified after all code changes: final.py pre-flight PASS, test_final_rejoin ALL CHECKS
  PASSED; shipped defaults unchanged (gate 0.30, selection 0, lane-aware pricing on).
