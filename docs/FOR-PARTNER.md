# For the deepseek branch: what main has tested (Saturday 14 Nov)

All numbers: paired backtests on the same level-8 weeks (`scripts/backtest.py`, `scripts/frontier.py`), the
current `main` defaults as baseline. Bank = 16 unseen `bt-*` + 8 stress weeks (`st-*`, `ho-3/4`) + 20 unseen
`jury-*` weeks = 44 weeks. "Worst" = worst week's on-time share.

## ⚠️ One runner in the final

At the 14:00 test final **both bots were connected to the same team world** (`fadb6cd2…`): main's
`--final` and the branch's `final.py`. Every request got two quotes (the dealer takes the last one) and the
€1,621 result is a mix of both. On Sunday exactly one runner may be connected. Agree who runs it and from
which commit.

## On main now (each change paired-tested)

| change | effect | worst week |
|---|---|---|
| v9 archive-fitted P(late) (`late2.json`) + main's safety terms at 0.3 | +€848/wk (t=5.4, 24 wks) | 92.4 → fixed below |
| tight two-car chains (link ≤30 min, drive ≤240 min, deadlines ≤18 h apart, 0.82×) | +€197/wk | 93.4 |
| min margin €8 → €6 | +€214/wk (t=1.9) | 95.0 |
| day-capacity term at full weight inside the blend | −€228/wk, fixes an unseen 466-order week (88.6% → 91.4%) | 91.4 |
| hold 36 h → 44 h before last pickup | +€213/wk (t=2.3, 44 wks) | 95.0 |

Main on 44 weeks: ~€14,600/week, mean on-time ~97%, worst week 95.0%.

## Tested on main and rejected (so you can skip them)

| idea | result |
|---|---|
| post at the win (hold 1000) | −€988/wk on main's bot (system-dependent: it helped v9) |
| v9 lane table instead of main's pickup+delivery effects | +€41 (no difference) |
| v9 P(late) alone, no safety terms (or ×1.2) | +€843–865 but worst week 90.3–91.5% |
| fees ×0.95 (whole ladder) | +€387/wk (t=4.6) but worst week 90.5%, 2 weeks <92.5% → kept as a risk-on option |
| fees ×0.9 | −€117/wk, worst 90.9% |
| opening fee 0.8 only / supply-tiered opening fee | −€432 / −€199 |
| min margin €4 | +€223 but flu week 92.4% (−2.1 pts) |
| adaptive (load-dependent) margin bar | −€173 / −€186 |
| wider competitor prior / warm-up learners (`regime_mix`) | −€36 / −€326 |
| post at a fixed morning hour | −€438 / −€440 |
| loose chain caps (hop 35, drive 280, 0.79×) | +€302 but worst 90.9% |
| direct offers (deadline-warning, chain, viewer offers) | ~0 acceptances, −€10 to −€133 |
| λ (late shadow price) 60 | −€73 to −€544 |

## Next on main

`note_fee` 1.15 → 1.0 (fees with a note are the same as without; +€356/wk, t=3.56 on 44 weeks) under test now.
