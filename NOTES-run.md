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

## Final candidate (all defaults in dispatch.py) and fresh-seed validation

q6_final = hold 0 + conservative bundling (link<=35, drive<=280, deadline gap<=18 h)
          + supply P(late)/expected fee + gate_pl=0.30, six archived seeds, 2 parallel:

  ev-1 15,102 / 434 / 95.4%   ev-2 12,199 / 435 / 94.9%   ev-3 9,623 / 297 / 98.0%
  ev-4 17,044 / 495 / 90.7%   ev-5 13,520 / 393 / 96.4%   ev-6 7,948 / 275 / 94.9%
  MEAN EUR12,573  worst on time 90.7%  ranked 6/6        (v7: EUR11,275 / 92.4% / 6/6)

Fresh seeds (never used for tuning), v7 vs final, paired:

  vb-1 12,379/89.1% -> 14,353/92.5%     vb-2 7,708/82.1% -> 7,763/85.1%
  vb-3 10,701/97.6% -> 11,387/95.9%     vb-4 14,883/96.2% -> 16,107/96.0%
  MEAN EUR11,418 -> EUR12,402 (+8.6%), ranked runs 2/4 -> 3/4

Shipped defaults reproduce q6_final exactly: quick_test --seeds ev-1 with no overrides gives
EUR15,102 / 434 orders / 95.4%.

Gate threshold A/B (same config): 0.20 vs 0.30 vs 0.45
  ev-1  15,253/95.2 (gated 90)   15,102/95.4 (gated 50)   15,076/95.3 (gated 37)
  ev-4  16,442/89.1 (gated 61)   17,044/90.7 (gated 34)   17,017/89.0 (gated 9)
  vb-2   8,207/86.4 (gated 35)    7,763/85.1 (gated 21)    7,423/84.3 (gated 7)
0.30 chosen: best on the hard ev-4, close elsewhere.

Direct offers (offer_h=8, no bundling):
  ev-4 15,447/90.8 -> 15,425/90.2;  ev-2 11,791/93.3 -> 11,710/94.2  (neutral, off by default)

Rejoin/loop test after promotion: final.py preflight PASS on the live server (404 poll path,
no world created). test_final_rejoin.py replays archived worlds through dispatch.py; the only
changed expectation is the held-order-timer check (v8 has hold_hours=0 so it has no held timers).

## Models actually active (bug found and fixed)

Until 02:50 the fitted supply models were silently OFF: Dispatcher.__init__ referred to an undefined
'p' inside its try blocks, so LateModel/QuoteModel/supply loading always fell into the except
branch and self.smodels stayed None. The q6_final numbers above are therefore the hand-table
variant (still valid as a measured config). Fixed by 'p = policy' in __init__.

With the fitted P(late) active (use_supply_model=1, use_supply_fee=0 so the fee stays v7's hand
formula), same defaults otherwise:

q6_v8b (gate 0.30):  ev-1 15,756/94.3  ev-2 13,473/95.7  ev-3 9,815/97.8  ev-4 19,978/94.2
                    ev-5 14,260/94.7  ev-6 9,056/95.6   MEAN EUR13,723  mean ot 95.4%
q6_v8c (gate 0.15): ev-1 16,087/97.3  ev-2 13,288/95.0  ev-3 9,737/97.7  ev-4 19,038/95.4
                    ev-5 13,745/95.6  ev-6 9,089/95.5   MEAN EUR13,497  mean ot 96.1%  worst 95.0%
                    6/6 ranked.
Gate 0.15 vs 0.30: -226 EUR margin for +0.7 points on time. 0.10 is worse on both for ev-1/ev-4
(14,170/97.1 and 16,831/96.1): more gating did not buy more punctuality, so 0.15 is the knee.

Feeding the fitted expected FEE into the margin (use_supply_fee=1) made the policy too
aggressive: ev-1 13,926/93.2, ev-2 11,265/91.7, ev-4 18,700/92.9 - better only on ev-4. Off.

price_mult / late_shadow on top of gate 0.15:
  price_mult=1.04: ev-2 13,907/95.9 (+619/+0.9), ev-4 18,644/95.8 (-394/+0.4)
  late_shadow=150: ev-2 13,256/96.0, ev-4 17,966/94.7 (worse on ev-4)
The qm-based request-level selection bar (select_qm) is BROKEN in the tested form: with qm loaded
for selection it declined the easy orders and kept the hard ones (ev-1 4,708/84.1, ev-4 -5,538/73.9
with 120 expired gigs), so it stays off.

## Frontier (all with gate_pl=0.15, conservative bundling, hold 0, safe notes)

