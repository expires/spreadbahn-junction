# Notes reader: evaluation against the level-8 archive

Worlds: 228. Won note orders evaluated: **28503** (one row per won request carrying a note, leg 0). Run time 15.1s.

Truth is the reveal's per-leg facts (reveal.orders), i.e. what the world actually acted on. Reader = notes_reader.read_note. Baseline = the kit starter's window_from_note, the old hand-set reader (it only narrows windows by clock times near a few keywords).

## Per-fact accuracy

| fact | truth cases | reader recall | reader precision | reader exact (all rows) | starter recall | starter precision | starter exact (all rows) |
|---|---|---|---|---|---|---|---|
| pickup floor (ready/release) | 2633 | 100.0% | 100.0% | 28503/28503 (100.0%) | 41.8% | 100.0% | 26557/28503 (93.2%) |
| ready delay days | 386 | 100.0% | 100.0% | 28503/28503 (100.0%) | 0.0% | - | 28117/28503 (98.6%) |
| delivery deadline | 1533 | 100.0% | 100.0% | 28503/28503 (100.0%) | 59.0% | 38.7% | 25872/28503 (90.8%) |
| pickup hours | 1425 | 100.0% | 100.0% | 28503/28503 (100.0%) | 0.0% | - | 27078/28503 (95.0%) |
| delivery hours | 1087 | 100.0% | 100.0% | 28503/28503 (100.0%) | 0.0% | - | 27416/28503 (96.2%) |
| manual licence | 594 | 100.0% | 100.0% | 28503/28503 (100.0%) | 0.0% | - | 27909/28503 (97.9%) |
| transfer plates | 545 | 100.0% | 100.0% | 28503/28503 (100.0%) | 0.0% | - | 27958/28503 (98.1%) |
| summer tyres | 314 | 100.0% | 100.0% | 28503/28503 (100.0%) | 0.0% | - | 28189/28503 (98.9%) |

Exact = same absolute world minute / same (opens, closes) pair / same boolean, over all evaluated orders including the harmless notes where the right answer is no constraint. Precision is over the reader's non-default predictions, recall over the truth's non-default facts. The starter never reads release/handover, day words, am/pm, corrections, opening-hours overrides or driver/car requirements, so its recall on those facts is 0 by construction.

### Per note language

Exact rows / all rows of that language (all facts match).

| language | rows | pickup floor exact | deadline exact | pickup hours exact | delivery hours exact | manual P/R | plates P/R | summer P/R |
|---|---|---|---|---|---|---|---|---|
| en | 7143 | 7143/7143 | 7143/7143 | 7143/7143 | 7143/7143 | 100.0%/100.0% | 100.0%/100.0% | 100.0%/100.0% |
| fi | 20024 | 20024/20024 | 20024/20024 | 20024/20024 | 20024/20024 | 100.0%/100.0% | 100.0%/100.0% | 100.0%/100.0% |
| sv | 1336 | 1336/1336 | 1336/1336 | 1336/1336 | 1336/1336 | 100.0%/100.0% | 100.0%/100.0% | 100.0%/100.0% |

## Combined truth-window coverage

For every won note order: pickup window = [planned floor, order lastPickupAt], delivery window = [window from, planned deadline]. Covered = the true earliest sensible pickup max(readyAt, releasedAt) / latest delivery min(order deadline, noteDeadline) is inside the planned window. Unsafe = the plan starts before the car is ready (a failed visit) / ends after the true delivery ceiling (late). Slack is the median minutes the plan is tighter(-) or wider(+) than the truth.

| plan | pickup covered | delivery covered | both covered | pickup unsafe | delivery unsafe | median pickup slack | median delivery slack |
|---|---|---|---|---|---|---|---|
| ignore the note | 100.0% | 98.6% | 98.6% | 9.2% | 4.3% | +0 | +0 |
| kit starter | 98.9% | 91.7% | 90.6% | 5.7% | 1.8% | +0 | +0 |
| notes_reader | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | +0 | +0 |

