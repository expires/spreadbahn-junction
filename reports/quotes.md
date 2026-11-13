# Competitor inference and the P(win) model (level 8)

Fitted on the harvested archive: **210 finished level-8 worlds**, 217,306 price requests, 141,754
requests whose dealer-fastest option we quoted genuinely (quote < 3x list; 4x-list declines are
excluded). Whole worlds are held out for every validation number below. All artifacts are
`quote_model.py` + `data/fits/quotes.json` (coefficients; 126 KB).

## Model

    P(win | menu) = sigmoid( b * (log_comp_eff + A(features) + menu_correction - log x_fast) )
    log_comp_eff  = level + day + lane_factor + 1.028*log(1 + 0.01*trust) + adapt(dealer share)

* `b = 13.71` (logit per log price ratio on the dealer's fastest genuinely quoted option)
* menu correction: `-9.15*(log x_next_day - log x_fast) - 1.36*(log x_within_3 - log x_fast)`
  (slower options that are genuinely quoted; adding this improved held-out AUC 0.881 -> 0.889)
* trust: `exp(1.028 * log(1 + 0.01*trust))` = **1.01^trust**, i.e. the rule's 1% per counted
  order (fitted exponent 1.03 +/- 0.05; v7's `1+0.01*trust` boost is correct)
* `A` = intercept + numeric effects (ref/list, list, km, minutes, slack, car flags, note, legs,
  private) + dealer / hour-bin / offered-option-set / n-quoted / fastest-kind dummies. All in
  `data/fits/quotes.json -> A`.
* lane factor: persistent part per ordered place pair, **798 directed lanes**, sd 0.139 log,
  range -0.54 .. +0.51. Additive for trade-ins (`a>b|c>d`). 0 for an average lane; unseen lanes
  use an origin+destination+km fallback (resid sd 0.116).
* day factor: new each world-day, non-war sd 0.049 log. Price-war shift estimated -0.17 (rules
  say -0.22; the archive's own policy adapts during the war, attenuating the estimate).

### Held-out validation (36 worlds never used in the fit, deployment path)

| metric | value |
|---|---|
| AUC P(request won) | **0.8887** |
| log loss | **0.3859** |
| mean predicted vs observed win | 0.3403 vs 0.3395 |
| calibration (deciles) | within 1-3 points everywhere |
| single-option / 2-option / 3-option menus | pred 0.410/0.477/0.482 vs obs 0.391/0.490/0.513 |
| dealer option-choice calibration P(take) | pred 0.470 vs obs 0.470 |

### Implied competitor level distribution

Per-world implied acceptance threshold (world mean of the 50% point, includes the request mix and
the dealer's value of speed): **mean 1.433, sd 0.124, range ~1.22-1.74** x list. The pure world-level
state (after removing lane and request-mix effects) has **sd 0.067 in log**, which matches the rules'
uniform range 1.26-1.54 (sd 0.058). Correlation of the implied level with the world's recorded
margin: +0.30 (levels are strongly confounded with the driver shortage, so this is not a clean read).

### Price war

* War day = minimum world-day effect. Weekday counts over 210 worlds: Wed 62, Fri 46, Mon 45,
  Tue 36, Thu 21 -> effectively uniform, no weekday prior.
* War-day effect distribution: mean -0.218, sd 0.073 (core single-x model); gap to the other days
  median 0.266. So 20%-under is clearly visible in outcomes at constant price.
* Sequential detector (two day-filter hypotheses, H0 N(0, day_sd), H1 N(log 0.8, day_sd), 10:1
  evidence threshold) on held-out worlds: **78% of war days detected inside the day, median 8.5
  quotes into the day (p25 6, p75 11); false alarm on 5.6% of non-war days.** Threshold trade-off
  (replay, day-level): 3:1 -> 85% detected / 15% FA-days; 10:1 -> 70% / 1.4%; 30:1 -> 43% / 0.25%.
  Recommend 10:1 and act only by cutting prices, so a false alarm costs little.
* A system should not wait for a bootstrap: after ~6-10 quotes the day offset is identifiable
  (b=13.7, p~0.5 gives ~20 logit-units of information per quote).

## Dealer option choice

Conditional logit on won requests (`choice` in quotes.json):
`U_k = 4.56*[same_day] - 1.92*[within_3_days] - 6.34*log(x_k) - 0.292*price_eur_k`
(hold-out accuracy 0.83, chance 0.73; mean P(take|won) predicted = observed 0.470).

Read: the dealer values **same_day about 12-15 EUR above next_day**, and **within_3_days about
7 EUR below next_day**; a ~10-15 unit (EUR) premium on same_day makes it indifferent. At equal
ratios the dealer takes same_day 70%, next_day 9%, within_3 21% (3-option wins). Steering a
request to next_day therefore needs the same_day quote to be 12-15 EUR (or ~15-20% of list)
dearer than next_day.

## Online competitor / convergence

`OnlineCompetitor` keeps a joint Gaussian posterior over (world level, day factor), a Bayesian
price-war detector and per-dealer win-share state. Prior from the archive: level log mean 0.005,
sd 0.067 (relative to the fitted baseline; the absolute threshold for a typical request is
exp(level+A+lane) = 1.44 x list).

Replaying archived v7 quote outcomes sequentially, all worlds: median |level error| 0.047 after
10 quotes, 0.042 after 20, **0.029 by week end**; naive single-log SGD (lr 0.02, clamp 1.22-1.60)
ends at **0.063**. Information limit: with b=13.7 an informative quote carries ~20 logit units,
so ~10-12 quotes take the posterior sd from 0.067 to 0.05.

## Dealer-level adaptation

Not exploitable in this archive. Controlling for price, lane, world-day and trust, a dealer's
previous-day win share has a **positive** net effect on today's win rate (+0.05 .. +0.13 log comp
from lowest to highest share bin, se ~0.02), i.e. dealer-level persistence outweighs any
price-cutting where we win. We store the small fitted cumulative-share offsets (adapt table,
-0.019 .. +0.030 log) so the calibration is right, and recommend **not** repricing per dealer.

## What the old hand-set constants got wrong

Expected margin per quoted request, evaluated under the fitted true model (v7's cost model and
pricing grid; 141,754 archive quotes; "oracle level" = per-request true effective comp):

| pricing competitor model | mean x | expected margin/quote |
|---|---|---|
| v7: flat 1.37 x list (+trust boost) | 1.447 | EUR 15.68 |
| v7 + persistent lane factor | 1.451 | EUR 16.33 (+EUR 0.65/quote ~ EUR 670/week) |
| prior level + A, no online learning | 1.424 | EUR 15.74 |
| prior + lane factor | 1.429 | EUR 16.43 |
| per-request oracle level | 1.441 | EUR 18.09 (+EUR 2.41/quote upper bound) |

* **No lane factor** is the biggest single omission: 0.65 EUR/quote (~670 EUR/week, ~5% of margin).
* **Single global level with a point prior 1.37**: the true level sd is 0.067 log, and the
  clamp range 1.22-1.60 truncates it (implied levels reach 1.74). The naive SGD's week-end level
  error is 0.063 log vs 0.029 for the Bayesian filter.
* **slope 8.7**: with menu substitution modelled the price response is steeper (13.7); the fitted
  single-x slope is 9.07, so 8.7 is only mildly shallow. Effect on margin < 0.1 EUR/quote.
* **trust boost 1+0.01n** is right (fitted multiplier 1.01^trust); trust is under-exploited only
  if you do not raise prices for trusted dealers at all (worth ~1.5 EUR/quote at trust 25).
* **grid max 2.03 x list** is too low: with lane +0.51 and level 1.74 the acceptance threshold can
  reach ~2.5 x list and the optimal quote ~2.2. Extend the grid to 0.55 .. 3.0.
* **dealer adaptation** does not need modelling (see above).

## Recommended pricing rule

1. Per request: `T = exp(oc.log_level + oc.day + lane_log_factor + 1.028*log1p(0.01*trust) + oc.adapt_offset(dealer_share) + qm.A_value(ctx, n_quoted))`.
   This is the price/list ratio at which the dealer is 50/50.
2. Quote each option at `x = qm.optimal_ratio(log T + menu_adj, c)` with `c = expected fee/list`
   (include the late-refund share as `late_cost_frac`). Typical map (fee/list c):
   T=1.26 -> x 1.08/1.12/1.16/1.22; T=1.40 -> 1.20/1.22/1.26/1.32; T=1.54 -> 1.32/1.34/1.38/1.42
   for c = 0.4/0.6/0.8/1.0.
3. Menu steering: price the option you want staffed so it is 12-15 EUR dearer than the one you
   don't (use `p_take` to check the split); the dealer then takes the slower/cheaper-to-staff option.
4. War: when `oc.war_active`, subtract 0.223 from log T for the rest of the day (already inside
   `oc.log_comp`); react within the first ~10 quotes.
5. Selection: expected margin per request from this model is the right filter for the
   "fewer orders, more margin/order" target; lane factor alone separates good lanes from bad.

## API (from `quote_model.py`)

```python
from quote_model import QuoteModel, OnlineCompetitor, DAY, HOUR

qm = QuoteModel.load()                          # data/fits/quotes.json
ctx = qm.context(request, road)                 # road.km(a,b), road.minutes(a,b); dict of features
qm.slope, qm.trust_ratio, qm.prior_log_level, qm.prior_log_level_sd, qm.day_sd
qm.lane_log_factor(pickup, delivery)            # log persistent lane factor; unknown lane -> fallback
qm.lane_log_factor_key("a>b") / ("a>b|c>d")     # helper, trade-ins add their legs
qm.trust_log(trust)                             # 1.028*log(1+0.01*trust)
qm.adapt_offset(dealer_win_share or None)       # log comp shift from our past win share at the dealer
qm.A_value(ctx, n_quoted)                       # remaining fixed effects, log-comp units
qm.log_comp(ctx, level, day=0, trust=0, dealer_share=None, dealer_bias=0, war_shift=0)
qm.p_win(ctx, prices: dict[kind->cents], log_comp, n_quoted=None, var=0.0) -> float
qm.p_choice(ctx, prices) -> dict[kind -> P(dealer takes kind | won)]
qm.p_take(ctx, prices, log_comp, n_quoted=None, var=0.0) -> dict[kind -> P(won and takes kind)]
qm.p_win_ratio(ratio, lane_key=None, log_comp=0.0, features=None, trust=0, n_quoted=1) -> float
qm.optimal_ratio(effective_log_comp, cost_ratio, late_cost_frac=0.0) -> x = price/list

oc = OnlineCompetitor(qm)                       # one per world
oc.log_comp_for(ctx, trust)                     # level+day+lane+trust+adapt (+war shift when active)
oc.p_win_now(ctx, prices, trust, at) / oc.p_win(ctx, prices, trust, n_quoted=None)
oc.p_take(ctx, prices, trust) / oc.p_win_ratio(ratio, lane_key, features, trust)
oc.observe(ctx, prices, won, at, trust)         # feed outcomes in request order
oc.log_level, oc.level, oc.level_sd, oc.day_mu
oc.war_active, oc.war_probability(), oc.war_log_odds (via war_llr)
oc.dealer_share(dealer), oc.dealer_bias[dealer] = manual override
```

Caveats: `p_win` is identified for menus that genuinely quote the dealer's fastest option (v7 did
this on 98% of its non-declined requests); declining the fastest option is an extrapolation, so
keep quoting it (at a high x if you want it rejected) and use the choice model to steer.

## Files

* `quote_model.py` - QuoteModel + OnlineCompetitor (this API).
* `data/fits/quotes.json` - slope, trust ratio, menu coefficients, A, lane factor table (798),
  lane fallback, adapt table, choice model, priors, day/war stats, per-world levels and war days,
  module-level validation/replay numbers.
* `data/fits/*.log`, `data/fits/*.pkl` - fit, validation and replay logs (scratch).