q6_pm4 = price_mult 1.04:            mean EUR13,513  mean ot 96.24%  min 95.4%  (ev-1 15,984/97.7)
q6_pm8 = price_mult 1.08:            mean EUR13,368  mean ot 96.87%  min 96.0%
q6_feem = pm 1.08 + fee model:       mean EUR12,876  mean ot 97.34%  min 95.6%
q6_comb = pm 1.08 + fee + offers:    mean EUR12,723  mean ot 97.48%  min 96.5%
(for reference: q6_v8c gate .15 pm 1.0 = EUR13,497 / 96.07%; q6_v8b gate .30 = EUR13,723 / 95.37%.)

So the frontier is: ~EUR13.5k at 96.1-96.9% on time, or ~EUR12.7-12.9k at 97.3-97.5%. The remaining
task is to lift margin at fixed punctuality; the levers measured so far each move one of the two.

Sub-levers measured on ev-2/ev-4 (paired, all on top of gate 0.15 + pm 1.08):
- use_fee_model (opening fee from supply.booking.fee_for_target, target .85 within 8h):
    ev-2 14,475/96.4 -> 14,696/98.4 (+221/+2.0), ev-4 17,617/96.0 -> 17,756/96.2 (+139/+0.2).
- tuned direct offers (offer_h 12, fee x1.15, radius 120min):
    ev-4 17,617/96.0 -> 18,078/97.0 (+461/+1.0); ev-2 14,475/96.4 -> 14,208/96.2 (-267/-0.2).
- looser bundle caps (link 40, drive 320): ev-4 17,737/95.9 (neutral), ev-2 13,594/96.0 (worse than 14,475).
- price_mult 1.12: ev-4 19,322/96.5 (+1,705/+0.5) but ev-2 13,663/95.3 (-812/-1.1): the seed's
  competitor level decides whether more price helps, so a blanket multiplier is the wrong tool.
- comp_from_qm (use quote_model's online lane-aware threshold as v7's comp): inverted the order
  selection (ev-1 4,708 / 84.1% / 49 expired gigs) - dead end in this form.
- select_qm (request-level EV bar with the fitted model) was also catastrophic in both forms tried.

The fee model + offers together buy ~+0.6 points of on-time for ~EUR600/week. Since the mission
wants >97% AND EUR14k, the next real step is a better cost/selection model (see NOTES.md section 9),
not another constant tweak.

## Best combined configs (6 archived seeds, deterministic)

q6_pm4fee = gate_pl 0.15 + price_mult 1.04 + use_fee_model 1 (no offers):
  ev-1 14,902/96.6  ev-2 14,956/97.8  ev-3 8,640/97.8  ev-4 18,663/97.2
  ev-5 14,524/97.6  ev-6 9,058/97.8
  MEAN EUR13,457  mean on-time 97.48%  worst 96.6%  ranked 6/6  <- first config over 97%

Gate 0.20 vs 0.15 on top of that (single seeds): ev-1 15,378/96.3 (+476/-0.3),
ev-4 19,081/95.3 (+418/-1.9); price_mult 1.06 on ev-4 15,774/93.1 (chaotic regression vs
18,663/97.2 at 1.04) - the win/trust/learning path is sensitive at these price levels, which is
why each candidate is re-run over all six seeds before it is believed.

q6_ship1 = gate 0.20 + pm 1.04 + fee model: mean EUR12,921 / 96.07% / min 95.2 - worse than
q6_pm4fee on BOTH axes (chaotic sensitivity on ev-2/ev-5/ev-6), so the shipped default is:

    gate_pl=0.15  price_mult=1.04  use_fee_model=1  + the rest of the v8 defaults
    (post at the win, conservative two-car bundling, safe note mode, supply P(late), no offers)

    six archived seeds:  mean EUR13,457/week   mean on-time 97.48%  worst 96.6%  ranked 6/6
    v7-lam50 reference:  mean EUR11,275/week   mean on-time 92.4%

## Generalisation check on six unseen seeds (vb-1..vb-6)

The ev panel was used to tune, so every serious candidate is re-run on seeds never used for a
decision. Results (mean margin / mean on-time / worst on-time):

    v7-lam50 (4 of the seeds)      EUR11,418 / 91.3% / 82.1%
    shipped (gate .15 pm1.04 fee)  EUR12,675 / 95.5% / 90.9%
    v8b      (gate .30 pm1.00 hand) EUR14,316 / 94.5% / 87.4%   <- best margin, one unranked seed
    pm8      (gate .15 pm1.08 hand) (running)

