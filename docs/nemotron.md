# Nemotron reads the dealer notes

**Model:** NVIDIA Nemotron 3.5 Super VL (`nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4`, served by vLLM over an
OpenAI-compatible endpoint). We use it with reasoning off by default. Reasoning on is an option we measured.

## What it does

Our dispatcher moves cars for Bahn's *Spread* challenge. Many price requests come with a free-text dealer note,
written in Finnish, Swedish or English, often mixed and full of typos. The note holds the facts that decide whether
a pickup works:

> *"Ostaja maksaa ensin, auton saa huomenna klo 15.30 alkaen. Liike kiinni puoli neljältä, eikun neljältä. Ei kilpiä."*

That note says: ready tomorrow 15:30 after payment, the shop closes at 16:00, and the car has no plates, so the
driver must bring trade plates. A missed fact here costs a failed visit (driver arrives before the car is ready,
after the shop closed, without plates or without a manual licence) or a late car.

Nemotron reads each note and returns the constraints in the same structure as our rule parser (`notes.parse_note`).
The dispatcher then keeps the **safer reading of each field** from the two readers: the later ready time, the
earlier closing time, the later opening time, the earlier deadline, and the OR of the flags (manual gearbox, no
plates, summer tyres). It also logs every field where the two readers disagreed.

## Why it matters

Our rule parser is about 1,000 lines of hand-tuned vocabulary. It is perfect on the note templates it was built on,
but it breaks when the wording changes. When we reworded the held-out notes with the same meaning, the rules made
**16 to 21 dangerous errors per ~70 notes**. Nemotron made 0, and the ensemble made 0. Dealers in the live final
can write anything, so we want a reader that understands language and not only a word list. We also want the
rules as a floor that the LLM cannot make worse.

## How it is built: grounded by construction (`src/bahn/models/nemotron_notes.py`)

1. **Structured output, no arithmetic in the model.** A strict JSON schema is enforced by vLLM's guided decoding
   (`response_format: json_schema`). The model emits a list of items `{quote, kind, day, time, payment_release}`.
   - `day` is the day *word* used in the note: `today | tomorrow | day_after_tomorrow | monday..sunday | none`.
   - `time` is a 24-hour `"HH:MM"`.

   Python converts these to world minutes with the challenge rules: days count from the arrival day, a weekday is
   2–8 days ahead, and a day without a clock time means 00:00 for a ready time and 20:00 for a deadline. The model
   never adds minutes or picks dates.
2. **Evidence or it didn't happen.** Every item quotes the note. If the quote is not in the note, the item is
   dropped. Matching ignores case, accents and punctuation, and allows a typo or two per token, because the model
   sometimes silently fixes a typo in its quote. The model cannot invent a constraint the dealer did not write.
3. **Time verification.** The quote is scanned for clock times with the rule parser's scanner. Suppose the quote
   states exactly one time, backs no other item, and the model wrote a different time. Then the quoted time wins.
   This caught `"half past two"` → 13:30.
4. **Unambiguous field names.** Our first probe used a key named `plates`. The model was unsure whether `true`
   meant "has plates" or "needs plates", and in reasoning mode it answered the wrong way. Renamed to `no_plates`,
   both modes get it right.
5. **Ensemble with the rules.** Described above. On any failure (network, HTTP error, invalid JSON, timeout) the
   result is `None` and the caller uses the rules.

**Dispatcher hook.** `NemotronNotes` runs a 4-thread pool with a cache keyed by `(note text, arrivedAt)`.

- `prefetch(request)` runs when the price request arrives. It does not block.
- `constraints(request)` runs at post time. It waits at most 2 s, then falls back to the rules.

A failed call is cached as a failure, so retries cannot pile up. HTTP calls retry 429, 5xx and network errors with
exponential backoff and jitter. A process-wide semaphore keeps at most 4 requests in flight. The API key is read
from the `NVIDIA_KEY` environment variable or from `.env`, and is never logged. The feature is behind
`Policy.notes_llm` (default **off**).

## Evaluation (`scripts/eval_nemotron_notes.py`)

**Ground truth.** The world's reveal files (`data/L8/*/reveal.json.gz`) record the true ready time, opening hours,
note deadline, gearbox, plates and tyres for every won order.

**Held-out data.** Only folders the rule parser was not developed on (`cal-`, `v13-`, `v14-`), 1,304 distinct notes.
We sampled a stratified set: notes whose truth changes something are over-sampled, and plain notes are kept to
measure false alarms. It is split into:
- **dev**: 150 notes, used for prompt iteration;
- **test**: 450 notes (300 that change something), reported once at the end.

