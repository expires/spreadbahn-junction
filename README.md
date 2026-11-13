# alignas — Spread dispatcher

A dispatcher for Bahn's *Spread* challenge: one simulated week of a car-transport company in Finland. It quotes dealers against a hidden competitor, posts drives to freelance drivers, and must deliver ≥ 90% of won cars on time. Score = gross margin.

## Run it

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install https://spread.bahnexpress.com/kit/spread-0.1.0-py3-none-any.whl numpy pandas scipy scikit-learn
cp .env.example .env   # SPREAD_URL, SPREAD_KEY (never commit the key)
. ./.env

python dispatch.py --seedlist nw-1,nw-2 --tag test --parallel 2   # practice backtest
python dispatch.py --board --levels 1,2,3,4,5,6,7,8              # this round's ranking runs, all levels
python dispatch.py --final                                       # live final / test final (rebuilds from the server on any restart)
python stress_scan.py --n 40                                     # find the nastiest weeks (bulk orders, snow, thin yards)
```

## How it works

One idea runs through the system: **every decision is an expected-value trade-off, with the 90% on-time rule priced in as a shadow cost.**

| Decision | Method | File |
|---|---|---|
| Which requests to take, at what price | `argmax_p (p − E[cost] − P(late)·(refund + λ)) · P(win \| p)` per delivery option | `dispatch.py` `quote` |
| P(win \| p) | Logistic in log(price/competitor); competitor = list × **persistent lane effects** (fitted on 59 worlds, transfer weight 1.00 on held-out worlds) × **Bayesian day level** with price-war mixture and outage detection × dealer-trust boost | `regime.py`, `lane_effects.json` |
| P(late) | Slack table × driver-supply logistic (distance-weighted resident drivers around pickup/delivery, fitted AUC 0.72) + per-(town, day) congestion + snow forecasts, calibrated ×0.6 | `dispatch.py` |
| SLA guard | Live controller: projected late share (late + at-risk orders) raises the margin bar and λ | `sla_pressure` |
| Driver fees | Hold each order until 36 h before its last safe pickup, open at 0.9× typical fee, step up at fixed points before `lastPickupAt` (no hourly raises: drivers learn and wait) | `fees_for`, `schedule` |
| Multi-car gigs | Pair an order with a return-trip partner (B starts near A's drop-off, ends near A's start) into one gig at 0.75× the summed typical fee | `find_partner`, `post_pair` |
| Dealer notes (fi/sv/en) | Rule-based parser → ready time, real hours, tighter deadline, plates, manual, summer tyres; 0 dangerous errors on held-out notes vs 415 for the kit | `notes.py` |
| Robustness | Server is the source of truth: on any crash/restart the desk rebuilds from events + snapshot; ranking runs resume instead of being abandoned | `rebuild`, `run_final`, `run_board` |

## How we got here (level 8, mean margin per week on fresh seeds)

| Step | €/week | on-time | Evidence |
|---|---|---|---|
| Kit starter | −72,600 | 45% | baseline |
| v1 expected-margin quotes + slack fees | 5,384 | 77–90% | |
| + dealer trust, unbiased competitor learning, SLA fix | 11,457 | | |
| + supply model, late shadow price λ=80 | 12,010 | 6/6 ranked | λ sweep |
| + congestion (bulk orders) | | | fixed `ev-4` |
| + lane price effects | +4.8% | | 8 fresh seeds, t≈2.3 |
| + return-trip pairing | +10.5% | unchanged | t>3 |
| + note parser | +€798 | failed visits 58→1 | t=3.7 |
| + P(late) calibration ×0.6 | +€540 | | t=2.1 |
| + regime learner (price war, outage) | +€546 | | t=2.2 |
| **current** | **≈ 13,400** | 94.7–97.4% | stress suite: 6/6 ranked, mean €13,620 |

Every change is A/B-tested on the same seeds (paired differences), on seeds never used for fitting, and on a stress suite of the worst weeks found by `stress_scan.py`.

## Repo map

- `dispatch.py` — the dispatcher, runners (`--board`, `--final`, sweeps)
- `regime.py` — competitor level learner, price-war and outage detection (+ `regime_eval.py`, `regime_data.py`)
- `notes.py` — dealer-note parser (+ `eval_notes.py`)
- `explore.py`, `fit.py` — randomized exploration runs and first model fits
- `stress_scan.py` — cheap scan of seeds for stress tests
- `test_restart.py` — kill-and-rebuild test
- `runs/` — every experiment's per-seed results (JSON) and logs