So the margin gain from the ev-tuned constants does not fully generalise: on the unseen seeds the
simpler, less aggressive v8b (gate 0.30, no fee model, no price multiplier) makes ~EUR1.6k/week
more than the shipped ev-tuned set, at ~1 point less on-time and with more ranking risk. Gate 0.20
variants are being measured now to find the knee. This is the main open decision.

## Shipped configuration verified with no overrides (g20 family)

quick_test.py --seeds ev-1..ev-6 with shipped defaults (gate 0.20, hand fee, price 1.0,
conservative bundling, safe notes, hold 0, supply P(late)):

  ev-1 15,784/96.7  ev-2 13,537/96.1  ev-3 9,328/97.1  ev-4 19,334/95.5
  ev-5 14,801/96.8  ev-6 9,383/96.2   MEAN EUR13,695  mean ot 96.4%  worst 95.5%  ranked 6/6

Unseen panels with the same defaults: vb-1..6 EUR14,080/95.2% (worst 89.1), vc-1..4
EUR12,467/95.2% (worst 92.7). Across 16 worlds: EUR13,532 mean, 95.7% on time, 15/16 ranked.

Frontier recap (6 archived seeds): gate .15 + pm1.04 + fee model = EUR13,457 at 97.48%;
gate .30 hand = EUR13,723 at 95.37; shipped gate .20 hand = EUR13,695 at 96.4. Unseen-seed
margin favours the simpler gate .20/.30 configs (EUR14.1-14.3k on vb), while the fee+pm config
drops to EUR11.4-12.7k unseen; that is why the shipped defaults are the simpler set.

## Root cause found: quote() routed to the fitted-menu path whenever a model was loaded

Earlier attempts that loaded QuoteModel/OnlineCompetitor for selection (select_qm, comp_from_qm,
lane_gate, net selection) all looked catastrophic (margin -5k to +4k, 45-123 expired gigs). The
cause was in Dispatcher.quote(): it called _quote_model() whenever qm and oc were loaded, NOT only
when Policy.use_quote_model was set. Every "model-loaded" experiment therefore silently ran the
unvalidated fitted-menu pricing path. Fixed: the _quote_model path now requires use_quote_model=1.

After the fix, re-running the same levers changes the picture completely:

  comp_from_qm=1  (v7 prices but the competitor threshold is quote_model's online lane-aware
                   estimate exp(log_comp_for(ctx, trust) + A_value(ctx, n_options)))
  ev-1 15,784/96.7 -> 17,881/97.2 (+2,097/+0.5)
  ev-2 13,537/96.1 -> 14,690/95.7 (+1,153/-0.4)
  ev-4 19,334/95.5 -> 21,925/96.2 (+2,591/+0.7)

That is the ~EUR0.65/quote lane-level lever from the competitor report, and it is the biggest
margin change since post-at-win. Full six-seed validation running.

Gently-selected net bar (fitted expected-net model, select_net=5 EUR):
  ev-1 16,018/97.5 (+234/+0.8), ev-4 19,204/95.5 (-130/0.0) - small but non-negative.
  select_net=15 was too aggressive after the routing fix (ev-1 11,945/99.2, ev-4 17,132/95.9:
  margin cost larger than the punctuality gain); 5 or lower is the useful range.

The fitted expected-net model (select_model.py) on 202k won orders / 589 worlds: OOF R2 0.412,
MAE 21.2 (base 29.4), decile calibration excellent: bottom decile predicted -6.8 vs realised
-15.7; top decile 80.7 vs 87.5. So it ranks orders well; used softly.

## Lane-aware pricing (comp_from_qm) validated with no overrides

Six archived seeds (gate 0.20, hand fee, price 1.0, conservative bundling, safe notes):
  ev-1 17,881/97.2  ev-2 14,690/95.7  ev-3 10,188/97.6  ev-4 21,925/96.2
  ev-5 14,191/95.1  ev-6 10,046/96.8
  MEAN EUR14,820  mean on-time 96.43%  worst 95.1%  ranked 6/6
