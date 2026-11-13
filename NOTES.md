# Spread level 8 - dispatch v8 work notes (branch deepseek-skdnfakldsfb)

Goal: >97% of won orders on time AND mean margin >= EUR14k/week at level 8 (ranked),
comparable with the leaderboard top (~EUR14.7k, 98.0% on time). Baseline in the repo is
Dispatcher v7: best recorded sweep EUR11,275/week mean, 90.3-95.4% on time.

## 1. Archive harvested (no worlds created for this)

`archive.py` read the team's 210 finished, reveal-able level-8 practice worlds into
`data/L8/<worldId>/{reveal,events,snapshot,roster}.json.gz` (four practice tokens per
world, ended worlds hold no slot, no fork/advance/delete). 27 distinct seeds, 74,731 won
orders, all 210 worlds ended at minute 14,280. 5 ranking runs and live worlds were skipped.

Reproduce: `cd .worktrees/deepseek-skdnfakldsfb && . ./env.sh && python archive.py`

Key facts measured from the archive (`analyze.py`, `runs/*.json`):

- recorded v7 policy weeks: margin EUR6.4k-14.3k, 90.3-95.4% on time, 300-580 won orders.
- money per won order in v7 weeks: revenue EUR100.5, driver fee EUR71.4, refunds EUR3.0,
  penalty EUR0.3, failed visits EUR0.9 -> net EUR25.7. Margin per order is the whole game.
- late orders are not "slightly late": of 657 late orders, 419 had a gig that expired
  without a driver, 139 lost a booked driver, 128 were never delivered. A late order loses
  EUR36-46 on average; 14.5% of all orders have negative net and the worst 5% destroy 20%
  of the net. The deadline warning fires on 160 orders in 8 v7 weeks; 81% of those are late.

## 2. What the best archived worlds do (reverse-engineered)

The archive contains 15-18 runs of other policy versions on the same seeds (same request
sets, so decisions are directly comparable). The best of them:

| world      | won | on time | margin/week | driver fee/order | 2-order gigs |
|------------|-----|---------|-------------|------------------|--------------|
| w53d32d3   | 373 | 98.7%   | EUR15,653   | EUR53.4          | 119 of 270   |
| w892edb9   | 376 | 97.9%   | EUR14,733   | EUR56.3          | 117 of 275   |
| wb6b6c12   | 398 | 98.0%   | EUR15,390   | EUR56.5          | 80 of 329    |
| wbd07835   | 462 | 93.9%   | EUR16,682   | EUR76.1          | 0 of 469     |
| v7 ev-4    | 517 | 90.3%   | EUR14,308   | EUR81.6          | 0 of 522     |

1. **Pair two single-car orders into one gig** (the decisive trick). One driver collects
   car A, delivers it, collects car B near that delivery, delivers it; stops are always
   pickupA, deliveryA, pickupB, deliveryB (407/407 archived pairs). 66% of the cars in the
   best worlds move this way. The opening fee fits fee ~= 0.79*(refA+refB) (R^2 0.86),
   against 0.90*ref for each single posting: about a 20% cut of the driver fee for the pair,
   plus far fewer last-minute rescue fees. This is what turns EUR11.3k into EUR15k+.
2. **Post at once with a long lead**: the proven worlds post on the win (median hold 0 h)
   and keep the 0.9*ref opening fee for a long time; their orders almost never reach the
   steep rescue ladder.
3. **Be selective**: the best worlds decline ~26% of requests (v7 declines ~14%), mostly
   long lanes with thin pickup supply and short slack; they win fewer orders but at a much
   higher margin per order (EUR42 vs EUR27). A hard margin bar is NOT the lever by itself
   (see live sweeps below).

## 3. Live A/B sweeps (2 practice slots, approved by the user; same seed = same world)

The six archived seeds ev-1..ev-6 are reproduced exactly by `dispatch.py --prefix ev`
(v7-lam50 on ev-1 gives EUR13,365 / 455 orders / 91.0%, identical to runs/v7-lam50.json).

Per-seed mean of the 6 seeds (`runs/*.json`):

| variant | mean margin | worst on time |
|---------|-------------|---------------|
| v7-lam50 (baseline) | EUR11,275 | 90.3% |
| swA late_shadow=150 sla_target=.95 | EUR11,387 | 91.3% |
| swC late_shadow=150 sla=.96 + pessimistic p_late | EUR11,070 | 91.7% |
| swB late_shadow=150 min_margin=25 | EUR7,473 | 89.3% (too selective) |
| swE price_mult=1.15 | EUR9,738 | 95.4% |

Conclusion: tuning the hand-set constants trades margin for punctuality but cannot reach
EUR14k. The structural change is bundling (and using the fitted models for the gate).

## 4. Files on this branch

- `archive.py` - harvest finished worlds (read-only).
- `analyze.py` - archive economics / win curves / world spread.
- `bundling.py` - two-order chain feasibility + fee formula (pure, reusable).
- `dispatch_next.py` - Dispatcher v8 candidate: v7 + pair-bundling + post-at-once.
- `final.py` + `test_final_rejoin.py` - live final runner (stream, rejoin, self-healing).
- `notes_reader.py`, `supply.py`, `quote_model.py` - fitted models (in progress at writing).
- `sweep_v7.sh`, `sweep_v7b.sh` - deterministic A/B sweep scripts on seeds ev-1..ev-6.

## 5. Reproduce

```sh
cd "/Users/lewissimpson/My Small Bets/Spacing problem Bahn/.worktrees/deepseek-skdnfakldsfb"
. ./env.sh                                # SPREAD_URL, SPREAD_KEY, SSL_CERT_FILE, PATH
python archive.py                         # one-off: harvest the finished archive
python analyze.py                         # archive numbers above
# A/B sweep of a candidate policy on the six known seeds (2 live practice worlds at a time):
python dispatch.py --seeds 6 --prefix ev --parallel 2 --tag myvariant --set late_shadow=150
# live final (Sunday 07:55):
python final.py
```
