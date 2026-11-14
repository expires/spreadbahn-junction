"""Dealer notes read by NVIDIA Nemotron 3.5 Super VL, checked against the rule parser.

``parse_note_llm(text, language, arrived_at)`` returns exactly ``notes.parse_note``'s dict (see
notes.py) or None when the model could not be reached or its answer did not validate.

Grounded by construction:
  * The model never does arithmetic.  Under a strict JSON schema (vLLM structured output) it
    returns a list of items ``{quote, kind, day, time, payment_release}``: ``day`` is the day WORD
    the note uses (today / tomorrow / day_after_tomorrow / a weekday / none) and ``time`` a 24-hour
    "HH:MM".  Python turns them into world minutes with the same rules as notes.py (days count from
    the arrival day, a weekday is 2-8 days ahead, a day without a clock is 00:00 for a ready time
    and 20:00 for a deadline).
  * Every item carries a verbatim quote.  An item whose quote is not found in the note is dropped
    (``meta["dropped"]``): the model cannot invent a constraint that the dealer did not write.
  * An item's clock time is checked against the times its quote states (notes.py's scanner);
    an unambiguous mismatch is corrected to the quoted time (``meta["time_fixed"]``).

Measured in scripts/eval_nemotron_notes.py; results and prompt iterations in docs/nemotron.md.

``ensemble(rule_dict, llm_dict)`` keeps the SAFER reading of every field (later ready time,
earlier closing, later opening, earlier deadline, OR of the flags) and records the disagreements.

``NemotronNotes`` is the dispatcher hook: a 4-thread pool with a cache keyed by (note text,
arrivedAt).  ``prefetch(request)`` starts the call when the price request arrives; at post time
``constraints(request)`` waits at most ``wait`` seconds and falls back to the rule parse when the
answer is not there or failed.  It never raises.

The API key is read from the environment (NVIDIA_KEY) or from the repository's .env; it is never
logged or written anywhere.
"""
from __future__ import annotations

import difflib
import json
import os
import random
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Optional

from bahn.models import notes
from bahn.models.notes import DAY, HOUR

BASE_URL = os.environ.get("NEMOTRON_URL", "https://containers.datacrunch.io/nemotron-3-5-super-vl-b300/v1")
MODEL = os.environ.get("NEMOTRON_MODEL", "nemotron-3.5-super-vl")
MAX_CONCURRENCY = 4
_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY)  # shared by every caller in the process: be polite

# ----------------------------------------------------------------------------------------
# Key
# ----------------------------------------------------------------------------------------
_KEY: Optional[str] = None


def _api_key() -> Optional[str]:
    """NVIDIA_KEY from the environment, else from the first .env found walking up from this file
    (works from a git worktree too).  Never printed."""
    global _KEY
    if _KEY:
        return _KEY
    k = os.environ.get("NVIDIA_KEY")
    if not k:
        d = os.path.dirname(os.path.abspath(__file__))
        while True:
            p = os.path.join(d, ".env")
            if os.path.isfile(p):
                with open(p) as fh:
                    for line in fh:
                        m = re.match(r"\s*(?:export\s+)?NVIDIA_KEY\s*=\s*(.*?)\s*$", line)
                        if m:
                            k = m.group(1).strip().strip('"').strip("'")
                if k:
                    break
            up = os.path.dirname(d)
            if up == d:
                break
            d = up
    _KEY = k or None
    return _KEY


