# Ablation: what each idea is worth (final configuration, 20 unseen level-8 weeks)

Full system vs the full system minus one idea, on the same 20 weeks (seeds jury-1..20, never used for tuning).
Paired differences; t = mean difference / standard error. Produced by scripts/ablate.py.

```
idea                                                €/week  Δ vs full      t  on-time mean/min  ranked
Full system                                         14,363         +0    nan    96.8% / 91.4%   20/20
− persistent lane prices                            14,319        -44   -0.3    96.9% / 90.0%   19/20
− day-level / price-war / outage learner            13,816       -547   -3.7    97.2% / 93.2%   20/20
− archive-fitted lateness model (our 6-week model instead)    13,993       -370   -2.2    96.8% / 89.7%   19/20
− two-car chains (return pairs only)                13,884       -479   -3.4    97.0% / 91.6%   20/20
− all two-car gigs                                  13,083     -1,280  -10.0    96.3% / 92.9%   20/20
− backhaul-aware quotes                             14,508       +145    1.2    97.0% / 90.0%   20/20
− note reader (kit's keyword reader instead)        13,485       -878   -5.9    95.9% / 91.6%   20/20
− day capacity & edge-day risk                      14,738       +375    1.8    95.9% / 85.7%   19/20
− holding gigs until 36 h before pickup             13,813       -550   -3.5    97.3% / 93.9%   20/20
− late-order shadow price λ                         14,900       +537    3.7    96.0% / 88.1%   19/20
```
