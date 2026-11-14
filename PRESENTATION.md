# Spread final - presentation outline (draft)

One system: a dispatcher that prices dealer requests against a hidden competitor and staffs them
with freelance drivers, for one simulated level-8 week. Score = gross margin; a week with <90% of
won orders on time is not ranked.

## The one idea

Every decision is an expected-value trade-off with the 90% on-time rule priced in:
- **Quote**: choose the delivery-option menu that maximises expected margin, where
  P(win) comes from a fitted competitor model (persistent lane effects, Bayesian day level,
  dealer trust) and the cost side from fitted booking/lateness models.
- **Staff**: post a won order immediately at an opening fee, then raise it on a schedule anchored
  to the last safe pickup, and pair two single-car orders into one gig when the chain is tight.
- **Read**: parse the dealer's note deterministically (100% exact against the reveal truth) and
  quote against the real window/deadline.

## Big ideas and the measured effect of each (paired, same deterministic worlds)

| idea | effect | evidence |
|---|---|---|
| post a won order at once instead of holding 36 h | +EUR1.7-2.8k/wk, +1-2 pts on time | ev-1/ev-4 paired runs |
| two-car bundling with tight chain caps (35 min link, 280 min drive) | +EUR450-650 in seeds that accept it; loose caps are catastrophic (ev-4: EUR11.5k/75%) | archive best worlds run 66% cars paired at ~0.79 x summed reference fee |
| lane-aware competitor threshold from the fitted online model | +EUR1,125/wk on ev, +EUR642 on unseen vb at equal on-time | latest largest single change |
| fitted P(late) gate (supply features, OOF AUC 0.789) | hard seeds: ev-1 91.0->96.7%, ev-4 90.3->95.5% | paired ev panel |
| fitted expected-net selection bar (202k orders, OOF R2 0.41) | +0.8 pts for EUR300/wk at gate 0.15; not worth it at 0.30 | paired ev panel |
| deterministic note parser | 100% exact on 28,503 won note orders (kit: 0% recall on release/hours/plates) | notes evaluation |
| restart-proof live runner | survives restart/stream loss; rejoin test ALL CHECKS PASSED | test_final_rejoin.py |

## Current measured performance (deterministic practice seeds)

- ev-1..6 (tuning panel): **EUR15,042/week, 95.92% on time** (v7 baseline: EUR11,275 / 92.4%)
- vb-1..6 (validation): EUR14,874 / 95.27%
- vc-1..4 (validation): EUR13,079 / 95.01%
- all 16 test worlds: **EUR14,488/week, 95.45% on time, 16/16 ranked**
- hardest validation seed vb-2: 90.3% on time (still ranked); the safe variant (gate 0.15 +
  selection bar) gives 91.1% there and 97.2% on ev at ~EUR300-500/week margin cost.

## What we tried and rejected (credibility)

- cheap fees alone: +EUR568 mean but 2/6 seeds negative; not shipped without validation.
- looser bundle caps: -EUR1.2-2.5k and -4 pts on time on tested seeds.
- fitted-menu pricing: catastrophic live (81-82% on time) even though model-internal EV looked +6-9k.
- rescue fee escalation on at-risk orders: -EUR4.2k for +0.7 pt. Rejected.
- projection-based safety controller: no effect - the booking model over-estimates bookings.
- lesson: model-internal EV is not evidence; only paired live runs decide.

## Robustness

- 16 validation worlds across three seed panels, 16/16 ranked, worst 90.3%.
- restart/rebuild: the final runner rebuilds from snapshot+events, drops repeated events, and was
  tested with an injected interruption.
- server outages handled (two multi-minute outages in our session; runner heals).
- worst week found (vb-2): diagnosed as a Friday driver-capacity crunch (25 of 40 lates due that
  day, 23 expired gigs); 57 deadline warnings and 52 of those orders still went late.

## How it was built (agent workflow)

Two-agent workflow: archive harvest of 780+ finished level-8 worlds; fitted models for competitor
P(win), booking hazard, expected fee and P(late); ~100 deterministic A/B runs on paired seeds;
a decision log recording every idea, its measured effect and its status; a one-command final runner.

## What we would do next

- make pricing/fee schedules seed-adaptive using the fitted booking model (targets the remaining
  ~EUR1.9/order driver-fee gap and the Friday-capacity failure mode);
- validate the fee-change candidate on the partner's 24-week bank (paired, per seed);
- extend the safety controller to act on realised capacity stress earlier.

## The rules in one slide

Score = gross margin. On-time is a per-week qualification threshold: fewer than 90% of won orders on
time means the week is not ranked. Ranking rounds need all five weeks each >= 90%; rounds do not
count for the final. The final is one secret level-8 week (Sunday 08:00); the 10 ranked teams with
the highest margin present, then the jury judges the system: elegant, clear, creative, robust.

## Robustness and the near-failure week (an honest slide)

- 16 deterministic validation worlds across three seed panels: 16/16 ranked, worst week 90.3%
  on time (vb-2), best 98.4%.
- vb-2 diagnosis: 40 late/undelivered of 397 won; 25 lates due on the Friday, with 23 gigs expired
  that day; 57 deadline warnings, 52 of those orders still arrived late. It is a driver-capacity
  crunch on one day, not a set of badly priced quotes.
- Responses tested against frozen defaults on identical worlds: a projection-based safety
  controller had no measurable effect (the booking model over-estimates bookings); an early
  fee-rescue response bought +0.7 points for -EUR4.2k and was rejected; quoting only slower
  delivery options above a realised-late threshold did not trigger in time. The documented safe
  configuration (gate 0.15 + selection bar) reaches 97.2% on the calibration panel at ~EUR300-500
  per week.
- Restart robustness: the runner rebuilds from snapshot + events, drops repeated events, and the
  offline rejoin test passes all checks.

## Comparable-baseline check against the other system

- Their bank (bt-1): our frozen policy scored EUR13,579, 408 won, 98.8% on time, ranked; the
  event-ledger sum equals the score margin to the cent, so the simulator, level settings and
  accounting are comparable. Their cached per-seed baselines are not in the shared repo, so the
  full paired bank table remains open.