## What the reader changes on the notes we win

| constraint | orders | share | median shift |
|---|---|---|---|
| pickup floor later than the stated ready time | 2633 | 9.2% | +180 min |
| car ready a later business day (deadline moves too) | 386 | 1.4% | +1 bd |
| tighter delivery deadline | 1533 | 5.4% | -240 min vs order deadline |
| real pickup opening hours | 1425 | 5.0% | - |
| real delivery opening hours | 1087 | 3.8% | - |
| manual licence required | 594 | 2.1% | - |
| transfer plates required | 545 | 1.9% | - |
| summer tyres (snow-sensitive) | 314 | 1.1% | - |
| at least one constraint | 8131 | 28.5% | - |

## How dispatch v8 applies the constraints

The order/request windows do NOT carry the note's truth: for ready-delay notes the
order's pickup window.from stays at the stated ready time and only the deadline
moves. Dispatch must use the reader's output explicitly:

    nc = read_note(request)
    # pickup window floor (post the pickup from here, never earlier)
    pickup_from = max(request_pickup_window_from, nc.pickup_from or -inf)
    # deadline: shift the quoted option's deadline by the ready-delay business days,
    # then tighten with the note deadline
    deadline = shift_business_days(option_deadline, nc.ready_delay_days)
    if nc.delivery_by is not None: deadline = min(deadline, nc.delivery_by)
    # opening hours: override the site's stated hours for every day when present
    pickup_hours = nc.pickup_hours or stated_pickup_hours
    delivery_hours = nc.delivery_hours or stated_delivery_hours
    # driver: audience manual only when nc.manual_licence; withPlates when nc.needs_plates
    # summer tyres: keep the car out of forecast snow at either end before the deadline

ready_delay_days is 0 for a release/handover note (a release moves the pickup floor
but not the deadline). pickup_from already equals the later ready time or the release
time, so the pickup window can be pinned to it even when ready_delay_days is 0.

## Validation split

The reader is a deterministic rule reader with no fitted parameters, so whole-world halves (even/odd world index) are a stability check rather than a train/test split. The rules were written from the rules of the game and audited against the archive as a whole, so treat this as confirmation that no single world dominates the numbers.

| half | rows | pickup floor exact | deadline exact | pickup hours exact | delivery hours exact |
|---|---|---|---|---|---|
| even worlds | 14289 | 14289/14289 | 14289/14289 | 14289/14289 | 14289/14289 |
| odd worlds | 14214 | 14214/14214 | 14214/14214 | 14214/14214 | 14214/14214 |

## What the old hand-set reader got wrong

The starter's 20 most common wrong notes, grouped by note text with digits normalised to '#'. Count = won orders in the archive with that note text where the starter disagrees with the truth. Truth = what the world acted on; reader = what notes_reader returns for the same request.

