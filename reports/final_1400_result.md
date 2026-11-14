# 14:00 test final — result, diagnosis, next experiment

World `fadb6cd2bc34303e35ce8`; frozen v9 `final.py` @ `bdff163`; shipped defaults; 14:00–14:18:42 EET.

## Result

| metric | ours | top 10 range |
|---|---:|---:|
| margin | **EUR 1,621.08** | EUR 3,291.51–4,022.84 |
| rank | **27 / 217** | 1–10 |
| won orders | **77** | 135–167 |
| on time | **98.7%** | 96.6–100% |
| undelivered | **0.0%** | 0–0.7% |
| revenue | EUR 5,770.64 | — |
| margin share | 28.1% | 31.3–39.1% |

Runner health: **495 actions sent, 0 dropped events, 0 rebuilds, 0 handler errors, 0 safety quotes,
0 held-order timers**; all 417 requests in the live window were quoted, none missed. This was a clean
clock rehearsal; the gap is policy, not liveness.

## Diagnosis

The final starts Thursday 00:00. Of 1,031 requests in the reveal, **417 were live** (Thu 238, Fri 179).
All 417 got a quote action. But:

- **291 were real bids; 126 were all-declines** (every option at 4x list).
- 78 wins from 291 real bids = **26.8% win rate** (18.7% of all live requests).
- Declines skew small: median reference fee **EUR 36.99** vs **EUR 64.52** for real bids; list/ref is
  the same (~1.1). The EUR 8 absolute `min_margin` and the lane/gate filters are leaving small jobs.
- 12 of 65 booked gigs were paired (24 of 77 orders, **31% of orders**), so pairing is working, not absent.
- Revenue/order **EUR 74.94**, margin/order **EUR 21.05**, cost/order **EUR 53.89**.
  Top 10: revenue/order ~EUR 65, margin/order ~EUR 21.5–25.5, cost/order ~EUR 43.

**Price cuts alone do not pay.** A logistic fit on the 291 real bids (P(win) vs log min quote/list)
gives slope −3.82 and a 50% win ratio of ~0.975x list. Predicted wins: 78 now; 89 at −5%; 101.5 at
−10%. At −5% the extra win margin (~EUR 17.25) gives ~EUR 1,535 total, below the current EUR 1,621;
at −10% (~EUR 13.5/win) it is ~EUR 1,364. The gap is the **cost per order**, not the quote level.

## Next experiment

**A/B `fee_open=0.85` (or `fee_step_scale=0.9`) on the same 8 practice seeds, frozen v9 vs variant.**
- Why: cost/order is EUR 53.89 vs top-10 ~EUR 43; on-time is 98.7% with 0 undelivered, so there is
  fee headroom. A 5–10% fee reduction is worth EUR 3–6/order at current volume and may also let the
  policy price lower without losing margin.
- Guard: on-time >= 94% mean, no seed below 92%; total margin must rise. If it fails, revert.
- Secondary, only after the fee test: `min_margin=3` to bid the small-job declines (expected smaller,
  ~EUR 150–250, but it targets the 126 all-declines).
- Do not ship a price multiplier: the fitted curve says it buys volume at a net margin loss.

**Needs explicit user approval to create the 8 practice worlds.** No world was created or deleted for
this analysis; the final world is read-only and has ended.