**Scoring.** Scoring is `eval_notes.py`'s. Each field is the effective value the dispatcher would plan with,
compared with the truth. A **danger** error is one that causes a failed visit or a late car: ready time too early,
closing too late, opening too early, deadline too late, or a flag missed. A **waste** error is the safe-side
mistake (waiting or hurrying for nothing).

**Robustness.** On the test notes that change something, we applied four truth-preserving perturbations:
- **typos**: 2 random typos per note;
- **paraphrase**: 40 rewrites of the note phrases a dealer could plausibly use instead (Finnish, Swedish, English),
  for example "noudettavissa aikaisintaan" → "auton voi hakea aikaisintaan", or "must be at destination by" →
  "needs to reach the drop-off no later than";
- **wordhours**: English clock times written as words ("16:00" → "four pm", "14:30" → "half past two");
- **hard**: paraphrase plus word hours plus a typo.

### Results on the held-out test split

Each cell is danger / waste. The "fast" variants run with reasoning off and the "think" variants with reasoning on.
"Ensemble" combines the rules with Nemotron.

| test set (notes) | rules | Nemotron fast | Nemotron think | ensemble fast | ensemble think |
|---|---|---|---|---|---|
| clean (450) | **0 / 0** | 3 / 1 | **0 / 0** | **0 / 1** | **0 / 0** |
| typos ×2 (120) | 2 / 0 | 3 / 1 | 1 / 0 | 2 / 0 | **1 / 0** |
| paraphrase (69) | 16 / 13 | **0 / 0** | **0 / 0** | 0 / 13 | 0 / 13 |
| word hours (19) | 5 / 2 | **0 / 0** | **0 / 0** | 0 / 2 | 0 / 2 |
| hard (74) | 21 / 12 | **0 / 0** | **0 / 0** | 0 / 12 | 0 / 12 |
| **sum of perturbed sets (282)** | **44 / 27** | **3 / 1** | **1 / 0** | **2 / 27** | **1 / 27** |
| latency p50 / p95 | 0.03 ms | 0.30–0.33 / 0.44–0.53 s | 0.9–1.3 / 2.7–3.6 s | (async) | (async) |
| completion tokens (mean) | – | 57–75 | 440–600 | | |

- **Failed calls:** 0 of 3,713. Retries needed: 0. All responses finished with `stop`. Ungrounded items: 0 in the final runs.
- **Stop-level check.** We simulated the first gig post through the dispatcher's real `apply_to_stops` path and
  counted violations: pickup before ready, window over a real closing, delivery after the note deadline, missing
  plates, missing manual licence, refused windows. On the hard set the rules gave 18 violations and both ensembles
  gave 0. On the paraphrase set it was 15 against 0. On the clean set all three gave 0.

Per field on the hard set (danger / waste):

| field | rules | Nemotron fast | ensemble fast |
|---|---|---|---|
| ready | 5 / 0 | 0 / 0 | 0 / 0 |
| pickup opens / closes | 1 / 1, 3 / 10 | 0 / 0, 0 / 0 | 0 / 1, 0 / 10 |
| delivery opens / closes | 1 / 0, 4 / 0 | 0 / 0, 0 / 0 | 0 / 0, 0 / 0 |
| deadline | 7 / 1 | 0 / 0 | 0 / 1 |
| manual / plates / summer tyres | 0 / 0 each | 0 / 0 each | 0 / 0 each |

On the clean test, Nemotron fast made 3 dangerous errors in 2 notes:
- It returned nothing for "Kohde auki arkisin 10-15" and for "perillä liike kiinni klo 16 jälkeen".
- It filed "Receiving 9:00-21:00" under the pickup site (a safe-side error).

The ensemble covers all of them. Reasoning mode gets them right, at 3–5× the latency.

### How we improved the outputs (each step measured)

All prompt changes were driven by error analysis on **dev** and confirmed on **test**. Cells are danger / waste,
thinking off.

