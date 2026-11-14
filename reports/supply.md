# Driver supply, booking, lateness and fee models (level 8)

Fitted on the team's finished level-8 practice archive: **228 worlds, 84,978 posted gigs, 492,937
hourly gig-bin rows, 79,625 won orders, 4,325 multi-order (2-car) gigs**. Code: supply.py;
coefficients: data/fits/supply.json; supplementary tables: data/fits/supply_diag.json.
Everything is deterministic and cheap (a few thousand calls per week cost < 0.2 s).

## 1. What is in the archive and what is identifiable

* The archive spans many earlier dispatcher versions (and 6 exploration worlds), so the posting
  policy - and therefore the fee posted for a given order - is **not** exogenous. To identify the
  fee effect we use the 6 exploration worlds, in which the fee ratio of a fresh posting was drawn
  at random from ~U(0.7, 2.2) of the reference fee independently of the order (2,357 single-car
  flat-fee gigs; correlation of the drawn ratio with lead time / km / supply / hour is < 0.06).
* Everything else (supply, lead time, km, route, hour/weekday, cars, audience, weather) is fitted
  on all 84,978 gigs with the randomised fee effect as a fixed offset.
* The end-of-week snapshot contains every posted gig, including expired and withdrawn ones (all
  5,274 gig_expired events match a snapshot gig), so the booking hazard is not survivorship-biased.
* Orders the old policies declined are not observed; predictions are conditional on "an order this
  dispatcher would have won". That is the population the archive can speak about.

## 2. Supply features - build_supply(roster, road)

Per place: drivers (home count), kernel = sum over towns n_h * exp(-road_minutes(h, place)/60),
manual (manual-licence kernel), experienced (rating >= 4.8 and >= 50 gigs kernel), nearest_min,
nearest_drivers. The kernel is exactly the old dispatcher supply value.
Level-8 roster sizes are 638-946 (26-40% of a normal week) and **the only strong per-world supply
signal is the town distribution**: Helsinki 260 vs 1-5 drivers in small towns. Using the town
kernel instead of the global count improves the lateness model AUC from 0.7780 to 0.7862 (see 5).

## 3. Booking hazard - BookingModel / p_book

Discrete-time hazard on 1-hour bins:
logit h = intercept + fee_offset(fee/ref) + beta_risk*x + age dummies + hour dummies + weekday dummies.
fee_offset(r) = 2.246*log(r/0.9) - 1.882*max(0, log(r/1.5)) (fitted on the randomised-fee gigs;
fee stops buying speed above about 1.5x reference). Risk terms: 5 supply kernels (pickup, delivery,
min, max, log total drivers), route km/minutes, chain minutes, n_cars, n_orders(>1), premium,
manual/experienced audience, private, snow, lead-to-pickup, explore flag.

* Bin-level discrimination: **grouped 5-fold by world CV AUC 0.874**.
* Gig-level booked-within-1h: **AUC 0.771, mean prediction 0.551 vs observed 0.543** (calibrated).
* Longer horizons are calibrated less well: 3h 0.615 vs 0.697; 8h 0.649 vs 0.787; 24h 0.665 vs 0.886.
  Use p_book for horizons <= 1-3 h and as a ranking beyond that; 8h/24h are lower bounds. The
  reason is that the archive's late-booking tail is driven by repricing/urgency, which the
  conditional hazard does not extrapolate from the observed fee path.

### Fee vs reliability, typical single-car gig (kernel supply ~ median, route 113 min, lead 12 h)

| flat fee / ref | P(book <=1h) | P(<=3h) | P(<=8h) | expected paid fee |
|---|---|---|---|---|
| 0.70 | 0.570 | 0.709 | 0.941 | EUR 60 |
| 0.80 | 0.641 | 0.785 | 0.970 | EUR 60 |
| 0.90 | 0.699 | 0.842 | 0.985 | EUR 59 |
| 1.00 | 0.747 | 0.885 | 0.993 | EUR 64 |
| 1.10 | 0.785 | 0.916 | 0.997 | EUR 68 |
| 1.20 | 0.816 | 0.938 | 0.998 | EUR 72 |
| 1.30 | 0.842 | 0.955 | 0.999 | EUR 77 |
| 1.50 | 0.880 | 0.975 | 1.000 | EUR 86 |
| 2.00 | 0.891 | 0.980 | 1.000 | EUR 114 |

**Key result: 1-hour booking probability saturates at ~0.88 even at 2x reference.** Paying more than
~1.3-1.5 buys only a few points; the big levers are lead time, supply and bundles.

### Fee ratio needed for P(booked) targets (fitted, fee_for_target)

Within 8 hours (single car, typical route):

