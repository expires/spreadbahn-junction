# Run log: candidate policies on the six archived seeds ev-1..ev-6

All runs are deterministic: `python quick_test.py --tag T --seeds ev-1,... --parallel 1 --set ...`.
Each row is one live practice world (2 slots at a time, user-approved); the reveal is kept in data/L8/.

Baselines (reproduced exactly from runs/*.json):
- v7-lam50: ev-1 13,365 / 91.0%; ev-2 10,995 / 94.8%; ev-3 6,386 / 95.4%; ev-4 14,308 / 90.3%;
  ev-5 13,789 / 94.5%; ev-6 8,806 / 94.8%. Mean 11,275 over 6 seeds.
- v7 module re-verified by quick_test: ev-4 14,308 / 517 orders / 90.3%; ev-1 13,365 / 455 / 91.0%.

Single-feature effects measured on the same world (dispatch_next unless stated):

| tag | config | seed | margin | on time | note |
|-----|--------|------|--------|---------|------|
| q_v7e4 | dispatch.py v7 | ev-4 | 14,308 | 90.3% | baseline |
| q_e4v7b | notes off, hold 36, no pair | ev-4 | 15,412 | 90.2% | +1.1k from bookkeeping fixes |
| qA_e4 | + notes reader windows | ev-4 | 14,371 | 89.6% | notes windows cost ~1k |
| qB_e4 | hold 0 (post at the win), no pair | ev-4 | 17,119 | 91.5% | +2.8k vs v7 |
| q_pair4 | hold 0 + bundling, ev-1 | ev-1 | 14,946 | 94.7% | +1.6k vs v7 |
| q_pair5 | hold 0 + bundling, ev-2 | ev-2 | 13,414 | 93.8% | +2.4k vs v7 |
| q6_base | hold 0 + bundling (6 seeds) | all | 10,717 mean | 88.0-96.0% | pairing hurt ev-4/5/6 |
| q6_imm | hold 0, no pairing (4 of 6 seeds) | 2/3/5/6 | 10,399 mean | 93.3-96.7% | ev-2 11.8, ev-3 8.7, ev-5 13.5, ev-6 7.6 |

The big fee bug found on the way: a default `retry_raise=0.18` inflated every reposted gig's fee
and a drop-reprice hook fired on every drop; removing both (defaults 0) took ev-4 from 12,780 to
17,119 at equal or better on-time. Lesson: never change fees on a failure by default; A/B it.

Bundling (2 orders in one gig, `bundling.py`) is worth +1.3-2.4k where it works (ev-1, ev-2),
but in its 60/480 form it is catastrophic on ev-4 (chains with median 322 drive minutes vs the
archive's 202; 24% of bundles expire). The archive's proven chain cap is effectively tighter:
link delivery->pickup median 25 min (p90 38), total drive median 118-223. Next test: link<=45,
drive<=330, deadlines within a day, and only when the supply booking model says the pair books.

Fitted models landed and are validated:
- notes_reader.py: 100% exact vs reveal truth on 28,503 won note orders (starter: 90.6-98.9%).
- quote_model.py: held-out AUC 0.889, calibrated option-choice (P(take|won) 0.470 vs 0.470),
  798 lane factors (sd 0.139 log), war detector 78% same-day after 8.5 quotes, 5.6% false alarm.
  Caution: a first live test with full menu optimisation on ev-1/ev-2 was WORSE than v7 pricing
  (13.5k / 11.5k) - the model is good but the EV optimiser needs the right cost model.
- supply.py + data/fits/supply.json: booking hazard CV AUC 0.879 (bins), fee elasticity fitted on
  2,357 random-fee gigs, expected fee MAE 0.17 x ref, lateness CV AUC 0.786.
  The shipped lateness coefficients are degenerate (standardisation not saved: predict ~0 for all
  real orders), so fit_late2.py refits the same 24 features: OOF AUC 0.789, calibrated.
- late_model.py: an independent P(late) fit on quote-time features, OOF AUC 0.686.

Offline gate trade-off with the refit P(late) on the kept runs (outstanding backlog from events):
  ev-4 qB (17,288, 7.9% late): gate p<=0.30 -> 16,700 / 6.1%; p<=0.20 -> 14,473 / 4.9%;
  p<=0.12 -> 10,049 / 3.7%.
Gating alone cannot reach 97% on time without losing 30-40% of the margin; the reliability lever
has to be operational (book risky cars earlier / direct offers), not only selection.