| step | change | effect |
|---|---|---|
| v1 | short task description plus the schema | dev clean **10 / 28** (8 notes with danger). It filed destination hours ("kohde auki 9-16") as pickup hours, called "summer tyres in the trunk" summer tyres (14 false alarms), and turned "collect before 14:30" into a deadline. |
| v2 | full semantics in the system prompt plus 10 few-shot examples | The prompt covers sites, readiness against closing, days as words, hours in words, "half three" = 14:30, corrections, denials, decoys (`rekisteriote` is papers, tyres in the trunk are cargo, a private seller's presence hours are ignored) and the unambiguous `no_plates` / `on_summer_tyres` keys. Dev clean **10 / 28 → 0 / 0**. Dev typos 8 / 16 → 0 / 0. Test hard (held out, prompt only, verification off) 6 / 12 → 1 / 1. |
| grounding v2 | quote matching tolerates a typo per token | The model had "corrected" `Manuaalilvaihtienen` in its quote, so a true manual flag was dropped as ungrounded. Dev typos 1 → 0 dangerous. |
| v3 | three fixes from v2's errors on perturbed dev notes | (1) "half past ten" was read as 09:30, because the Nordic "half N" rule leaked into English "half past N". The prompt now contrasts the two forms. (2) "hakekaa auto ennen klo 14.30" ("fetch before") was read as a ready time; the prompt now says *before* = closing, *from / after / not before* = ready. (3) "vastaanotamme autoja 10-16" ("we receive cars") was read as pickup hours; the prompt has more delivery-site words and a default site. Plus 2 few-shot examples. Prompt only (verification off): dev hard 4 / 4 → 0 / 0, **test hard (held out) 1 / 1 → 0 / 0**, test word hours 1 / 1 → 0 / 0. |
| time verification | the quoted clock time beats the model's when unambiguous | Removes the remaining "half past two" → 13:30 slip, about 1 waste per set. The first version also "fixed" a correct `7:00-tree pm` → 15:00 back to 07:00, which created a danger. We restricted it to quotes that back a single item. |
| v4 (rejected) | prompt asks the model to read through typos before choosing the site | No measurable change on dev: the double typo `receiing shpo open 7-17` was still filed as pickup. Kept v3, the shorter prompt. |
| reasoning on | `enable_thinking: true` | Test clean 3 / 1 → 0 / 0. All perturbed sets 3 / 1 → 1 / 0. Latency p50 0.3 → 0.9–1.3 s, p95 0.5 → 2.7–3.6 s. Inside the ensemble it removes 1 more danger. |

### Honest limits

- On the clean held-out templates the rules are already perfect, so there Nemotron adds nothing. Its value shows
  when wording drifts away from the templates, as live dealers' wording might.
- The ensemble takes the safe side of each field, so it inherits the rules' safe-side mistakes on reworded notes
  (12–13 waste per ~70 notes). This waste means an earlier closing or deadline than necessary, which costs slack,
  not visits.
- One pair of typos, `Klar först på osag kl 13` ("onsdag"), is missed by all readers.
- We have not yet run a dispatch simulation with `notes_llm=True`. The flag is off until the team A/B tests it.

## Reproduce

```bash
# offline unit checks (no network)
PYTHONPATH=src .venv/bin/python tests/test_nemotron_notes.py
# one note, both readers and the ensemble
PYTHONPATH=src .venv/bin/python -m bahn.models.nemotron_notes "Auto valmis huomenna klo 12 jälkeen. Liike kiinni klo 17. Ei kilpiä."
# prompt iterations on dev (clean + perturbed)
PYTHONPATH=src .venv/bin/python scripts/eval_nemotron_notes.py --split dev --prompts v1,v2,v3,v4 --thinking off --robust 100 --perturb typos,paraphrase,wordhours,hard
# final held-out numbers, both modes, stop-level check
PYTHONPATH=src .venv/bin/python scripts/eval_nemotron_notes.py --split test --prompts v3 --thinking off,on --robust 120 --perturb typos,paraphrase,wordhours,hard --stops
```

Answers are cached in `runs/nemotron/cache.jsonl` (gitignored), so re-running costs no calls. On a cache hit,
validation, grounding and conversion are re-run on the raw answer, so changes to that layer are measured for free.
`--no-verify` turns time verification off, and `--errors N` prints the failing notes with the model's raw answer.
The key comes from `NVIDIA_KEY` in the environment or `.env`.

## Enabling it in the dispatcher

Set `Policy(notes_llm=True)`. Three edits go into `src/bahn/simulation/dispatch.py`:
- `quote()` prefetches the note when a request arrives;
- `post_single()` and `post_pair()` read the note through `nemotron_notes.client()` instead of `notes`.

Both objects expose the same `stops_for` / `audience_for` API. `client().stats` and `client().log` record how often
the LLM answer was used, how often each fallback fired, and every disagreement.