# ----------------------------------------------------------------------------------------
# Output schema (enforced by the server's structured-output decoder)
# ----------------------------------------------------------------------------------------
KINDS = ["ready", "pickup_opens", "pickup_closes", "delivery_opens", "delivery_closes", "deadline",
         "manual_gearbox", "no_plates", "on_summer_tyres"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAYS = ["none", "today", "tomorrow", "day_after_tomorrow"] + WEEKDAYS
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "day": {"type": "string", "enum": DAYS},
                    "time": {"type": "string", "pattern": "^(none|([01][0-9]|2[0-3]):[0-5][0-9])$"},
                    "payment_release": {"type": "boolean"},
                },
                "required": ["quote", "kind", "day", "time", "payment_release"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

# ----------------------------------------------------------------------------------------
# Prompts.  Versions are kept so the evaluation can show what each iteration changed.
# ----------------------------------------------------------------------------------------
_SCHEMA_TEXT = """Answer with JSON only: {"items": [{"quote": ..., "kind": ..., "day": ..., "time": ..., "payment_release": ...}, ...]}
- quote: the exact words of the note that state the fact (copied verbatim, typos included).
- kind: one of ready, pickup_opens, pickup_closes, delivery_opens, delivery_closes, deadline, manual_gearbox, no_plates, on_summer_tyres.
- day: none, today, tomorrow, day_after_tomorrow, monday, tuesday, wednesday, thursday, friday, saturday, sunday.
- time: "HH:MM" (24-hour) or "none".
- payment_release: true only for a ready time that waits for payment / financing / handover.
An empty list is the right answer for a note without constraints."""

PROMPT_V1 = """You extract transport constraints from a car dealer's note (Finnish, Swedish or English) attached to a car-transport request.
Constraints: when the car is ready for pickup, opening/closing hours of the pickup and delivery sites, a delivery deadline, manual gearbox, car has no number plates, car is on summer tyres.
""" + _SCHEMA_TEXT

PROMPT_V2 = """You read the free-text note a car dealer attaches to a car-transport request. Notes are Finnish, Swedish or English (often mixed, often with typos). A driver will be sent to collect the car at the PICKUP site and bring it to the DELIVERY site (destination). Extract ONLY the facts that constrain that transport, each with a verbatim quote.

KINDS
- ready: the earliest moment the car may be collected ("valmis", "valmis vasta", "noudettavissa aikaisintaan", "voi noutaa ... alkaen", "auton saa ... alkaen", "ready", "not ready before", "pickup from", "klar", "tidigast", "kan hämtas från"). If it waits for payment/financing/handover ("ostaja maksaa ensin", "released after payment", "efter betalning"), also set payment_release=true.
- pickup_opens / pickup_closes: daily hours of the PICKUP site (the dealer's own shop): "liike kiinni klo 14, haettava ennen sitä" = pickup_closes 14:00; "nouto ennen klo 18" / "collect before 16:00" = pickup_closes; "nouto klo 9-17", "hämtning kl 8-14.30", "pickup weekdays 9:00-14:30", "liike auki 7-20", "open 7:00-16:00" = pickup_opens AND pickup_closes (two items with the same quote). "ingen utlämning efter 15", "ei enää luovutuksia klo 19 jälkeen" = pickup_closes.
- delivery_opens / delivery_closes: daily hours of the DELIVERY site ("kohde", "vastaanotto", "vastaanottava liike", "vastaanottaja", "mottagning", "receiving", "destination open"): "kohde sulkee klo 17" = delivery_closes 17:00; "vastaanotto arkisin 7-18" = delivery_opens 07:00 and delivery_closes 18:00; "receiving until 16:30 only" / "vastaanottaja paikalla vain klo 18 saakka" = delivery_closes.
- deadline: the car must be AT the destination by then ("oltava perillä viimeistään", "perillä ... mennessä", "toimitus (perille) viim.", "must be at destination by", "at destination ... latest", "framme senast"). "Asiakas noutaa ma klo 20, auton oltava silloin perillä" / "customer picks it up at 15" = deadline at that time.
- manual_gearbox: the car has a manual gearbox ("manuaali", "manuaalivaihteinen", "käsivaihteet", "manuell", "manual", "stick shift"). NOT when the note says automatic / not manual ("ei manuaali", "not a manual").
- no_plates: the car has NO registration plates, so the driver must bring trade plates ("ilman kilpiä", "ei kilpiä", "rekisterikilvet irrotettu", "poistettu rekisteristä, tarvitaan siirtokilvet", "unregistered", "plates removed", "saknar skyltar", "avregistrerad"). "rekisteriote" / "registreringsbevis" / "registration document" are PAPERS, not plates.
- on_summer_tyres: the car is currently MOUNTED on summer tyres ("vielä kesärenkailla", "kesärenkaat alla", "ei talvirenkaita", "still on summer tyres", "summer tyres on, no winters", "har sommardäck på", "fortfarande sommardäck"). Tyres carried as cargo are NOT this ("kesärenkaat takakontissa", "4 kesärengasta tavaratilassa", "summer tyres in the trunk", "sommardäcken i bakluckan").

DAY: the day word the note uses for a ready time or a deadline; do NOT convert it to a date.
- today: tänään, idag, today, tonight. tomorrow: huomenna, i morgon, tomorrow, tmrw. day_after_tomorrow: ylihuomenna, i övermorgon, day after tomorrow.
- a weekday name in any language or abbreviation (ma/maanantaina/mån/måndag/mon = monday, ti/tiistaina/tis/tue = tuesday, ke/keskiviikkona/ons/wed = wednesday, to/torstaina/tors/thu = thursday, pe/perjantaina/fre/fri = friday, la/lauantaina/lör/sat = saturday, su/sunnuntaina/sön/sun = sunday).
- none when no day is given. Hours and flags always use day "none".

TIME: "HH:MM", 24-hour. "klo 14" = 14:00, "kl 9.30" = 09:30, "4pm" = 16:00, "8 am" = 08:00, "18:aan saakka" = 18:00. Hours written as WORDS are working hours between 09:00 and 17:00: one/yksi/yhdeltä/ett = 13:00, two/kaksi/kahdelta/två = 14:00, three/kolme/kolmelta/tre = 15:00, four/neljä/neljältä/fyra = 16:00, five/viisi/viideltä/fem = 17:00, nine/yhdeksältä/nio = 09:00, ten/kymmeneltä/tio = 10:00, eleven/yhdeltätoista/elva = 11:00, twelve/kahdeltatoista/tolv = 12:00, noon/keskipäivällä/middag = 12:00. "half three"/"puoli kolme"/"halv tre" = 14:30 (half an hour BEFORE the named hour); "half past two" = 14:30. "none" when the note gives a day but no clock.

CORRECTIONS AND DENIALS
- When the dealer corrects themself ("ei kun", "eikun", "korjaus:", "correction:", "sorry, I mean", "rättelse", "jag menar"), output only the corrected fact. A correction may change only the time or only the day; keep the other part from the original statement.
- A denied value is not a fact: "(not 15:00)", "ei klo 12", "inte kl 21" are ignored; the other value in that sentence stands.

IGNORE: a private seller's presence hours ("myyjä kotona klo 11-14", "seller home tomorrow 9-12", "myyjä paikalla 15-18"), "call 15 min before", reference/order numbers, key boxes and codes, spare wheels, fuel, washing, trade-in instructions, greetings.

""" + _SCHEMA_TEXT

# few-shot examples: (language, note, items)
_FEW_SHOT = [
    ("fi", "Auto valmis vasta huomenna klo 12.30, ei kun klo 13. Kohde sulkee klo 16. Viite 123456",
     [("valmis vasta huomenna klo 12.30, ei kun klo 13", "ready", "tomorrow", "13:00", False),
      ("Kohde sulkee klo 16", "delivery_closes", "none", "16:00", False)]),
    ("en", "Pickup only after payment, from tmoorrow 4pm. Summer tyres in the trunk, go with the car.",
     [("Pickup only after payment, from tmoorrow 4pm", "ready", "tomorrow", "16:00", True)]),
    ("fi", "Liike kiinni puoli neljältä, haettava ennen sitä! Toimitus viimeistään huomenna klo 18.30, eikun ylihuomenna.",
     [("Liike kiinni puoli neljältä", "pickup_closes", "none", "15:30", False),
      ("Toimitus viimeistään huomenna klo 18.30, eikun ylihuomenna", "deadline", "day_after_tomorrow", "18:30", False)]),
    ("sv", "Mottagning vardagar kl 9-20. Hämtning vardagar kl 8-14.30. Obs manuell.",
     [("Mottagning vardagar kl 9-20", "delivery_opens", "none", "09:00", False),
      ("Mottagning vardagar kl 9-20", "delivery_closes", "none", "20:00", False),
      ("Hämtning vardagar kl 8-14.30", "pickup_opens", "none", "08:00", False),
      ("Hämtning vardagar kl 8-14.30", "pickup_closes", "none", "14:30", False),
      ("Obs manuell", "manual_gearbox", "none", "none", False)]),
    ("en", "at destination tomorrow 20:00 latest (not 21:00). seller home tomorrow 9:00-12:00. Huoltokirja ja rekisteriote autossa",
     [("at destination tomorrow 20:00 latest", "deadline", "tomorrow", "20:00", False)]),
    ("fi", "Noudettavissa aikaisintaan huomenna yhdeksältä, ei kun keskiviikkona. Perillä torstaina yhteen mennessä. Poistettu rekisteristä, tarvitaan siirtokilvet.",
     [("Noudettavissa aikaisintaan huomenna yhdeksältä, ei kun keskiviikkona", "ready", "wednesday", "09:00", False),
      ("Perillä torstaina yhteen mennessä", "deadline", "thursday", "13:00", False),
      ("Poistettu rekisteristä, tarvitaan siirtokilvet", "no_plates", "none", "none", False)]),
    ("sv", "Hej, har fortfarande sommardäck på. Kunden hämtar fredag kl 15, bilen ska vara framme då.",
     [("har fortfarande sommardäck på", "on_summer_tyres", "none", "none", False),
      ("Kunden hämtar fredag kl 15, bilen ska vara framme då", "deadline", "friday", "15:00", False)]),
    ("en", "automatic, not a manual. Keys at the desk, ref 55312. Call 15 min before.", []),
    ("en", "Receiving until half four only. Not ready before Thursday.",
     [("Receiving until half four only", "delivery_closes", "none", "15:30", False),
      ("Not ready before Thursday", "ready", "thursday", "none", False)]),
    ("fi", "Auto ilman kilpiä. Kesärenkaat vanteineen takakontissa. Kuski tarvii manuaalikortin. Asiakas tulee hakemaan, perillä viim. klo 19.00!",
     [("Auto ilman kilpiä", "no_plates", "none", "none", False),
      ("Kuski tarvii manuaalikortin", "manual_gearbox", "none", "none", False),
      ("perillä viim. klo 19.00", "deadline", "none", "19:00", False)]),
]

# v3: three failure modes found by error analysis of v2 on the perturbed dev notes
# (scripts/eval_nemotron_notes.py --split dev --robust 100), fixed as a diff on v2:
#   1. "half past ten" read as 09:30: the Nordic "half N" (= N-1:30) rule leaked into English "half past N".
#   2. "hakekaa auto ennen klo 14.30" (fetch the car BEFORE 14:30) read as a READY time.
#   3. "vastaanotamme autoja 10-16" (we receive cars 10-16) read as PICKUP hours.
PROMPT_V3 = PROMPT_V2.replace(
    '"half three"/"puoli kolme"/"halv tre" = 14:30 (half an hour BEFORE the named hour); "half past two" = 14:30.',
    'Two different "half" forms: "half N" WITHOUT "past" (Finnish "puoli", Swedish "halv", English "half three") is '
    'half an hour BEFORE N: "puoli kolme" = "halv tre" = "half three" = 14:30, "puoli neljältä" = 15:30. English '
    '"half past N" is half an hour AFTER N: "half past ten" = 10:30, "half past two" = 14:30. "quarter past ten" = 10:15, '
    '"quarter to three" = 14:45.',
).replace(
    '"ingen utlämning efter 15", "ei enää luovutuksia klo 19 jälkeen" = pickup_closes.',
    '"ingen utlämning efter 15", "ei enää luovutuksia klo 19 jälkeen" = pickup_closes. The car must be FETCHED BEFORE '
    'a time ("ennen", "before", "före", "innan", "viimeistään" about the pickup: "hakekaa auto ennen klo 14.30", '
    '"pick it up before 3pm", "hämta bilen före 15") = pickup_closes, never a ready time. A ready time says FROM / AFTER / '
    'NOT BEFORE ("alkaen", "jälkeen", "aikaisintaan", "ei ennen", "from", "after", "not before", "tidigast", "inte före").',
).replace(
    '("kohde", "vastaanotto", "vastaanottava liike", "vastaanottaja", "mottagning", "receiving", "destination open")',
    '("kohde", "kohteessa", "perillä", "vastaanotto", "vastaanotamme autoja", "vastaanottava liike", "vastaanottaja", '
    '"mottagning", "mottagaren", "tar emot", "receiving", "we receive cars", "drop-off", "destination open")'
    '. Hours with no site named ("auki vain klo 20 asti", "we close at 15:00", "open 7-16") belong to the PICKUP site',
)
assert PROMPT_V3.count("half past ten") == 1 and "hakekaa" in PROMPT_V3 and "we receive cars" in PROMPT_V3

_FEW_SHOT_V3 = _FEW_SHOT + [
    ("en", "Can be picked up from Friday half past nine. Drop-off accepted until half four only. Get it before 3pm on pickup day.",
     [("Can be picked up from Friday half past nine", "ready", "friday", "09:30", False),
      ("Drop-off accepted until half four only", "delivery_closes", "none", "15:30", False),
      ("Get it before 3pm", "pickup_closes", "none", "15:00", False)]),
    ("fi", "Hakekaa auto ennen klo 15. Vastaanotamme autoja arkisin 8-16, ei kun 9-16.",
     [("Hakekaa auto ennen klo 15", "pickup_closes", "none", "15:00", False),
      ("Vastaanotamme autoja arkisin 8-16, ei kun 9-16", "delivery_opens", "none", "09:00", False),
      ("Vastaanotamme autoja arkisin 8-16, ei kun 9-16", "delivery_closes", "none", "16:00", False)]),
]

# v4: a typo'd site word ("receiing shpo open 7-17") fell under v3's new "no site named -> pickup"
# default.  Tell the model to read through typos before deciding the site.
PROMPT_V4 = PROMPT_V3.replace(
    "IGNORE:",
    "TYPOS: dealers type fast. Read each word as the word it was meant to be before deciding the kind or the site "
    '("recieving"/"reciving" = receiving, "destinaton" = destination, "vastanotto" = vastaanotto, "perilä" = perillä, '
    '"huomena" = huomenna, "tmorrow" = tomorrow, "manuaalivahteinen" = manuaalivaihteinen), and quote the note as written.\n\n'
    "IGNORE:",
)
assert PROMPT_V4 != PROMPT_V3

PROMPTS = {"v1": (PROMPT_V1, []), "v2": (PROMPT_V2, _FEW_SHOT), "v3": (PROMPT_V3, _FEW_SHOT_V3),
           "v4": (PROMPT_V4, _FEW_SHOT_V3)}
DEFAULT_PROMPT = "v3"  # v4 measured no better on dev (see docs/nemotron.md)


def _user_msg(text: str, language: Optional[str]) -> str:
    return f"Language hint: {language or 'unknown'}\nNote: <<<{text}>>>"


def build_messages(text: str, language: Optional[str], version: str = DEFAULT_PROMPT) -> list[dict]:
    system, shots = PROMPTS[version]
    msgs = [{"role": "system", "content": system}]
    for lang, note, items in shots:
        msgs.append({"role": "user", "content": _user_msg(note, lang)})
        msgs.append({"role": "assistant", "content": json.dumps({"items": [
            {"quote": q, "kind": k, "day": d, "time": t, "payment_release": pr} for q, k, d, t, pr in items]},
            ensure_ascii=False)})
    msgs.append({"role": "user", "content": _user_msg(text, language)})
    return msgs


# ----------------------------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------------------------

class LLMError(Exception):
    pass


def chat(messages: list[dict], thinking: bool = False, timeout: float = 20.0, retries: int = 3,
         max_tokens: Optional[int] = None) -> tuple[str, dict]:
    """One chat completion with structured output.  Retries 429 / 5xx / network errors with
    exponential backoff and jitter; at most MAX_CONCURRENCY requests in flight per process.
    Returns (content, info) where info has latency, attempts, usage, reasoning length."""
    key = _api_key()
    if not key:
        raise LLMError("no NVIDIA_KEY")
    body = {"model": MODEL, "messages": messages, "temperature": 0.0,
            "max_tokens": max_tokens or (4096 if thinking else 1024),
            "chat_template_kwargs": {"enable_thinking": bool(thinking)},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "note_constraints", "schema": SCHEMA, "strict": True}}}
    data = json.dumps(body).encode()
    t0 = time.perf_counter()
    last: Exception = LLMError("no attempt")
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random()))
        req = urllib.request.Request(BASE_URL + "/chat/completions", data=data, method="POST",
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with _SEM:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    out = json.load(r)
            msg = out["choices"][0]["message"]
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
            return content, {"latency": time.perf_counter() - t0, "attempts": attempt + 1,
                             "usage": out.get("usage") or {}, "reasoning_chars": len(reasoning),
                             "finish": out["choices"][0].get("finish_reason")}
        except urllib.error.HTTPError as e:
            last = LLMError(f"HTTP {e.code}")
            if e.code != 429 and e.code < 500:
                break  # a 4xx other than rate limiting will not get better
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as e:
            last = LLMError(type(e).__name__)
    raise last


# ----------------------------------------------------------------------------------------
# Validation and grounding
# ----------------------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", s))


def _tok_in(w: str, toks: set) -> bool:
    if w in toks:
        return True
    if len(w) < 4:
        return False
    # the model quietly fixes typos in its quotes ("Manuaalilvaihtienen" -> "Manuaalilvaihtinen")
    return any(abs(len(w) - len(x)) <= 3 and difflib.SequenceMatcher(None, w, x).ratio() >= 0.75 for x in toks)


def grounded(quote: str, text: str) -> bool:
    """Is the quote really in the note?  Exact after folding case/accents/punctuation, else at
    least 80% of its word tokens appear in the note, a token matching up to a typo or two."""
    q, t = _norm(quote), _norm(text)
    if not q:
        return False
    if q in t:
        return True
    qt, tt = q.split(), set(t.split())
    return len(qt) > 0 and sum(_tok_in(w, tt) for w in qt) / len(qt) >= 0.8


def parse_json(content: str) -> Optional[dict]:
    """The structured-output decoder should give clean JSON; still tolerate fences/prose."""
    if not content:
        return None
    s = content.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s)
    try:
        obj = json.loads(s)
    except ValueError:
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            obj = json.loads(s[i:j + 1])
        except ValueError:
            return None
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return None
    return obj