| supply kernel at pickup | lead <0 h | lead 0-2 h | lead 6-12 h | lead 12-24 h | lead >24 h |
|---|---|---|---|---|---|
| 33 (worst): 50 / 80 / 90% | 0.58 / 0.88 / 1.08 | 0.42 / 0.64 / 0.79 | 0.40 / 0.54 / 0.65 | 0.40 / 0.56 / 0.68 | 0.40 / 0.58 / 0.71 |
| 228 (median): 50 / 80 / 90% | 0.55 / 0.85 / 1.03 | 0.40 / 0.62 / 0.76 | 0.40 / 0.51 / 0.62 | 0.40 / 0.53 / 0.64 | 0.40 / 0.56 / 0.68 |
| 340 (best): 50 / 80 / 90% | 0.50 / 0.76 / 0.92 | 0.40 / 0.55 / 0.68 | 0.40 / 0.46 / 0.56 | 0.40 / 0.48 / 0.58 | 0.40 / 0.50 / 0.61 |

Within 1 hour, 90% is **not reachable** for most (supply, lead) cells (would need > 2.4-3x ref);
80% in 1 h needs 1.0-1.5x ref in good supply and > 2x in poor supply/short lead. The full cell grid
is in data/fits/supply_diag.json.

### 2-order bundles (gig_features with n_cars=2, n_orders=2, chain_min; fee denominator = refA+refB)

In the 60 archive worlds that post multi-order gigs (4,325 two-order gigs):

| | booked ever | <=1 h | <=3 h | <=8 h | median time-to-book |
|---|---|---|---|---|---|
| 1-order gig | 89.9% | 59.5% | 72.9% | 80.6% | 14 min |
| 2-order gig | 94.0% | 65.7% | 78.4% | 84.3% | 11 min |

The same-world fee medians differ (bundle ratio ~0.65-0.76 x (refA+refB) vs 0.90 x ref for single),
so the hazard model (with n_orders2 coefficient **+1.67**, chain_min100 +0.55, route_h -1.02)
is used for a like-for-like comparison. Model, typical 2-order bundle (route 218 min, chain 25 min,
sum ref EUR 107): fee ratio 0.50 -> P(<=1h) 0.61, 0.65 -> 0.74, 0.80 -> 0.82; **ratio needed for 90%
within 8 h is 0.41x (refA+refB)** versus 0.71x for a single car - i.e. bundling is worth a ~30%
fee discount per reference euro. The fitted +1.67 is partly selection (policies bundle chainable
pairs), so treat the model-implied bundle advantage as an upper bound; the raw evidence (65.7% vs
59.5% <=1 h at a lower fee ratio; 94.0% vs 89.9% ever booked) is robust in direction.

Bundle economics per won order: orders carried in 2-order gigs pay **EUR 41 mean driver fee** vs EUR 76
for 1-order gigs (0.78 vs 1.13 x own reference fee), and are late 4.6% vs 6.9%. Bundling cuts the
driver cost per car by about 45%.

## 4. Lateness - LatenessModel.p_late(features)

Logistic on quote-time features, 79,625 won orders, 5-fold grouped-by-world CV:

* **CV AUC 0.786**, Brier 0.0549 (base late rate 0.0669) - versus the current hand-set p_late step
  table, which scores **AUC 0.562 / Brier 0.0627** on the same orders.
* Overall in-sample calibration: 0.0668 predicted vs 0.0669 observed.

Calibration by bucket (predicted vs observed late rate):

| quote slack (h) | n | pred | obs | pickup supply kernel | n | pred | obs | backlog orders | n | pred | obs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.3-24 | 9984 | 0.143 | 0.142 | 4-72 | 9977 | 0.134 | 0.127 | 0-5 | 1086 | 0.001 | 0.008 |
| 24-27 | 9949 | 0.065 | 0.067 | 72-128 | 9961 | 0.094 | 0.094 | 5-15 | 2276 | 0.004 | 0.010 |
| 27-30 | 9937 | 0.056 | 0.062 | 128-206 | 10117 | 0.072 | 0.071 | 15-40 | 5651 | 0.011 | 0.029 |
| 30-32 | 9970 | 0.037 | 0.027 | 206-244 | 10886 | 0.061 | 0.067 | 40-100 | 13440 | 0.032 | 0.041 |
| 32-52 | 9956 | 0.050 | 0.045 | 244-272 | 9088 | 0.054 | 0.048 | 100+ | 57172 | 0.084 | 0.080 |
| 52-74 | 9954 | 0.052 | 0.062 | 272-290 | 10849 | 0.045 | 0.047 | | | | |
| 74-91 | 9922 | 0.050 | 0.052 | 290-322 | 8863 | 0.039 | 0.043 | | | | |
| 91-175 | 9953 | 0.082 | 0.078 | 322-372 | 9884 | 0.034 | 0.036 | | | | |