(previous shipped defaults without lane-aware pricing: EUR13,695 / 96.42%. +EUR1,125 margin for
same punctuality; this is the competitor report's ~EUR0.65/quote lane lever realised.)

Six unseen seeds vb-1..6: MEAN EUR14,722 @95.16% (worst 88.8% on vb-2; the per-seed results are
15,853/10,670/11,576/16,887/18,603/14,741). vs shipped defaults on the same panel EUR14,080/95.19:
lane-aware pricing generalises on the unseen seeds too (+EUR642).

Gate 0.15 on top: MEAN EUR14,462 / 96.55% (worst 95.8): costs EUR358 for +0.12 points - not the
way to 97%. Direct offers on top: ev-4 21,768/95.6, ev-5 14,198/95.1 - neutral/worse, off.
Fee floor at target 0.7/horizon 12 h was a no-op (the model never asks for a fee above 0.9 x ref
at those targets); stronger targets are the thing to try if it is needed for punctuality.

## Both targets reached on the archived panel: cqm + gate 0.15 + select_net=5

  ev-1 17,059/98.4  ev-2 14,484/95.6  ev-3 9,568/97.8  ev-4 20,286/97.2
  ev-5 15,396/96.0  ev-6 10,348/98.3
  MEAN EUR14,524/week  mean on-time 97.22%  worst 95.6%  ranked 6/6   (expired gigs now 2-8/world)

That is the first configuration to clear both mission thresholds on the calibrated panel
(>= EUR14k and >97%). It is lane-aware pricing (comp_from_qm) + P(late) gate at 0.15 + a light
fitted-net selection bar of EUR5. Generalisation check on vb/vc running now.

## Shipped v9 configuration (both targets on the calibrated panel)

  comp_from_qm=1 (lane-aware competitor threshold) + gate_pl=0.15 + select_net=5 + the rest of
  the v8 defaults (post at win, conservative bundling, safe notes, fitted P(late) margin).

Calibrated panel ev-1..6 (no overrides): 14,524 mean EUR/week, mean on-time 97.22%, worst 95.6%,
all ranked. Per seed: ev-1 17,059/98.4; ev-2 14,484/95.6; ev-3 9,568/97.8; ev-4 20,286/97.2;
ev-5 15,396/96.0; ev-6 10,348/98.3.

Unseen panels with the same config: vb-1..6 13,072 mean / 96.52% (worst 91.1, all ranked);
vc-1..4 12,519 mean / 97.04% (worst 96.3). All 16 worlds: 13,478 mean / 96.90%, 16/16 ranked.

Frontier knobs measured today:
  comp_from_qm=1 gate 0.20 select 0: ev 14,820/96.43, vb 14,722/95.16  (margin side of the trade)
  comp_from_qm=1 gate 0.15 select 5: ev 14,524/97.22, vb 13,072/96.52, vc 12,519/97.04 (shipped)
  middle settings do not interpolate: gate .18/select 4 on ev-1 16,613/95.9 and ev-4 21,136/95.6
  (worse than both ends), gate .20/select 5 vb-2 8,021/89.1, gate .15/select 5 vb-2 7,701/91.1 -
  the win/trust/driver-feedback dynamics are chaotically sensitive, so every candidate is re-run
  on all six seeds before it is believed.

## v9 verification (committed defaults, no flags)

  final.py pre-flight: PASS (Dispatcher + Policy built with comp_from_qm=1, gate 0.15, select 5;
  answered a synthetic price request; GET /v1/final polled 404, no world created).
  test_final_rejoin.py --worlds 1 --skip-loop: ALL CHECKS PASSED (replay + rejoin identical,
  every request answered in its window, repeat dropped, interruption healed, fallback works).
  quick_test with no overrides: ev-1 16,707/98.7%, ev-4 20,286/97.2% (mean EUR18,496, 2/2 ranked).

## Round-5 mission (on-time bar 92%, maximise money): gate 0.30, selection off

  comp_from_qm=1 + gate_pl=0.30 + select_net=0, everything else v9 defaults.

  ev-1..6: 15,042 mean / 95.92% (per seed 17,787/14,619/10,367/23,764/14,092/9,621)
  vb-1..6: 14,874 mean / 95.27% (worst 90.3 vb-2, still ranked)
  vc-1..4: 13,079 mean / 95.01% (worst 94.3)
  ALL 16:  14,488 mean / 95.45% / min 90.3 / 16 ranked

=> clears both round-5 bars (>= EUR14k mean and >92% on time) on every panel while being the
highest-margin setting measured so far. Screen results that led here (same code):
  gate 0.15 + select 5: ev 14,524/97.22 (punctuality side)
  gate 0.20, no select: ev 14,820/96.43, vb 14,722/95.16
  gate 0.30, no select: ev 15,042/95.92, vb 14,874/95.27, vc 13,079/95.01  <- shipped
  gate 0.40 / off (ev-1, ev-4 only): 17,335/95.9 and 22,887/94.9; 17,359/95.9 and 22,989/95.8 -
  the two ends do not beat 0.30 on both seeds, but a full-panel test of 0.40 is the next run.
  late_shadow 40: ev-1 16,608/95.0, ev-4 21,324/94.0 (worse); price_mult 1.06: 14,482/95.3,
  21,186/96.0 (worse). Both rejected.