def _hhmm(t: Any) -> Optional[int]:
    if not isinstance(t, str):
        return None
    m = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", t.strip())
    return int(m.group(1)) * HOUR + int(m.group(2)) if m else None


def day_offset(day: str, arrived_at: int) -> Optional[int]:
    """Days after the arrival day, as notes.py counts them."""
    if day in ("none", "today", None):
        return 0
    if day == "tomorrow":
        return 1
    if day == "day_after_tomorrow":
        return 2
    if day in WEEKDAYS:
        k = (WEEKDAYS.index(day) - (arrived_at // DAY) % 7) % 7
        return k + 7 if k < 2 else k
    return None


def quote_times(quote: str) -> set:
    """Clock times literally present in a quote (minute of day), read by notes.py's scanner:
    digits ("klo 14.30", "4pm", "9-16") and the unambiguous word forms ("half past two",
    "puoli neljältä").  Denied times ("not 15:00") are excluded."""
    try:
        times, ranges, _, _ = notes._scan(notes._tokenize(notes._fold(quote)), 0)
    except Exception:
        return set()
    c = {t.minute for t in times if not t.denied}
    for a, b, _ in ranges:
        c |= {a, b}
    return c


def to_constraints(obj: dict, text: str, arrived_at: int, verify_times: bool = True) -> tuple[dict, dict]:
    """Model items -> notes.parse_note dict.  Combination as in notes.py: latest ready, earliest
    deadline, latest opening, earliest closing.  Returns (parsed, meta).

    With ``verify_times`` an item's clock time should be one of the times its quote states; when
    the quote states exactly one, backs no other timed item, and the model wrote another time (it
    once read "half past two" as 13:30), the quoted time wins (``meta["time_fixed"]``)."""
    out = notes._empty()
    meta = {"dropped": [], "items": []}
    arr_day = arrived_at // DAY
    timed_per_quote: dict[str, int] = {}
    for it in obj.get("items", []):
        if isinstance(it, dict) and _hhmm(it.get("time")) is not None:
            timed_per_quote[it.get("quote")] = timed_per_quote.get(it.get("quote"), 0) + 1
    for it in obj.get("items", []):
        if not isinstance(it, dict):
            meta["dropped"].append({"why": "not an object"})
            continue
        kind, quote = it.get("kind"), it.get("quote", "")
        if kind not in KINDS or not isinstance(quote, str):
            meta["dropped"].append({"why": "bad kind", "item": it})
            continue
        if not grounded(quote, text):
            meta["dropped"].append({"why": "quote not in note", "item": it})
            continue
        meta["items"].append(it)
        if kind == "manual_gearbox":
            out["manual"] = True
            continue
        if kind == "no_plates":
            out["plates"] = True
            continue
        if kind == "on_summer_tyres":
            out["summer_tyres"] = True
            continue
        minute = _hhmm(it.get("time"))
        if minute is not None and verify_times:
            cands = quote_times(quote)
            if cands and minute not in cands:
                if len(cands) == 1 and timed_per_quote.get(quote, 0) == 1:
                    # the quote states exactly one clock time and backs only this item: trust the text.
                    # (A range quote backs two items; "7:00-tree pm" has one READABLE time, but the
                    # model's 15:00 for the typo'd end is right, so never overwrite there.)
                    fixed = next(iter(cands))
                    meta.setdefault("time_fixed", []).append({"item": it, "to": f"{fixed // 60:02d}:{fixed % 60:02d}"})
                    minute = fixed
                else:
                    meta.setdefault("time_unverified", []).append(it)
        if kind in ("ready", "deadline"):
            off = day_offset(it.get("day", "none"), arrived_at)
            if off is None:
                meta["dropped"].append({"why": "bad day", "item": it})
                continue
            if minute is None:
                if it.get("day", "none") == "none":
                    meta["dropped"].append({"why": "no day and no time", "item": it})
                    continue
                minute = 0 if kind == "ready" else 20 * HOUR  # as notes.py: start / end of that business day
            t = (arr_day + off) * DAY + minute
            if kind == "ready":
                out["ready_from"] = t if out["ready_from"] is None else max(out["ready_from"], t)
                if it.get("payment_release"):
                    out["released"] = True
            else:
                out["deadline"] = t if out["deadline"] is None else min(out["deadline"], t)
            continue
        if minute is None:
            meta["dropped"].append({"why": "hours without a time", "item": it})
            continue
        cur = out[kind]
        if kind.endswith("_opens"):
            out[kind] = minute if cur is None else max(cur, minute)
        else:
            out[kind] = minute if cur is None else min(cur, minute)
    return out, meta


def parse_note_llm_meta(text: Optional[str], language: Optional[str] = None, arrived_at: int = 0,
                        thinking: bool = False, version: str = DEFAULT_PROMPT, timeout: float = 20.0,
                        retries: int = 3) -> tuple[Optional[dict], dict]:
    """parse_note_llm plus diagnostics (latency, raw content, dropped items, error)."""
    if not text or not text.strip():
        return notes._empty(), {"latency": 0.0, "skipped": True}
    try:
        content, info = chat(build_messages(text, language, version), thinking=thinking, timeout=timeout,
                             retries=retries)
    except LLMError as e:
        return None, {"error": str(e)}
    obj = parse_json(content)
    if obj is None:
        return None, dict(info, error="invalid JSON", raw=content[:500])
    try:
        parsed, meta = to_constraints(obj, text, arrived_at)
    except Exception as e:  # never let a malformed answer escape
        return None, dict(info, error=f"convert: {type(e).__name__}", raw=content[:500])
    return parsed, dict(info, raw=content, **meta)


def parse_note_llm(text: Optional[str], language: Optional[str] = None, arrived_at: int = 0,
                   thinking: bool = False, version: str = DEFAULT_PROMPT) -> Optional[dict]:
    """notes.parse_note's dict read by Nemotron, or None on any failure (caller falls back)."""
    return parse_note_llm_meta(text, language, arrived_at, thinking, version)[0]


# ----------------------------------------------------------------------------------------
# Ensemble: the safer reading per field
# ----------------------------------------------------------------------------------------
_LATER = ("ready_from", "pickup_opens", "delivery_opens")
_EARLIER = ("pickup_closes", "delivery_closes", "deadline")
_FLAGS = ("manual", "plates", "summer_tyres", "released")


def ensemble(rule_dict: dict, llm_dict: Optional[dict]) -> dict:
    """Per field, the reading that cannot cause a failed visit or a late car: later ready time and
    opening, earlier closing and deadline, OR of the flags.  ``None`` means "no constraint", so a
    constraint found by either reader is kept.  ``out["disagreements"]`` lists
    {field, rules, llm, chosen} for logging / review."""
    out = dict(rule_dict)
    dis: list[dict] = []
    if not llm_dict:
        out["disagreements"] = dis
        return out
    for k in _LATER + _EARLIER:
        a, b = rule_dict.get(k), llm_dict.get(k)
        if a == b:
            continue
        if a is None or b is None:
            v = a if b is None else b
        else:
            v = max(a, b) if k in _LATER else min(a, b)
        out[k] = v
        dis.append({"field": k, "rules": a, "llm": b, "chosen": v})
    for k in _FLAGS:
        a, b = bool(rule_dict.get(k)), bool(llm_dict.get(k))
        if a != b:
            dis.append({"field": k, "rules": a, "llm": b, "chosen": True})
        out[k] = a or b
    out["disagreements"] = dis
    return out


# ----------------------------------------------------------------------------------------
# Dispatcher hook
# ----------------------------------------------------------------------------------------

class NemotronNotes:
    """Non-blocking note reader for the dispatcher.

        nn = NemotronNotes()
        nn.prefetch(request)            # when the price request arrives (returns immediately)
        parsed = nn.constraints(request) # at post time: ensemble if Nemotron answered, else rules

    The cache is keyed by (note text, arrivedAt) -- never by request id, which repeats across
    worlds.  A failed call is cached as a failure (no retry storm); the rule parse is used."""

    def __init__(self, wait: float = 2.0, thinking: bool = False, version: str = DEFAULT_PROMPT,
                 workers: int = MAX_CONCURRENCY, http_timeout: float = 10.0, retries: int = 2,
                 use_ensemble: bool = True):
        self.wait, self.thinking, self.version = wait, thinking, version
        self.http_timeout, self.retries, self.use_ensemble = http_timeout, retries, use_ensemble
        self.pool = ThreadPoolExecutor(max_workers=min(workers, MAX_CONCURRENCY), thread_name_prefix="nemotron")
        self.futures: dict[tuple, Future] = {}
        self.lock = threading.Lock()
        self.stats = {"calls": 0, "llm_used": 0, "fallback_timeout": 0, "fallback_error": 0, "no_note": 0,
                      "disagreements": 0}
        self.log: list[dict] = []  # disagreements, for review after a run

    @staticmethod
    def _key(request: dict) -> Optional[tuple]:
        note = request.get("note")
        if not note or not note.get("text"):
            return None
        return note["text"], request["arrivedAt"]

    def _work(self, text: str, language: Optional[str], arrived_at: int) -> Optional[dict]:
        parsed, _ = parse_note_llm_meta(text, language, arrived_at, self.thinking, self.version,
                                        timeout=self.http_timeout, retries=self.retries)
        return parsed

    def prefetch(self, request: dict) -> Optional[Future]:
        key = self._key(request)
        if key is None:
            return None
        with self.lock:
            fut = self.futures.get(key)
            if fut is None:
                if len(self.futures) > 50_000:
                    self.futures.clear()
                fut = self.pool.submit(self._work, key[0], request["note"].get("language"), key[1])
                self.futures[key] = fut
                self.stats["calls"] += 1
        return fut

    def llm(self, request: dict, wait: Optional[float] = None) -> Optional[dict]:
        """Nemotron's reading if it is ready within `wait` seconds, else None."""
        fut = self.prefetch(request)
        if fut is None:
            return None
        try:
            return fut.result(timeout=self.wait if wait is None else wait)
        except Exception:  # TimeoutError or anything raised inside the worker
            return None

    def constraints(self, request: dict, wait: Optional[float] = None) -> dict:
        """Drop-in for notes.constraints(request).  Never raises, never blocks longer than `wait`."""
        rules = notes.constraints(request)
        key = self._key(request)
        if key is None:
            self.stats["no_note"] += 1
            return rules
        fut = self.prefetch(request)
        got = None
        try:
            got = fut.result(timeout=self.wait if wait is None else wait)
            if got is None:
                self.stats["fallback_error"] += 1
        except Exception:
            self.stats["fallback_timeout"] += 1
        if got is None:
            return rules
        self.stats["llm_used"] += 1
        if not self.use_ensemble:
            return dict(got)
        out = ensemble(rules, got)
        if out["disagreements"]:
            self.stats["disagreements"] += 1
            if len(self.log) < 10_000:
                self.log.append({"text": key[0], "arrivedAt": key[1], "fields": out["disagreements"]})
        return out

    # Same as notes.stops_for / notes.audience_for, reading the note through this client.
    def stops_for(self, order: dict, legs: list[int], request: dict, retry: Any = None, now: int = 0) -> list[dict]:
        parsed = self.constraints(request)
        return _stops_for(order, legs, parsed, retry, now)

    def audience_for(self, order: dict, legs: list[int], request: dict, retry: Any = None) -> dict:
        parsed = self.constraints(request)
        return {"experienced": any(order["legs"][i]["car"]["premium"] for i in legs),
                "manualLicence": bool(getattr(retry, "manual_licence", False)) or (0 in legs and bool(parsed.get("manual")))}


def _stops_for(order: dict, legs: list[int], parsed: dict, retry: Any, now: int) -> list[dict]:
    """notes.stops_for with the parsed note passed in (identical logic, via notes.apply_to_stops)."""
    r_from = getattr(retry, "pickup_from", 0) or 0
    r_plates = bool(getattr(retry, "plates", False))
    stops: list[dict] = []
    for leg in legs:
        L = order["legs"][leg]
        car, pick, deliv = L["car"], L["pickup"], L["delivery"]
        base_p = dict(pick["window"])
        start = max(base_p["from"], r_from)
        pw = {"from": start, "to": max(base_p["to"], start)}
        base_d = {"from": deliv["window"]["from"], "to": max(deliv["window"]["from"], order["deadline"])}
        pw, dw, flags = notes.apply_to_stops(order, leg, parsed, pw, dict(base_d), now)
        low = bool(car.get("electric")) and car.get("chargePercent") is not None and car["chargePercent"] < 30
        first = {"orderId": order["id"], "leg": leg, "kind": "pickup",
                 "withPlates": r_plates or flags["withPlates"], "withCharging": low}
        second = {"orderId": order["id"], "leg": leg, "kind": "delivery"}
        if pw["from"] > base_p["from"] or pw["to"] < base_p["to"]:
            first["window"] = pw
        if dw["to"] < base_d["to"] or dw["from"] > base_d["from"]:
            second["window"] = dw
        stops += [first, second]
    return stops


_DEFAULT: Optional[NemotronNotes] = None


def client() -> NemotronNotes:
    """The process-wide reader (lazily created)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = NemotronNotes()
    return _DEFAULT


if __name__ == "__main__":  # quick manual check: python -m bahn.models.nemotron_notes "note" ...
    import sys
    for line in sys.argv[1:] or ["Auto valmis huomenna klo 12 jälkeen. Liike kiinni klo 17. Ei kilpiä."]:
        rules = notes.parse_note(line, None, 600)
        llm, meta = parse_note_llm_meta(line, None, 600)
        print(line)
        print("  rules   ", json.dumps(rules))
        print("  nemotron", json.dumps(llm), f"{meta.get('latency', 0):.2f}s", meta.get("error", ""))
        print("  ensemble", json.dumps(ensemble(rules, llm)))