Per-world predicted late share vs recorded: **Pearson 0.730, Spearman 0.676** over 228 worlds.
Worst misses: the starter-like world w1024... (949 orders, pred 0.154 vs actual 0.542 - that world
won everything with no gating); next: -0.156, -0.146, -0.145, -0.140. Most worlds are within 0.10.

Driver-fee cost of lateness: late orders cost **EUR 127 mean driver fee vs EUR 69 on-time** - the urgent
repricing/reposting spiral roughly doubles the fee, before refunds.

### (a) What actually drives P(late): supply vs slack vs km vs backlog

Feature-block GLM, CV by world: all 0.786 / no-supply 0.778 / slack-only 0.579 / supply-only 0.642 /
no-backlog 0.761. So:

* **Our own outstanding backlog is the second-strongest signal** (delta AUC 0.025). Coefficient
  log1p(backlog) = +1.58: 0 -> 40 outstanding raises the odds ~x7 (about +0.03 to +0.08).
* **Slack dominates in the tail, not on average.** 1/(1+slack_h) coefficient +14.9: 2 h slack is
  ~0.9 late for same-day work; 8 h ~0.3; 20-30 h ~0.07; 40 h+ ~0.04-0.08. The old step table
  overstates 4-8 h risk (0.6 vs observed 0.44 at 4-5 h) and understates >90 h risk (0.03 vs 0.08).
* **Town supply matters much more than the global driver count**: 4-72 kernel ~12.7% late vs
  322-372 kernel ~3.6%; replacing the town kernels with log(total drivers) loses the whole
  supply gain (AUC 0.778 vs 0.786).
* **km is not monotone in risk** as a single linear term (CV-fitted coefficient is negative, because
  long lanes get higher fees/more slack); observed late rate is U-shaped: 0.07 at <50 km, ~0.05 at
  100-250 km, 0.138 at >400 km. Keep a km cap/gate rather than a linear penalty.
* Other quantified risks: premium car 15.1% late (vs 6.3%), private address 9.0% (vs 6.3%), note
  8.4% (vs 5.8%), snow-forecast exposure 11.7% (vs 6.3%), electric 8.6% (vs 6.3%);
  day-of-week +0.035 Monday to +0.09 Thursday/Friday; bundles 4.6% (vs 6.9%).

## 5. Expected driver fee - DriverFeeModel.expected_fee_cents

Ridge regression of log1p(paid / own reference fee) on schedule/order features, fitted on 78,180
orders that paid a driver. R2 = 0.582, CV MAE 0.17 x reference fee (~EUR 13 at the median EUR 75 ref).
Coefficients: opening fee ratio +0.319; 2-order gig -0.116; chain minutes +0.070/100 min; town
supply small; slack ~0. Realised mean paid ratio 1.12 for 1-order gigs, 0.78 for orders in
2-order gigs. Note the target is per **order** (its own reference fee share), so for a bundle pass
that order's ref share to ref_cents, not the sum.

## 6. Top quantified levers for dispatch v8

1. **Gate on p_late x price, not on slack alone** - lateness risk is 3.5x higher in the bottom
   supply decile (12.7% vs 3.6% late) and 0.078 vs 0.001 calibrated per order; the old step table
   ranks far worse (AUC 0.562 to 0.786). A 20% late probability is worth ~0.45 x price + trust risk.
2. **Pass the outstanding-orders pressure to order_features.** Backlog 40+ multiplies risk; the
   same order is safe at backlog 5 and dangerous at backlog 50. This is the signal that lets v8
   throttle winning when the fleet is full - the main seed-spread and late-orders cause.
3. **Bundle 2 orders whenever the chain drive is short.** At the same fee per reference euro, a
   2-order gig books faster (65.7% vs 59.5% <=1 h) and 90% reliability costs 0.41x vs 0.71x of the
   reference fees. Observed bundle policies pay EUR 41/order vs EUR 76 - ~45% lower driver cost.
4. **Fees above ~1.3-1.5x reference barely improve booking** (1 h probability saturates at 0.88).
   The reliability curve says pay more only below 1.2; the real fix for risky orders is posting
   earlier (lead >= 6 h) or declining.
5. **Supply is a town-level quantity, not a world-level one.** Roster kernel split (pickup/delivery)
   carries the signal; the global driver count does not (AUC 0.778 vs 0.786).
   Also: late orders cost ~2x the driver fee of on-time orders, so gating is margin-positive even
   before refunds and trust.

## 7. Caveats

* Hazard/lateness models are fitted mostly on policies that differ from v8; conditional-on-features
  calibration is good, but absolute levels can shift if v8's order mix changes (recalibrate the
  lateness intercept on the first days of a live week if possible).
* The 8h/24h booking curve is a lower bound (see 3); use <=3 h predictions for staffing decisions.
* Bundle hazard coefficient is partly selection; direction robust, magnitude likely overstated.
* Everything here is traceable to data/fits/supply.json; refit with python data/fits/dev/fit_models.py.