| # | count | lang | note (example) | starter says | truth | reader says |
|---|---|---|---|---|---|---|
| 1 | 49 | fi | Manuaalivaihteinen | no constraint | manual | manual |
| 2 | 34 | fi | Rekisterikilvet irrotettu | no constraint | plates | plates |
| 3 | 33 | en | not ready before tomorrow 11:00 | no constraint | pickup>=1:11:00 | pickup>=1:11:00 |
| 4 | 32 | fi | Nouto ennen klo 14. | no constraint | pickup hours 07:00-14:00 | pickup hours 07:00-14:00 |
| 5 | 32 | fi | Poistettu rekisteristä, tarvitaan siirtokilvet. | no constraint | plates | plates |
| 6 | 31 | fi | Ei automaatti vaan manuaali. | no constraint | manual | manual |
| 7 | 30 | fi | Kilvet puuttuu, tuo siirtokilvet. | no constraint | plates | plates |
| 8 | 27 | fi | nouto ennen klo 15.30 | no constraint | pickup hours 08:00-15:30 | pickup hours 08:00-15:30 |
| 9 | 24 | fi | Perillä liike kiinni klo 15 jälkeen | no constraint | delivery hours 09:00-15:00 | delivery hours 09:00-15:00 |
| 10 | 24 | fi | auto valmis vasta huomenna klo 15.30. | no constraint | pickup>=1:15:30 | pickup>=1:15:30 |
| 11 | 23 | fi | Kesärenkaat alla, ei talvirenkaita | no constraint | summer tyres | summer tyres |
| 12 | 22 | fi | Toimitus perille viim. ma klo 15 | by 4:15:00 | by 7:15:00 | by 7:15:00 |
| 13 | 22 | fi | Maksun jälkeen, ei ennen huomenna klo 11.30 | no constraint | pickup>=4:11:30 | pickup>=4:11:30 |
| 14 | 22 | en | manual, driver needs a manual licence | no constraint | manual | manual |
| 15 | 22 | fi | huom manuaali. | no constraint | manual | manual |
| 16 | 21 | fi | Vastaanotto arkisin klo 8-21. Huolto kesken, valmis huomenna klo 11 | no constraint | pickup>=1:11:00 | pickup>=1:11:00 |
| 17 | 21 | en | Not ready before tmrw 9:30. | no constraint | pickup>=1:09:30 | pickup>=1:09:30 |
| 18 | 21 | fi | Moi. Noudettavissa aikaisintaan huomenna klo 10 | no constraint | pickup>=2:10:00 | pickup>=2:10:00 |
| 19 | 21 | fi | Nouto arkisin 9-17 | no constraint | pickup hours 09:00-17:00 | pickup hours 09:00-17:00 |
| 20 | 20 | fi | perillä maanantaina klo 20 mennessä | by 4:20:00 | no constraint | no constraint |

The systematic misses, in words:

- **No day semantics.** It reads the clock time but puts it on the stated window's
  own day, so 'huomenna 11:00', 'i morgon kl 12', 'tomorrow 4pm', 'to klo 10' and
  'keskiviikkona 9:30' land on the wrong day. It also has no language-robust
  weekday handling and no corrections ('eikun klo 18', 'korjaus: ma', 'sorry, i mean').
- **No am/pm and no hour words.** '3pm' is read as 03:00; 'kahdeltatoista',
  'viideltä', 'halv tio' are not read at all, so those notes keep the stated window.
- **Opening hours are only a window narrowing.** Ranges ('9-16') are moved into the
  planned window's day but the plan still allows the driver to arrive after the real
  closing time; 'arki(sin)'/'weekdays' is not understood; the real pickup/delivery
  hours are never applied per day, so the driver waits or the visit fails (closed).
- **It cannot read a release, a later ready DAY, a manual gearbox, missing plates or
  summer tyres.** Those notes are invisible: the plan starts before handover/ready
  (visit_failed not_ready), sends an automatic-licence driver, forgets withPlates
  (visit_failed no_plates) or drives summer tyres in snow (visit_failed not_ready).
- **The delivery-keyword heuristic misfires.** Any delivery-ish word anywhere in the
  note sends every clock time to the delivery window and ignores the pickup side, so
  two-stop notes ('nouto ennen klo 15. vastaanotto 8-20') are half-read.
- **No deadline-tightening.** A note deadline later than the won option's deadline is
  harmless, but one that tightens it is missed: the driver plans by the order deadline
  and the car is late by the note (a refund), or the quote under-prices the risk.

## Reader residual mistakes

None. On every labelled fact of every won note order in the archive the reader agrees with the reveal exactly (28,503 orders / 98,696 note requests).

## Robustness and speed

Run over every note request in the archive, won or not: 98,696 note requests, 0 internal parse failures, 28,591 requests that produced at least one constraint, 4.35s total (22689 notes/second). read_note never raises: an internal error falls back to an empty constraint set, and the audit checks that this fallback never fires.
