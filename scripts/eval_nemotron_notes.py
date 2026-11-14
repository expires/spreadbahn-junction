"""Evaluate Nemotron 3.5 Super VL as a dealer-note reader against the reveal ground truth.

    PYTHONPATH=src .venv/bin/python scripts/eval_nemotron_notes.py --split dev --prompts v1,v2,v3 --thinking off
    PYTHONPATH=src .venv/bin/python scripts/eval_nemotron_notes.py --split test --prompts v3 --thinking off,on
    PYTHONPATH=src .venv/bin/python scripts/eval_nemotron_notes.py --split test --prompts v3 --thinking off --robust 200

Data: won orders in data/L8/*/reveal.json.gz from the HELD-OUT folders (cal-, v13-, v14-: never used
to develop notes.py).  One row per distinct note text.  Stratified: notes whose truth changes
something (ready time, hours, deadline or a flag) are over-sampled, plus notes that change nothing
(to measure false alarms).  Split into dev (prompt iteration) and test (reported once at the end).

Scoring is eval_notes.py's: per field the EFFECTIVE value the dispatcher would plan with, against
the truth the world used.  "danger" = ready too early, closing too late, opening too early,
deadline too late, flag missed (a failed visit or a late car).  "waste" = the safe-side error.

Variants: rules (notes.parse_note), nemotron-<prompt>-<think> (alone; a failed call counts as an
empty parse), ensemble-<prompt>-<think> (safer reading per field; a failed call -> rules).

LLM answers are cached in runs/nemotron/cache.jsonl, so re-running costs no calls.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import random
import re
import statistics
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_notes as E  # noqa: E402  (truth logic, Stat/Flag, typo)

from bahn.models import nemotron_notes as NN  # noqa: E402
from bahn.models.notes import parse_note  # noqa: E402
from bahn.paths import DATA, ROOT, RUNS  # noqa: E402


def _data_dir() -> str:
    """data/ is gitignored, so a worktree has none: walk up to the checkout that has it."""
    d = os.path.join(DATA, "L8")
    p = ROOT
    while not os.path.isdir(d):
        up = os.path.dirname(p)
        if up == p:
            break
        p = up
        d = os.path.join(p, "data", "L8")
    return d


HELD_OUT = ("cal-", "v13-", "v14-")
FIELDS = ("ready", "pickup_opens", "pickup_closes", "delivery_opens", "delivery_closes", "deadline")
FLAGS = (("manual", "manualGearbox"), ("plates", "needsPlates"), ("summer_tyres", "summerTyres"))
CACHE = os.path.join(RUNS, "nemotron", "cache.jsonl")


# ----------------------------------------------------------------------------------------
# Sample
# ----------------------------------------------------------------------------------------

def changes(r) -> set:
    req, T = r["req"], r["truth"]["legs"][0]
    p, d = req["legs"][0]["pickup"], req["legs"][0]["delivery"]
    ch = set()
    if max(p["window"]["from"], T["readyAt"], T["releasedAt"] or 0) != p["window"]["from"]:
        ch.add("ready")
    for side, th, sh in (("pickup", T["pickupHours"], p["hours"]), ("delivery", T["deliveryHours"], d["hours"])):
        if sh and th:
            if th["opens"] != sh["opens"]:
                ch.add(side + "_opens")
            if th["closes"] != sh["closes"]:
                ch.add(side + "_closes")
    D = E.option_deadline(req, r["option"])
    if T["noteDeadline"] is not None and T["noteDeadline"] < D:
        ch.add("deadline")
    for k, tk in FLAGS:
        if T[tk]:
            ch.add(k)
    return ch


def sample(seed=7, dev_change=100, dev_plain=50, test_change=300, test_plain=150):
    rows = [r for r in E.load(_data_dir()) if r["folder"].startswith(HELD_OUT) and r["req"]["note"]]
    by = {}
    for r in rows:
        by.setdefault(r["req"]["note"]["text"], r)
    uniq = sorted(by.values(), key=lambda r: r["req"]["note"]["text"])
    rng = random.Random(seed)
    rng.shuffle(uniq)
    chg = [r for r in uniq if changes(r)]
    plain = [r for r in uniq if not changes(r)]
    dev = chg[:dev_change] + plain[:dev_plain]
    test = chg[dev_change:dev_change + test_change] + plain[dev_plain:dev_plain + test_plain]
    return dev, test, len(uniq), len(chg)


# ----------------------------------------------------------------------------------------
# Scoring (eval_notes.evaluate, returning per-row errors)
# ----------------------------------------------------------------------------------------

def row_errors(r, P) -> list[tuple]:
    """[(field, 'danger'|'waste', truth, pred)] for one row and one parse."""
    req, T = r["req"], r["truth"]["legs"][0]
    p, d = req["legs"][0]["pickup"], req["legs"][0]["delivery"]
    errs = []

    def cmp(field, truth, pred, danger_if_less):
        if truth != pred:
            bad = pred < truth if danger_if_less else pred > truth
            errs.append((field, "danger" if bad else "waste", truth, pred))

    stated = p["window"]["from"]
    cmp("ready", max(stated, T["readyAt"], T["releasedAt"] or 0), max(stated, P["ready_from"] or 0), True)
    for side, th, sh in (("pickup", T["pickupHours"], p["hours"]), ("delivery", T["deliveryHours"], d["hours"])):
        if sh is None or th is None:
            continue
        po, pc = P[side + "_opens"], P[side + "_closes"]
        o = max(sh["opens"], po) if po is not None else sh["opens"]
        c = min(sh["closes"], pc) if pc is not None else sh["closes"]
        if o >= c:
            o, c = sh["opens"], sh["closes"]
        cmp(side + "_opens", th["opens"], o, True)
        cmp(side + "_closes", th["closes"], c, False)
    D = E.option_deadline(req, r["option"])
    truth = min(D, T["noteDeadline"]) if T["noteDeadline"] is not None else D
    cmp("deadline", truth, min(D, P["deadline"]) if P["deadline"] is not None else D, False)
    for k, tk in FLAGS:
        t, q = bool(T[tk]), bool(P[k])
        if t != q:
            errs.append((k, "danger" if t else "waste", t, q))
    return errs


def summarize(rows, parses) -> dict:
    out = {f: {"danger": 0, "waste": 0, "exact": 0} for f in FIELDS + tuple(k for k, _ in FLAGS)}
    n_dang_notes = 0
    for r, P in zip(rows, parses):
        errs = row_errors(r, P)
        bad = {e[0]: e[1] for e in errs}
        for f in out:
            if f in bad:
                out[f][bad[f]] += 1
            else:
                out[f]["exact"] += 1
        n_dang_notes += any(e[1] == "danger" for e in errs)
    tot = {"danger": sum(v["danger"] for v in out.values()), "waste": sum(v["waste"] for v in out.values()),
           "notes_with_danger": n_dang_notes,
           "notes_all_exact": sum(1 for r, P in zip(rows, parses) if not row_errors(r, P)), "n": len(rows)}
    return {"fields": out, "total": tot}


# ----------------------------------------------------------------------------------------
# LLM runs with a disk cache
# ----------------------------------------------------------------------------------------
_lock = threading.Lock()
STOPS = False  # --stops: also count stop-level violations through the dispatcher path
VERIFY = True  # time verification against the quote (--no-verify to measure without it)
_cache: dict = {}


def _ckey(version, thinking, text, language, arrived_at):
    prompt = json.dumps(NN.build_messages(text, language, version), ensure_ascii=False)
    h = hashlib.sha1((prompt + f"|{thinking}|{arrived_at}|{NN.MODEL}").encode()).hexdigest()
    return h


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE) as fh:
            for line in fh:
                try:
                    x = json.loads(line)
                    _cache[x["k"]] = x
                except ValueError:
                    pass


def llm_parse(text, language, arrived_at, version, thinking):
    k = _ckey(version, thinking, text, language, arrived_at)
    hit = _cache.get(k)
    if hit is None or (hit.get("parsed") is None and "HTTP" in str(hit.get("meta", {}).get("error", ""))):
        parsed, meta = NN.parse_note_llm_meta(text, language, arrived_at, thinking=thinking, version=version,
                                              timeout=60.0 if thinking else 20.0)
        meta = {k2: v for k2, v in meta.items() if k2 in ("latency", "attempts", "usage", "error", "raw", "dropped",
                                                            "reasoning_chars", "finish")}
        hit = {"k": k, "v": version, "think": thinking, "text": text, "arr": arrived_at, "parsed": parsed,
               "meta": meta}
        with _lock:
            _cache[k] = hit
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            with open(CACHE, "a") as fh:
                fh.write(json.dumps(hit, ensure_ascii=False) + "\n")
    return rederive(hit)


def rederive(hit):
    """Re-run validation/grounding/conversion on the cached raw answer, so changes to that layer
    are measured without new calls."""
    m = hit["meta"]
    if m.get("error") or not m.get("raw"):
        return hit
    obj = NN.parse_json(m["raw"])
    if obj is None:
        return dict(hit, parsed=None, meta=dict(m, error="invalid JSON"))
    parsed, meta = NN.to_constraints(obj, hit["text"], hit["arr"], verify_times=VERIFY)
    return dict(hit, parsed=parsed, meta=dict(m, dropped=meta["dropped"], time_fixed=meta.get("time_fixed", [])))


def run_llm(rows, version, thinking, workers=4):
    def one(r):
        n = r["req"]["note"]
        return llm_parse(n["text"], n.get("language"), r["req"]["arrivedAt"], version, thinking)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(workers, NN.MAX_CONCURRENCY)) as ex:
        hits = list(ex.map(one, rows))
    print(f"  [{version} thinking={'on' if thinking else 'off'}] {len(rows)} notes in {time.time() - t0:.0f}s",
          file=sys.stderr)
    return hits


def rules_parse(r):
    n = r["req"]["note"]
    return parse_note(n["text"], n.get("language"), r["req"]["arrivedAt"])


# ----------------------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------------------

def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def report(rows, variants: dict, title: str, lat: dict | None = None) -> dict:
    """variants: name -> list of parses aligned with rows."""
    lat = lat or {}
    S = {name: summarize(rows, ps) for name, ps in variants.items()}
    names = list(variants)
    w = max(14, max(len(n) for n in names) + 1)
    print(f"\n=== {title}: {len(rows)} notes ({sum(1 for r in rows if changes(r))} change something)")
    print("danger / waste per field (danger = failed visit or late car)")
    print(f"{'field':<17}" + "".join(f"{n:>{w}}" for n in names))
    for f in FIELDS + tuple(k for k, _ in FLAGS):
        print(f"{f:<17}" + "".join(f"{str(S[n]['fields'][f]['danger']) + ' / ' + str(S[n]['fields'][f]['waste']):>{w}}"
                                   for n in names))
    for lab, key in (("TOTAL danger", "danger"), ("TOTAL waste", "waste"), ("notes w/ danger", "notes_with_danger"),
                     ("notes all exact", "notes_all_exact")):
        print(f"{lab:<17}" + "".join(f"{S[n]['total'][key]:>{w}}" for n in names))
    if lat:
        print(f"{'latency p50 s':<17}" + "".join(f"{(f'{pct(lat[n][0], .5):.2f}' if n in lat else '-'):>{w}}" for n in names))
        print(f"{'latency p95 s':<17}" + "".join(f"{(f'{pct(lat[n][0], .95):.2f}' if n in lat else '-'):>{w}}" for n in names))
        print(f"{'failed calls':<17}" + "".join(f"{(str(lat[n][1]) if n in lat else '-'):>{w}}" for n in names))
        print(f"{'ungrounded drops':<17}" + "".join(f"{(str(lat[n][2]) if n in lat else '-'):>{w}}" for n in names))
    return S


def show_errors(rows, parses, hits, label, limit=40):
    print(f"\n--- {label}: notes with an error (danger first) ---")
    shown = 0
    items = []
    for r, P, h in zip(rows, parses, hits):
        errs = row_errors(r, P)
        if errs:
            items.append((0 if any(e[1] == "danger" for e in errs) else 1, r, errs, h))
    items.sort(key=lambda x: x[0])
    for _, r, errs, h in items[:limit]:
        req = r["req"]
        print(f"[{req['note']['language']}] arr={E.fmt(req['arrivedAt'])} {req['note']['text']!r}")
        for f, kind, t, q in errs:
            fm = E.fmt if f in ("ready", "deadline") else (E.hm if isinstance(t, int) and not isinstance(t, bool) else str)
            print(f"     {kind.upper():<6} {f:<16} truth={fm(t)} pred={fm(q)}")
        if h is not None:
            m = h["meta"]
            if m.get("error"):
                print(f"     error: {m['error']}")
            else:
                print(f"     model: {m.get('raw', '')[:400]}")
            if m.get("dropped"):
                print(f"     dropped: {m['dropped']}")
        shown += 1
    return shown


# ----------------------------------------------------------------------------------------
# Robustness: truth-preserving perturbations
# ----------------------------------------------------------------------------------------

def fold_ascii(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


# Paraphrases a dealer could plausibly write, same meaning, wording outside the templates the rules were tuned on.
PARAPHRASES = [
    (r"(?i)\bnoudettavissa aikaisintaan\b", "auton voi hakea aikaisintaan"),
    (r"(?i)\bauto valmis vasta\b", "auto on valmiina vasta"),
    (r"(?i)\boltava perillä viimeistään\b", "pitää olla määränpäässä viimeistään"),
    (r"(?i)\btoimitus perille viim\.", "auton pitää olla määränpäässä viim."),
    (r"(?i)\bperillä viim\.", "määränpäässä viim."),
    (r"(?i)\bliike kiinni\b", "liike menee kiinni"),
    (r"(?i)\bkohde sulkee\b", "vastaanottava liike menee kiinni"),
    (r"(?i)\bkohde auki\b", "vastaanottava liike auki"),
    (r"(?i)\bvastaanotto\b", "vastaanotamme autoja"),
    (r"(?i)\bmust be at destination by\b", "needs to reach the drop-off no later than"),
    (r"(?i)\bat destination (\w+ )?(\d{1,2}:\d{2}) latest\b", r"deliver \1by \2 at the latest"),
    (r"(?i)\bnot ready before\b", "can't be collected before"),
    (r"(?i)\bready for pickup from\b", "can be picked up from"),
    (r"(?i)\bcollect before\b", "pick it up before"),
    (r"(?i)\breceiving until\b", "drop-off accepted until"),
    (r"(?i)\bdestination open\b", "drop-off site open"),
    (r"(?i)\bbuyer pays first, pickup from\b", "pickup once the buyer has paid, from"),
    (r"(?i)\bsenast\b", "allra senast"),
    (r"(?i)\bmanuaalivaihteinen\b", "vaihteisto on manuaali"),
    (r"(?i)\bunregistered\b", "not registered"),
    (r"(?i)\bauto ilman kilpiä\b", "autossa ei ole rekisterikilpiä"),
    (r"(?i)\bstill on summer tyres\b", "it still has its summer tyres fitted"),
    (r"(?i)\bauto vielä kesärenkailla\b", "autossa on vielä kesärenkaat"),
    (r"(?i)\bostaja maksaa ensin, auton saa\b", "kun ostaja on maksanut, auton voi hakea"),
    (r"(?i)\basiakas tulee hakemaan, perillä viim\.", "asiakas hakee auton kohteesta, sen pitää olla siellä viim."),
    (r"(?i)\bluovutus vasta kun maksettu, aikaisintaan\b", "auto luovutetaan vasta maksun jälkeen, aikaisintaan"),
    (r"(?i)\bcar won't be ready until\b", "the car will only be done at"),
    (r"(?i)\bin the workshop, ready\b", "still being serviced, done"),
    (r"(?i)\bklar först i morgon\b", "bilen blir klar tidigast i morgon"),
    (r"(?i)\bmottagaren har öppet\b", "mottagaren tar emot bilar"),
    (r"(?i)\bwe close at\b", "our shop shuts at"),
    (r"(?i)\bsaknar skyltar\b", "bilen har inga registreringsskyltar"),
    (r"(?i)\bkilvet poistettu\b", "kilvet on otettu pois"),
    (r"(?i)\bei rekkareita\b", "autossa ei ole rekkareita"),
    (r"(?i)\bsummer tyres on, no winters\b", "fitted with summer tyres, no winter set"),
    (r"(?i)\bkesärenkaat alla\b", "alla on vielä kesärenkaat"),
    (r"(?i)\bperillä liike kiinni klo\b", "kohteessa ovet menevät kiinni klo"),
    (r"(?i)\bvoi noutaa\b", "auton voi hakea"),
    (r"(?i)\bnouto ennen klo\b", "hakekaa auto ennen klo"),
    (r"(?i)\breleased after payment, not before\b", "handed over once the payment clears, not before"),
]

_EN_WORD = {9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "one", 14: "two", 15: "three", 16: "four", 17: "five"}


def word_hours(text):
    """English clock times between 9 and 17 written as words ("16:00" -> "four pm", "14:30" ->
    "half past two"); only in English sentences with HH:MM times."""
    def rep(m):
        h, mi = int(m.group(1)), int(m.group(2))
        if h in _EN_WORD and mi == 0:
            return "noon" if h == 12 else _EN_WORD[h] + (" am" if h < 12 else " pm")
        if h in _EN_WORD and mi == 30:
            return "half past " + _EN_WORD[h]
        return m.group(0)
    out = re.sub(r"\b(\d{1,2}):(\d{2})\b", rep, text)
    return out if out != text else None


def paraphrase(text):
    out, n = text, 0
    for pat, rep in PARAPHRASES:
        out, k = re.subn(pat, rep, out, count=1)
        n += k
    return out if n else None


def perturb(rows, kind, rng):
    out = []
    for r in rows:
        t = r["req"]["note"]["text"]
        if kind == "typos":
            t2 = E.typo(E.typo(t, rng), rng)
        elif kind == "style":  # ASCII folding, shouting, 'klo' -> 'kello', '12:00' <-> '12.00'
            t2 = fold_ascii(t)
            t2 = re.sub(r"\bklo\b", "kello", t2)
            t2 = re.sub(r"(\d{1,2})[:.](\d{2})", lambda m: m.group(1) + (":" if rng.random() < .5 else ".") + m.group(2), t2)
            t2 = t2.upper() if rng.random() < 0.5 else t2.lower()
        elif kind == "paraphrase":
            t2 = paraphrase(t)
            if t2 is None:
                continue
        elif kind == "wordhours":
            if r["req"]["note"].get("language") != "en":
                continue
            t2 = word_hours(t)
            if t2 is None:
                continue
        elif kind == "hard":  # paraphrase and/or word hours, plus one typo
            t2 = paraphrase(t) or t
            if r["req"]["note"].get("language") == "en":
                t2 = word_hours(t2) or t2
            if t2 == t:
                continue
            t2 = E.typo(t2, rng)
        else:
            raise ValueError(kind)
        out.append(dict(r, req=dict(r["req"], note=dict(r["req"]["note"], text=t2))))
    return out


# ----------------------------------------------------------------------------------------

def evaluate_split(rows, prompts, thinkings, title, errors=0, ens_only_for=None):
    variants = {"rules": [rules_parse(r) for r in rows]}
    lat = {}
    for v in prompts:
        for th in thinkings:
            tag = f"{v}-{'think' if th else 'fast'}"
            hits = run_llm(rows, v, th)
            empty = NN.notes._empty()
            alone = [h["parsed"] if h["parsed"] is not None else empty for h in hits]
            ens = [NN.ensemble(rp, h["parsed"]) for rp, h in zip(variants["rules"], hits)]
            variants["nemo-" + tag] = alone
            variants["ens-" + tag] = ens
            lats = [h["meta"].get("latency") for h in hits if h["meta"].get("latency")]
            fails = sum(1 for h in hits if h["parsed"] is None)
            drops = sum(1 for h in hits for d in (h["meta"].get("dropped") or []) if d.get("why") == "quote not in note")
            lat["nemo-" + tag] = (lats, fails, drops)
            fixed = sum(len(h["meta"].get("time_fixed") or []) for h in hits)
            if fixed:
                print(f"  {tag}: {fixed} times corrected by quote verification", file=sys.stderr)
            toks = [h["meta"].get("usage", {}).get("completion_tokens") for h in hits if h["meta"].get("usage")]
            if toks:
                print(f"  {tag}: completion tokens mean {statistics.mean(toks):.0f}, "
                      f"p95 {pct(toks, .95)}", file=sys.stderr)
            if errors:
                show_errors(rows, alone, hits, f"nemotron {tag} [{title}]", errors)
    S = report(rows, variants, title, lat)
    if STOPS:
        stop_check(rows, variants, title)
    return S, variants


def stop_check(rows, variants, title):
    """Through the dispatcher's real path (notes.apply_to_stops via NemotronNotes' stops_for): the
    post made right after winning each order, counted against the truth (eval_notes.check_stops)."""
    sys.modules.setdefault("notes", NN.notes)  # eval_notes._open_ivs does `from notes import _day_open`
    out = {}
    for name, parses in variants.items():
        if name.startswith("nemo-"):
            continue
        by_id = {id(r["req"]): P for r, P in zip(rows, parses)}
        with contextlib.redirect_stdout(io.StringIO()):
            V = E.check_stops(rows, lambda o, legs, req, now: NN._stops_for(o, legs, by_id[id(req)], None, now),
                              lambda o, legs, req: {"manualLicence": bool(by_id[id(req)]["manual"])}, name)
        out[name] = V
    keys = ("pickup_before_ready", "pickup_window_hits_real_closed", "delivery_after_note_deadline",
            "delivery_end_at_real_closed", "no_plates", "no_manual_licence", "REFUSED_starts_before_order",
            "REFUSED_private_outside_slot")
    w = max(14, max(len(n) for n in out) + 1)
    print(f"stop-level violations of the first post [{title}]")
    print(f"{'':<32}" + "".join(f"{n:>{w}}" for n in out))
    for k in keys:
        print(f"{k:<32}" + "".join(f"{out[n][k]:>{w}}" for n in out))
    print(f"{'TOTAL':<32}" + "".join(f"{sum(out[n][k] for k in keys):>{w}}" for n in out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=("dev", "test", "both"))
    ap.add_argument("--prompts", default="v3")
    ap.add_argument("--thinking", default="off")
    ap.add_argument("--errors", type=int, default=0, help="print up to N erroneous notes per LLM variant")
    ap.add_argument("--robust", type=int, default=0, help="robustness check on N changing test notes")
    ap.add_argument("--perturb", default="typos,style,paraphrase")
    ap.add_argument("--no-clean", action="store_true", help="skip the unperturbed evaluation")
    ap.add_argument("--rules-errors", type=int, default=0)
    ap.add_argument("--json", default=None, help="write the summary here")
    ap.add_argument("--stops", action="store_true", help="stop-level check through the dispatcher path")
    ap.add_argument("--no-verify", action="store_true", help="turn off the quote time verification")
    args = ap.parse_args()
    global VERIFY, STOPS
    VERIFY = not args.no_verify
    STOPS = args.stops
    load_cache()
    dev, test, n_uniq, n_chg = sample()
    print(f"held-out distinct notes: {n_uniq} ({n_chg} change something); dev {len(dev)}, test {len(test)}")
    prompts = [p for p in args.prompts.split(",") if p]
    thinkings = [t == "on" for t in args.thinking.split(",") if t]
    summary = {}
    splits = {"dev": [dev], "test": [test], "both": [dev, test]}[args.split]
    for name, rows in zip(["dev", "test"] if args.split == "both" else [args.split], splits):
        if args.rules_errors:
            rp = [rules_parse(r) for r in rows]
            show_errors(rows, rp, [None] * len(rows), f"rules [{name}]", args.rules_errors)
        if not args.no_clean:
            S, _ = evaluate_split(rows, prompts, thinkings, name.upper(), args.errors)
            summary[name] = S
    if args.robust:
        for name, part in zip(["dev", "test"] if args.split == "both" else [args.split], splits):
            chg = [r for r in part if changes(r)][:args.robust]
            for kind in args.perturb.split(","):
                rows = perturb(chg, kind, random.Random(11))
                S, _ = evaluate_split(rows, prompts, thinkings, f"ROBUSTNESS {kind} [{name}]", args.errors)
                summary[f"robust-{kind}-{name}"] = S
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(summary, fh, indent=1)


if __name__ == "__main__":
    main()
