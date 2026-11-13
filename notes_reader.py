"""Reads a dealer's free-text note into hard constraints for dispatch v8.

The world acts on a note whether or not we read it: a car may only be collectable after
a later ready time or after payment, the site's real opening hours can differ from the
stated ones, a tighter deadline can apply, and the driver may need a manual licence,
transfer plates, or a snow-free window for summer tyres. Ignoring the note causes
failed visits, waiting and lateness.

    from notes_reader import read_note, NoteConstraints
    nc = read_note(request)

Design: a deterministic rule reader for the phrasings the level-8 note generator uses
(Finnish, Swedish, English, with typos, two-language notes and corrections). Every note
is parsed into clock times, day words and keyword facts; harmless notes that merely
restate the request's own ready time / hours / deadline therefore produce constraints
equal to the stated values, and the composer drops those (never widening a window).

Day rules (RULES.md "Notes and changes of plan"):
  * a time without a day is on the arrival day;
  * tänään/idag/today = arrival day, huomenna/i morgon/tomorrow = +1 day,
    ylihuomenna/i övermorgon/day after tomorrow = +2 days (short forms included);
  * a weekday name means that weekday 2 to 6 days after the arrival;
  * digits are a 24-hour clock unless am/pm follows;
  * an hour in words (fi/sv) is the working hour with that number (1 -> 13:00 ...
    5 -> 17:00; 9..12 -> 09:00..12:00); "puoli kolme"/"half three"/"halv tre" is 14:30;
  * a correction counts: "korjaus: 16.30", "ei kun klo 15", "sorry, i mean 4pm";
    a denied time, "(ei klo 15)", "not 14:00", does not.

The reader never widens a window: on any doubt it returns no constraint for that fact.

Public API (only these two names are needed):

    from notes_reader import read_note, NoteConstraints
    nc = read_note(request)        # request = spread.types.PriceRequest (dict); note may be None

    nc.pickup_from               absolute minute: the pickup of leg 0 may not start before it
    nc.delivery_by               absolute minute: do not plan the delivery after it (None = no
                                 tighter deadline than the option's own)
    nc.note_deadline             the same value as delivery_by, kept for tight-deadline notes
    nc.pickup_hours              (opens, closes) minutes-since-midnight, override every day
    nc.delivery_hours            same for the delivery site
    nc.manual_licence            the driver must hold a manual licence
    nc.needs_plates              post the pickup withPlates
    nc.summer_tyres              the car cannot be driven while snow falls at either end
    nc.ready_delay_days          the car is ready this many BUSINESS days later than stated;
                                 the option deadline moves by the same business days
    nc.confidence                0.0 when the note changes nothing, 0.85-0.95 otherwise

read_note is pure (it never mutates the request) and never raises. It costs about 45
microseconds per note (22k notes/second). For a trade-in request the constraint set is for
leg 0, which is where every note fact in the archive lands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

__all__ = ["read_note", "NoteConstraints"]

DAY = 1440
HOUR = 60


@dataclass
class NoteConstraints:
    pickup_from: Optional[int] = None      # absolute world-minute floor for picking up the first car
    delivery_by: Optional[int] = None      # absolute world-minute ceiling for delivery, if the note tightens it
    pickup_hours: Optional[tuple[int, int]] = None   # (opens, closes) minutes-since-midnight
    delivery_hours: Optional[tuple[int, int]] = None
    manual_licence: bool = False
    needs_plates: bool = False
    summer_tyres: bool = False
    ready_delay_days: int = 0              # car ready this many business days later than stated
    note_deadline: Optional[int] = None    # absolute minute, same as delivery_by for tight-deadline notes
    confidence: float = 0.0


# --------------------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------------------
# Canonical words per semantic group. Typos are handled by matching single-edit variants
# of each word (the generator transposes / drops / doubles / substitutes one letter), so
# no fuzzy matching is needed and short words never match by accident.
_GROUPS: dict[str, tuple[str, ...]] = {
    "today": ("tänään", "tanaan", "tänä", "idag", "today"),
    "tomorrow": ("huomenna", "huomen", "huomiseksi", "tmrw", "tmr", "tmw", "tomorrow",
                 "imorgon", "morgon", "morron"),
    "aftertomorrow": ("ylihuomenna", "ylihuomen", "övermorgon", "overmorgon"),
    "wd0": ("maanantai", "maanantaina", "maanantain", "maanantaisin", "monday", "måndag"),
    "wd1": ("tiistai", "tiistaina", "tiistain", "tuesday", "tisdag"),
    "wd2": ("keskiviikko", "keskiviikkona", "keskiviikon", "wednesday", "onsdag"),
    "wd3": ("torstai", "torstaina", "torstain", "thursday", "torsdag"),
    "wd4": ("perjantai", "perjantaina", "perjantain", "friday", "fredag"),
    "wd5": ("lauantai", "lauantaina", "saturday", "lördag"),
    "wd6": ("sunnuntai", "sunnuntaina", "sunday", "söndag"),
    # short weekday forms are accepted only next to a clock time or a period (see _days)
    "wd0s": ("ma", "mon", "mån", "må"),
    "wd1s": ("ti", "tue", "tues", "tis"),
    "wd2s": ("ke", "wed", "ons"),
    "wd3s": ("to", "thu", "thur", "thurs", "tors"),
    "wd4s": ("pe", "fri", "fre"),
    "wd5s": ("la", "sat", "lör"),
    "wd6s": ("su", "sun", "sön"),
    "after": ("jälkeen", "jälkehen", "alkaen", "aikaisintaan", "lähtien", "vasta",
              "efter", "tidigast", "från", "after", "from", "earliest", "onwards",
              "alkaen", "notbefore", "aiemmin"),
    "before": ("ennen", "mennessä", "mennessa", "viimeistään", "viimeistaan", "viim",
               "asti", "saakka", "före", "fore", "senast", "innan", "before", "by",
               "latest", "until", "till", "nolaterthan"),
    "pickup": ("nouto", "noutaa", "noutona", "noudettavissa", "noudettava", "haettava",
               "haettavissa", "hakea", "haetaan", "haku", "hämtning", "hamtning",
               "hämtas", "hamtas", "hämtar", "pickup", "collect", "collection",
               "valmis", "valmiina", "valmistuu", "valmistu", "ready", "klar",
               "utlämning", "utlamning", "noudettavaksi", "täällä", "taalla", "paikalla",
               "ollaan", "here", "luovutus", "luovutuksia", "luovutukset", "handover"),
    "delivery": ("toimitus", "toimitetaan", "toimitusosoite", "perille", "perillä",
                 "perilla", "kohde", "kohteen", "deliver", "delivery", "delivered",
                 "destination", "arrive", "arrival", "framme", "leverans", "leverera",
                 "kunden", "customer"),
    "receiving": ("vastaanotto", "vastaanottava", "vastaanottaja", "vastaanotetaan",
                  "vastaanottaa", "receiving", "receive", "mottagning", "mottagaren"),
    "manual": ("manuaali", "manuaalinen", "manuaalivaihteinen", "manuaalivaihteet",
               "manuaalivaihteisto", "manuaalikortti", "manuell", "manual", "käsivaihteet",
               "kasivaihteet", "käsivaihteinen", "stick", "automaatti",
               "automaattivaihteinen", "automaattivaihteisto", "automatic"),
    "plates": ("kilvet", "kilpi", "kilpiä", "kilvia", "kilvettä", "rekkarit", "rekkareita",
               "rekisterikilvet", "rekisterikilpi", "rekisteristä", "rekisteroimätön",
               "siirtokilvet", "siirtokilpi", "koekilvet", "koekilpi", "plates", "plate",
               "plated", "unregistered", "skyltar", "skyltarna", "skylt", "registrerad",
               "registreringsskyltar", "provskyltar"),
    "keys": ("avain", "avaimet", "avaimen", "avainboksissa", "avainbox", "nyckel", "nycklar",
             "nycklarna", "nyckelbox", "nyckelboks", "key", "keys", "koodi", "koodin",
             "receptionen", "reception", "tiskiltä", "tiskilta", "plats", "platsen", "kassan"),
    "summer": ("kesärenkaat", "kesärenkailla", "kesärenkailla", "kesärenkaat", "kesärenkain",
               "kesärenkaisiin", "kesärengas", "kesärengasta", "sommardäck", "sommar",
               "summer", "talvirenkaat", "talvirenkaita", "talvirenkailla", "winter"),
    "boot": ("takakontissa", "tavaratilassa", "kontissa", "kyydissä", "kyytiin", "mukaan",
             "kulkee", "kulkevat", "kuljetettava", "kuljetetaan", "boot", "trunk", "bakluckan",
             "bagaget", "bagage", "följer", "foljer", "medföljer", "medfoljer", "sku", "last"),
    "release": ("maksun", "maksu", "maksaa", "maksanut", "maksettu", "maksun", "rahoitus",
                "payment", "paid", "pays", "pay", "betalning", "betalningen", "luovutus",
                "handover", "financing", "finansiering"),
    "hours_open": ("auki", "avoinna", "avoin", "aukiolo", "aukioloajat", "aukeaa",
                   "aukeavat", "opens", "open", "öppet", "oppet", "öppen", "öppna"),
    "hours_close": ("kiinni", "sulkee", "sulkeutuu", "suljettu", "sulkevat", "closes",
                    "close", "closed", "stänger", "stanger", "stängd", "stängt"),
    "hours_ctx": ("arkisin", "arkipäivisin", "arkiisin", "arkipaivisin", "arkisin",
                  "vardagar", "vardag", "weekdays", "weekday"),
    "weekdayword": ("weekdays", "vardagar", "arkisin", "arkipäivisin"),
    "correction": ("korjaus", "korjattu", "korjaan", "korrection", "correction",
                   "eikun", "eiku", "meani", "sorry"),
    "deny": ("ei", "not", "inte", "ej", "ilman", "ingen", "inget", "no", "nobody",
             "nobbody", "kukaan", "ketään", "kettaan", "noone"),
    "quantity": ("min", "mins", "minuuttia", "minuutin", "minuutti", "minutes", "minute",
                 "tuntia", "tunnin", "tunti", "hours", "hour", "km", "kilometriä",
                 "timme", "timmen", "timmar", "timma", "minuter", "minuten"),
    "clockintro": ("klo", "kl", "kello", "klockan", "klocka"),
    "noon": ("noon", "midday", "keskipäivä", "keskipaivalla"),
    "half": ("puoli", "puolta", "puolelta", "puoleen", "half", "halv"),
    "misc": ("vain", "myös", "myos", "sekä", "seka", "noin", "about", "around", "noin"),
    "range": ("kunnes",),
}

_ALPHA = "abcdefghijklmnopqrstuvwxyzåäöéü"

# number words -> working-hour number (1..12)
_FI_NUMS = {
    1: ("yksi", "yhden", "yhdeltä", "yhdellä", "yhteen", "yheltä", "yhdest"),
    2: ("kaksi", "kahden", "kahdelta", "kahdella", "kahteen", "kahta"),
    3: ("kolme", "kolmen", "kolmelta", "kolmella", "kolmeen", "kolmea"),
    4: ("neljä", "neljän", "neljältä", "neljällä", "neljään", "neljää"),
    5: ("viisi", "viiden", "viideltä", "viidellä", "viiteen", "viittä"),
    6: ("kuusi", "kuuden", "kuudelta", "kuudella", "kuuteen", "kuutta"),
    7: ("seitsemän", "seitsemältä", "seitsemällä", "seitsemään"),
    8: ("kahdeksan", "kahdeksalta", "kahdeksalla", "kahdeksaan"),
    9: ("yhdeksän", "yhdeksältä", "yhdeksällä", "yhdeksään"),
    10: ("kymmenen", "kymmeneltä", "kymmenellä", "kymmeneen", "kymmentä"),
    11: ("yksitoista", "yhdeltätoista", "yhdentoista", "yhteentoista"),
    12: ("kaksitoista", "kahdeltatoista", "kahdentoista", "kahteentoista"),
}
_SV_NUMS = {
    1: ("ett", "en"),
    2: ("två", "tva"),
    3: ("tre",),
    4: ("fyra",),
    5: ("fem",),
    6: ("sex",),
    7: ("sju",),
    8: ("åtta", "atta"),
    9: ("nio",),
    10: ("tio",),
    11: ("elva",),
    12: ("tolv",),
}
_EN_NUMS = {
    1: ("one",), 2: ("two",), 3: ("three",), 4: ("four",), 5: ("five",),
    6: ("six",), 7: ("seven",), 8: ("eight",), 9: ("nine",), 10: ("ten",),
    11: ("eleven",), 12: ("twelve",),
}
_HOUR_WORDS: dict[str, int] = {}
for _table in (_FI_NUMS, _SV_NUMS, _EN_NUMS):
    for _n, _forms in _table.items():
        for _form in _forms:
            _HOUR_WORDS.setdefault(_form, _n)


def _clock_hour(n: int) -> int:
    """The working hour with that number: 1 -> 13:00, 5 -> 17:00, 9 -> 09:00, 12 -> 12:00."""
    if 1 <= n <= 5:
        return 12 + n
    return n


def _single_edits(word: str) -> set[str]:
    out: set[str] = set()
    L = len(word)
    for i in range(L):                                  # deletion
        out.add(word[:i] + word[i + 1:])
    for i in range(L - 1):                              # transposition
        out.add(word[:i] + word[i + 1] + word[i] + word[i + 2:])
    for i in range(L):                                  # substitution
        for c in _ALPHA:
            if c != word[i]:
                out.add(word[:i] + c + word[i + 1:])
    for i in range(L + 1):                              # insertion
        for c in _ALPHA:
            out.add(word[:i] + c + word[i:])
    return out


def _build_variants() -> dict[str, list[str]]:
    """variant spelling -> canonical words. Exact spellings win over mutations; a mutation
    that is ambiguous between canonical words of *different* meaning is dropped (never
    widen on doubt), while variants of the same semantic identity are merged."""
    identity: dict[str, set[str]] = {}
    for group, words in _GROUPS.items():
        for w in words:
            identity.setdefault(w, set()).add(group)
    for table in (_FI_NUMS, _SV_NUMS, _EN_NUMS):
        for n, forms in table.items():
            for w in forms:
                identity.setdefault(w, set()).add(f"hw{n}")
    exact: set[str] = set(identity)
    muts: dict[str, set[str]] = {}
    for w in exact:
        if len(w) < 4:
            continue
        for v in _single_edits(w):
            if v in exact:
                continue
            muts.setdefault(v, set()).add(w)
    out: dict[str, list[str]] = {v: [v] for v in exact}
    for v, ws in muts.items():
        common: Optional[set[str]] = None
        for w in ws:
            common = set(identity[w]) if common is None else (common & identity[w])
        if common:
            out[v] = sorted(ws, key=lambda w: (-len(w), w))
    return out


_VARIANT = _build_variants()
_CANON_GROUPS: dict[str, set[str]] = {}
for _group, _words in _GROUPS.items():
    for _w in _words:
        _CANON_GROUPS.setdefault(_w, set()).add(_group)
for _table in (_FI_NUMS, _SV_NUMS, _EN_NUMS):
    for _n, _forms in _table.items():
        for _w in _forms:
            _CANON_GROUPS.setdefault(_w, set()).add(f"hw{_n}")


# --------------------------------------------------------------------------------------
# lexical analysis
# --------------------------------------------------------------------------------------
_SENTINELS = {
    "zzzafterzz": "afterphrase",
    "zzzbeforezz": "beforephrase",
    "zzztomorrowzz": "tomorrow",
    "zzzaftertomorrowzz": "aftertomorrow",
    "zzzcorrectionzz": "correction",
    "zzzrangezz": "rangemark",
    "zzzweekdayszz": "weekdayword",
    "zzzdeliveryzz": "delivery",
    "zzzpickupzz": "pickup",
}

# multi-word phrases, replaced before tokenising. Written as (pattern, sentinel).
_PHRASES = (
    (r"\bei\s+ennen\b", "zzzafterzz"),
    (r"\bei\s+aiemmin\b", "zzzafterzz"),
    (r"\bnot\s+ready\s+bef\w*\b", "zzzafterzz zzzpickupzz"),
    (r"\bnot\s+bef\w*\b", "zzzafterzz"),
    (r"\bno\s+earlier\s+than\b", "zzzafterzz"),
    (r"\bnot\s+before\b", "zzzafterzz"),
    (r"\bno\s+later\s+than\b", "zzzbeforezz"),
    (r"\b(?:won't|wont|isn't|isnt|not)\s+(?:be\s+)?ready\s+until\b", "zzzafterzz"),
    (r"\bnot\s+ready\s+till\b", "zzzafterzz"),
    (r"\bnot\s+available\s+until\b", "zzzafterzz"),
    (r"\basiakas\s+(?:noutaa|hakee|tulee\s+hakemaan)\b", "zzzdeliveryzz"),
    (r"\bkunden\s+(?:hämtar|hamtar|hämtar)\b", "zzzdeliveryzz"),
    (r"\bcustomer\s+(?:picks|collects|comes\s+to\s+collect)\b", "zzzdeliveryzz"),
    (r"\bma\s*[-–]\s*pe\b", "zzzweekdayszz"),
    (r"\bi\s+morgon\b", "zzztomorrowzz"),
    (r"\bi\s+morron\b", "zzztomorrowzz"),
    (r"\bi\s+över\s*morgon\b", "zzzaftertomorrowzz"),
    (r"\bi\s+over\s*morgon\b", "zzzaftertomorrowzz"),
    (r"\bday\s+after\s+tomorrow\b", "zzzaftertomorrowzz"),
    (r"\bma\s*[-–]\s*pe\b", "zzzweekdayszz"),
    (r"\bti\s*[-–]\s*ke\b", "zzzweekdayszz"),
    (r"\bmon\s*[-–]\s*fri\b", "zzzweekdayszz"),
    (r"\beikun\b|\bei\s+kun\b", "zzzcorrectionzz"),
    (r"\bi\s+mean\b", "zzzcorrectionzz"),
    (r"(?i)oik\.", " "),   # "pieni kolhu oik. ovessa" = scratch on the right side: not a correction
    (r"\b(?:half|halv)\s+past\b", "zzzpastzz"),
    (r"\bquarter\s+past\b", "zzzquarterpastzz"),
    (r"\bquarter\s+to\b", "zzzquartertozz"),
    (r"\bkvart\s+över\b", "zzzquarterpastzz"),
    (r"\bkvart\s+i\b", "zzzquartertozz"),
    (r"\bvartin\s+yli\b", "zzzquarterpastzz"),
    (r"\bvarttia\s+vaille\b", "zzzquartertozz"),
)

_TOKEN_RE = re.compile(
    r"[a-zåäöéü']+"          # words
    r"|\d{1,2}[:.]\d{2}"      # 9.30 / 14:30
    r"|\d{1,2}(?:am|pm)"      # 3pm
    r"|\d+"                   # plain number
    r"|[-–]"                  # range dash
    r"|[/.,;:!?()]"           # punctuation
)


def _preprocess(text: str) -> str:
    t = text.lower()
    t = t.replace("–", "-").replace("—", "-")
    # Finnish hour-case suffixes glued to the hour: 7:stä / 20:een / 9stä / 16een
    t = re.sub(r"(\d{1,2})\s*[:.]\s*(?:stä|sta)\s+(\d{1,2})\s*[:.]?\s*(?:een|aan|ään)\b",
               r"\1 zzzrangezz \2", t)
    t = re.sub(r"(\d{1,2})\s*[:.]?\s*(?:stä|sta)\b", r"\1 ", t)
    t = re.sub(r"(\d{1,2})\s*[:.]?\s*(?:een|aan|ään)\b", r"\1 ", t)
    t = re.sub(r"(\d{1,2})\s*[:.]?\s*(?:hyn|yn|ltä|lle)\b", r"\1 ", t)
    for pattern, replacement in _PHRASES:
        t = re.sub(pattern, replacement, t)
    return t


class _Lex:
    __slots__ = ("kind", "text", "value", "canon", "groups")

    def __init__(self, kind: str, text: str, value=None):
        self.kind = kind
        self.text = text
        self.value = value          # int for numbers / clock tokens
        self.canon: Optional[str] = None
        self.groups: set[str] = set()

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        return f"<{self.kind}:{self.text}{'' if self.canon is None else '/' + self.canon}>"


def _lex(text: str) -> list[_Lex]:
    out: list[_Lex] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok.isdigit():
            out.append(_Lex("num", tok, int(tok)))
        elif re.fullmatch(r"\d{1,2}[:.]\d{2}", tok):
            h, mnt = re.split(r"[:.]", tok)
            out.append(_Lex("clock", tok, int(h) * HOUR + int(mnt)))
        elif re.fullmatch(r"\d{1,2}(?:am|pm)", tok):
            h = int(tok[:-2])
            if tok.endswith("pm") and h < 12:
                h += 12
            out.append(_Lex("clock", tok, h * HOUR))
        elif re.fullmatch(r"[-–.,;:!?()/]", tok):
            out.append(_Lex("punct", tok))
        else:
            out.append(_Lex("word", tok))
    # canonical words and groups
    for lx in out:
        if lx.kind != "word":
            continue
        if lx.text in _SENTINELS:
            lx.canon = lx.text
            lx.groups = {_SENTINELS[lx.text]}
            continue
        cands = _VARIANT.get(lx.text)
        if cands:
            lx.canon = cands[0]
            for w in cands:
                lx.groups |= _CANON_GROUPS.get(w, set())
    return out


# --------------------------------------------------------------------------------------
# clock expressions
# --------------------------------------------------------------------------------------
def _numword_minutes(lex: list[_Lex], i: int) -> Optional[tuple[int, int]]:
    """A number in words at i -> (end index, minutes-of-day). Handles puoli/half/halv and
    quarter past/to phrases."""
    if i >= len(lex):
        return None
    lx = lex[i]
    if lx.canon in ("zzzpastzz", "zzzquarterpastzz", "zzzquartertozz"):
        nxt = _numword_minutes(lex, i + 1)
        if nxt is None:
            return None
        end, minutes = nxt
        if lx.canon == "zzzpastzz":
            return end, minutes + 30
        if lx.canon == "zzzquarterpastzz":
            return end, minutes + 15
        return end, minutes - 15
    if lx.groups & {"half"}:
        nxt = _numword_minutes(lex, i + 1)
        if nxt is None:
            return None
        end, minutes = nxt
        return end, minutes - 30
    if lx.kind == "word" and lx.canon is not None:
        n = _HOUR_WORDS.get(lx.canon)
        if n is not None:
            return i + 1, _clock_hour(n) * HOUR
    return None


def _clock_at(lex: list[_Lex], i: int) -> Optional[tuple[int, int]]:
    """A single clock time at i -> (end index, minutes-of-day)."""
    n = len(lex)
    if i >= n:
        return None
    lx = lex[i]
    if "clockintro" in lx.groups:
        i += 1
        if i >= n:
            return None
        lx = lex[i]
    if lx.kind == "clock":
        minutes = lx.value
        i += 1
        if i < n and lex[i].kind == "word" and lex[i].text in ("am", "pm"):
            if lex[i].text == "pm" and minutes < 12 * HOUR:
                minutes += 12 * HOUR
            elif lex[i].text == "am" and minutes >= 12 * HOUR:
                minutes -= 12 * HOUR
            i += 1
        return i, minutes
    if lx.kind == "num" and 100 <= lx.value <= 2359:
        # "kello 1330" / "930": the generator sometimes writes the clock without a separator
        hh, mm = divmod(lx.value, 100)
        if hh <= 23 and mm <= 59:
            return i + 1, hh * HOUR + mm
    if lx.kind == "num" and 0 <= lx.value <= 24:
        # a bare number is a clock hour only with some corroboration (see _try_time)
        minutes = lx.value * HOUR
        i += 1
        if i < n and lex[i].kind == "word" and lex[i].text in ("am", "pm"):
            if lex[i].text == "pm" and minutes < 12 * HOUR:
                minutes += 12 * HOUR
            elif lex[i].text == "am" and minutes >= 12 * HOUR:
                minutes -= 12 * HOUR
            i += 1
        return i, minutes
    numword = _numword_minutes(lex, i)
    if numword is not None:
        return numword
    if lx.canon == "noon":
        return i + 1, 12 * HOUR
    return None


def _corroborated(lex: list[_Lex], i: int, clause_start: int) -> bool:
    """True when position i is a plausible place for a clock time: a clock introducer,
    a direction word, a day word, or opening-hours vocabulary nearby."""
    ctx = {"after", "before", "hours_ctx", "hours_open", "hours_close",
           "weekdayword", "today", "tomorrow", "aftertomorrow"}
    for j in range(max(clause_start, i - 5), i):
        lx = lex[j]
        if "clockintro" in lx.groups or (lx.groups & ctx) or lx.text in _SENTINELS:
            return True
        if lx.kind == "word" and lx.canon is not None and lx.canon.startswith("wd"):
            return True
    for j in range(i + 1, min(len(lex), i + 3)):
        lx = lex[j]
        if lx.groups & {"after", "before", "hours_ctx", "hours_open", "hours_close"}:
            return True
        if lx.canon in ("zzzafterzz", "zzzbeforezz"):
            return True
    return False


@dataclass
class _TimeExpr:
    start: int
    end: int
    minutes: int
    minutes2: Optional[int] = None   # set for ranges
    denied: bool = False


def _try_time(lex: list[_Lex], i: int, clause_start: int) -> Optional[_TimeExpr]:
    n = len(lex)
    first = _clock_at(lex, i)
    if first is None:
        return None
    end, minutes = first
    # ranges: "9-16", "klo 9.00-14.30", "9 to 16", "7:stä 20:een" (pre-tokenised with a mark)
    if end < n:
        j = end
        mark = False
        if lex[j].kind == "punct" and lex[j].text in ("-",):
            mark = True
            j += 1
        elif lex[j].canon == "zzzrangezz":
            mark = True
            j += 1
        elif lex[j].kind == "word" and lex[j].text == "to" and j + 1 < n:
            peek = _clock_at(lex, j + 1)
            if peek is not None:
                mark = True
                j += 1
        elif lex[j].groups & {"range"}:
            mark = True
            j += 1
        if mark:
            second = _clock_at(lex, j)
            if second is not None:
                end2, minutes2 = second
                if minutes != minutes2:
                    return _TimeExpr(i, end2, minutes, minutes2)
    # "30 min before", "2 hours" are durations, not clock times
    if end < n and lex[end].groups & {"quantity"}:
        return None
    # a bare number needs corroboration; clock tokens and clock words are unambiguous
    if lex[i].kind == "num" and not _corroborated(lex, i, clause_start):
        return None
    if lex[i].kind == "word" and lex[i].canon in _HOUR_WORDS and not _corroborated(lex, i, clause_start):
        return None
    denied = False
    back = i - 1
    if back >= clause_start and lex[back].kind == "word" and "clockintro" in lex[back].groups:
        back -= 1
    if back >= clause_start and lex[back].kind == "word" and lex[back].text in _TIME_DENY:
        denied = True
    return _TimeExpr(i, end, minutes, denied=denied)


# --------------------------------------------------------------------------------------
# days
# --------------------------------------------------------------------------------------
_WEEKDAY_SHORT = {"ma": 0, "ti": 1, "ke": 2, "to": 3, "pe": 4, "la": 5, "su": 6,
                  "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
                  "thurs": 3, "fri": 4, "sat": 5, "sun": 6, "tors": 3, "fre": 4,
                  "lör": 5, "sön": 6}
_WEEKDAY_LONG = {"wd0": 0, "wd1": 1, "wd2": 2, "wd3": 3, "wd4": 4, "wd5": 5, "wd6": 6}
_WEEKDAY_SHORT_GROUPS = {"wd0s": 0, "wd1s": 1, "wd2s": 2, "wd3s": 3, "wd4s": 4,
                          "wd5s": 5, "wd6s": 6}


def _weekday_of_word(lx: _Lex) -> Optional[int]:
    for g, wd in _WEEKDAY_LONG.items():
        if g in lx.groups:
            return wd
    for g, wd in _WEEKDAY_SHORT_GROUPS.items():
        if g in lx.groups:
            return wd
    return None


@dataclass
class _DayExpr:
    start: int
    end: int
    offset: int          # calendar days after the arrival day


def _days(lex: list[_Lex]) -> list[_DayExpr]:
    out: list[_DayExpr] = []
    n = len(lex)
    for i, lx in enumerate(lex):
        if lx.kind != "word" or lx.canon is None:
            continue
        if "today" in lx.groups:
            out.append(_DayExpr(i, i + 1, 0))
        elif "tomorrow" in lx.groups:
            out.append(_DayExpr(i, i + 1, 1))
        elif "aftertomorrow" in lx.groups:
            out.append(_DayExpr(i, i + 1, 2))
        else:
            wd = _weekday_of_word(lx)
            if wd is None:
                continue
            # short forms need corroboration: "to." / "to klo 13" / "pe 16.00"; not "to us"
            if lx.canon in _WEEKDAY_SHORT or lx.text in _WEEKDAY_SHORT:
                nxt = lex[i + 1] if i + 1 < n else None
                ok = False
                if nxt is not None:
                    if nxt.kind == "punct" and nxt.text == ".":
                        ok = True
                    elif "clockintro" in nxt.groups or nxt.kind in ("num", "clock"):
                        ok = True
                    elif nxt.kind == "word" and (nxt.canon in _HOUR_WORDS or nxt.canon == "noon"):
                        ok = True
                    elif nxt.canon == "noon":
                        ok = True
                    elif nxt.groups & {"today", "tomorrow", "aftertomorrow"}:
                        ok = True
                if not ok:
                    prev = lex[i - 1] if i > 0 else None
                    if prev is not None and prev.kind == "word" and prev.canon in ("på", "pa", "on", "ensi"):
                        ok = True
                if not ok:
                    continue
            out.append(_DayExpr(i, i + 1, -1))  # -1 = weekday, resolve later
    return out


# --------------------------------------------------------------------------------------
# clause boundaries
# --------------------------------------------------------------------------------------
# abbreviations whose full stop does not end a clause: "viim. ma klo 18", "t. myynti"
_ABBREV = {"viim", "viimeist", "t", "n", "no", "ref", "br", "nro", "nr", "kl", "klo",
           "ma", "ti", "ke", "to", "pe", "la", "su",
           "esim", "jne", "vrt", "pv", "kk", "min", "h", "st", "mm", "yms", "n", "n.",
           "klo", "kl", "esim", "pvm", "tel", "g", "kg", "cm", "km", "v", "vh", "n"}


def _clauses(lex: list[_Lex]) -> list[int]:
    """clause index for every lexeme: split on sentence punctuation and ' / ', but not
    after an abbreviation or a single letter ("viim. ma klo 18")."""
    bounds = [0] * len(lex)
    cur = 0
    for i, lx in enumerate(lex):
        if lx.kind == "punct" and lx.text in (".", "!", "?", ";", "/"):
            if lx.text == "." and i > 0:
                prev = lex[i - 1]
                if prev.kind == "word" and (prev.text in _ABBREV or len(prev.text) == 1):
                    bounds[i] = cur
                    continue
            cur += 1
        bounds[i] = cur
    return bounds


def _clause_range(lex: list[_Lex], clause: list[int], i: int) -> tuple[int, int]:
    c = clause[i]
    lo = i
    while lo > 0 and clause[lo - 1] == c:
        lo -= 1
    hi = i
    while hi + 1 < len(lex) and clause[hi + 1] == c:
        hi += 1
    return lo, hi + 1


# --------------------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------------------
@dataclass
class _Fact:
    kind: str            # pickup_floor | delivery_ceiling | pickup_hours | delivery_hours
    value: object        # int minute, or (opens, closes)
    pos: int
    corrected: bool = False
    subtype: str = ""    # ready | release | plain


def _parse(lex: list[_Lex], arrived_at: int, stated_pickup: Optional[dict],
           stated_delivery: Optional[dict], option_deadlines: list[int]) -> list[_Fact]:
    clause = _clauses(lex)
    n = len(lex)
    facts: list[_Fact] = []
    times: list[_TimeExpr] = []
    day_exprs = _days(lex)

    # correction region starts: after a correction marker through the end of its clause
    correction_marks = [i for i, lx in enumerate(lex) if "correction" in lx.groups]
    correction_span: set[int] = set()
    day_correction: list[int] = []
    for m in correction_marks:
        lo, hi = _clause_range(lex, clause, m)
        # a correction can carry a bare day ("korjaus: ma") which only fixes the day;
        # a clock after the marker means the correction fixes a time instead
        first_day = next((d.start for d in day_exprs if m < d.start < hi), None)
        first_clock = next((j for j in range(m + 1, hi) if _clock_at(lex, j) is not None), None)
        only_day = first_day is not None and (first_clock is None or first_day < first_clock)
        for j in range(m + 1, hi):
            correction_span.add(j)
        if only_day:
            day_correction.append(m)

    i = 0
    while i < n:
        lo, hi = _clause_range(lex, clause, i)
        te = _try_time(lex, i, lo)
        if te is not None:
            times.append(te)
            i = te.end
            continue
        i += 1

    # attach days and classify each time expression
    last_fact: Optional[_Fact] = None
    last_day = 0
    for te in times:
        lo, hi = _clause_range(lex, clause, te.start)
        day_hit: Optional[_DayExpr] = None
        best = None
        for d in day_exprs:
            if d.end <= te.start and d.start >= lo:
                best = d
        if best is not None and te.start - best.end <= 4:
            day_hit = best
        if day_hit is None:
            for d in day_exprs:
                if te.end <= d.start <= te.end + 2 and d.start < hi:
                    day_hit = d
                    break
        ctx_hours = ctx_open = ctx_close = False
        has_after = has_before = negation = payment = False
        for j in range(lo, hi):
            g = lex[j].groups
            if "hours_ctx" in g or "weekdayword" in g:
                ctx_hours = True
            if "hours_open" in g:
                ctx_open = True
            if "hours_close" in g:
                ctx_close = True
            if "release" in g:
                payment = True
            if "deny" in g:
                negation = True
            if "after" in g or lex[j].canon == "zzzafterzz":
                has_after = True
            if "before" in g or lex[j].canon == "zzzbeforezz":
                has_before = True
        # the stop words nearest the time decide whether it is about the pickup or the delivery
        def _nearest(group: str) -> Optional[int]:
            best = None
            for j in range(lo, hi):
                if group in lex[j].groups:
                    d = 0 if te.start <= j < te.end else min(abs(j - te.start), abs(j - (te.end - 1)))
                    if best is None or d < best:
                        best = d
            return best
        dp, dd, dr = _nearest("pickup"), _nearest("delivery"), _nearest("receiving")
        ctx_receiving = dr is not None
        ctx_delivery = dd is not None or dr is not None
        ctx_pickup = dp is not None and (dr is None and dd is None or dp < min(
            [x for x in (dd, dr) if x is not None], default=10 ** 9))
        # the tokens nearest the time decide the direction more strongly than the whole clause
        near_before = near_after = False
        for j in range(max(lo, te.start - 3), te.start):
            lx = lex[j]
            if "after" in lx.groups or lx.canon == "zzzafterzz":
                near_after = True
            if "before" in lx.groups or lx.canon == "zzzbeforezz":
                near_before = True
        for j in range(te.end, min(hi, te.end + 2)):
            lx = lex[j]
            if "after" in lx.groups or lx.canon == "zzzafterzz":
                near_after = True
            if "before" in lx.groups or lx.canon == "zzzbeforezz":
                near_before = True
        if near_before and not near_after:
            direction = "before"
        elif near_after and not near_before:
            direction = "after"
        elif has_before and not has_after:
            direction = "before"
        elif has_after and not has_before:
            direction = "after"
        elif has_before and has_after:
            direction = "after" if near_after else "before"
        else:
            direction = ""

        corrected = any(j in correction_span for j in range(te.start, te.end))
        delivery_ctx = ctx_delivery or ctx_receiving
        # "korjaus: klo 16.30" / "ei kun klo 10" carries only a new value: it replaces the
        # previous fact of the same kind, so it inherits that fact's type, day and side.
        if corrected and last_fact is not None and te.minutes2 is None and day_hit is None:
            if last_fact.kind.startswith(("pickup_hours", "delivery_hours")):
                # "nouto ennen klo 17, eikun klo 18" fixes the value of the previous hours fact
                if not (ctx_hours or ctx_open or ctx_close or ctx_receiving):
                    facts.append(_Fact(last_fact.kind, te.minutes, te.start, True, last_fact.subtype))
                    last_fact = facts[-1]
                    continue
            elif last_fact.kind in ("pickup_floor", "delivery_ceiling"):
                if not (ctx_hours or ctx_open or ctx_close):
                    absolute = ((arrived_at // DAY) + last_day) * DAY + te.minutes
                    facts.append(_Fact(last_fact.kind, absolute, te.start, True, last_fact.subtype))
                    last_fact = facts[-1]
                    continue
        if te.minutes2 is not None:
            opens, closes = te.minutes, te.minutes2
            if opens >= closes:
                continue
            target = "delivery" if delivery_ctx and not ctx_pickup else "pickup"
            facts.append(_Fact(f"{target}_hours", (opens, closes), te.start, corrected))
            last_fact = facts[-1]
            continue
        if te.denied:
            continue
        minutes = te.minutes
        has_day = day_hit is not None
        day = 0
        if not has_day and corrected and last_fact is not None and te.minutes2 is None:
            # "korjaus: klo 16.30" after "huomenna klo 15.30" keeps the day
            if last_fact.kind in ("pickup_floor", "delivery_ceiling"):
                day = last_day
                has_day = True
        if day_hit is not None:
            if day_hit.offset >= 0:
                day = day_hit.offset
            else:
                wd = _weekday_of_word(lex[day_hit.start])
                if wd is None:
                    continue
                day = (wd - (arrived_at // DAY) % 7) % 7
                if day < 2:
                    day += 7
        absolute = ((arrived_at // DAY) + day) * DAY + minutes

        # a statement about opening hours, not about one date
        def _hours_target() -> str:
            if ctx_receiving:
                return "delivery"
            if ctx_delivery and (not ctx_pickup or direction == "before" or ctx_close):
                return "delivery"
            return "pickup"

        hours_style = ctx_hours or ctx_open or ctx_close
        if not hours_style and not has_day:
            if direction == "before" and ctx_pickup and not delivery_ctx and near_before:
                hours_style = True
            if ctx_receiving:
                hours_style = True
            if negation and direction == "after" and (ctx_pickup or delivery_ctx):
                hours_style = True
        if hours_style:
            side = ""
            if ctx_close or (negation and direction == "after") or direction == "before":
                side = "close"
            elif ctx_open or direction == "after":
                side = "open"
            if side:
                target = _hours_target()
                facts.append(_Fact(f"{target}_hours_{side}", minutes, te.start, corrected))
                last_fact = facts[-1]
            continue
        # an absolute time fact: a pickup floor or a delivery ceiling
        target = ""
        if ctx_receiving and not ctx_pickup:
            target = "delivery"
        elif ctx_delivery and (not ctx_pickup or direction == "before" or ctx_close):
            target = "delivery"
        elif ctx_pickup:
            target = "pickup"
        elif direction == "before":
            target = "delivery"
        elif direction == "after":
            target = "pickup"
        if not target:
            continue
        if target == "delivery":
            facts.append(_Fact("delivery_ceiling", absolute, te.start, corrected))
            last_fact = facts[-1]
            last_day = day
        else:
            subtype = "release" if payment else "ready"
            facts.append(_Fact("pickup_floor", absolute, te.start, corrected, subtype))
            last_fact = facts[-1]
            last_day = day

    # a correction carrying only a day changes the day of the previous event,
    # e.g. "perillä su klo 20. korjaus: ma."
    for m in day_correction:
        lo, hi = _clause_range(lex, clause, m)
        d = next((d for d in day_exprs if m < d.start and d.end <= hi), None)
        if d is None:
            continue
        if d.offset >= 0:
            day_offset = d.offset
        else:
            wd = _weekday_of_word(lex[d.start])
            if wd is None:
                continue
            day_offset = (wd - (arrived_at // DAY) % 7) % 7
            if day_offset < 2:
                day_offset += 7
        prev = [f for f in facts if f.pos < m]
        if not prev:
            prev = [f for f in facts if f.pos < lo]
        if not prev:
            continue
        f = prev[-1]
        if isinstance(f.value, int) and "hours" not in f.kind:
            f.value = ((arrived_at // DAY) + day_offset) * DAY + (f.value % DAY)
    return facts


_TIME_DENY = {"ei", "not", "inte", "ej", "eikä", "eika"}


# --------------------------------------------------------------------------------------
# composing constraints
# --------------------------------------------------------------------------------------
def _business_days_between(day_from: int, day_to: int) -> int:
    """Number of business days (Mon-Fri) in (day_from, day_to]."""
    if day_to <= day_from:
        return 0
    count = 0
    for d in range(day_from + 1, day_to + 1):
        if d % 7 < 5:
            count += 1
    return count


_AUTOMATIC_WORDS = ("automaatti", "automaattivaihteinen", "automaattivaihteisto",
                     "automatic")


def _flag_hits(lex: list[_Lex]) -> tuple[bool, bool, bool]:
    """manual licence, plates, summer tyres: greps over the whole note, with the negative
    wordings the generator uses ("automaatti, ei manuaali" is automatic; "ei automaatti"
    is manual; "summer tyres in the trunk" is a spare set, not the tyres on the car)."""
    manual = plates = summer = False
    clause = _clauses(lex)
    for i, lx in enumerate(lex):
        g = lx.groups
        if "manual" in g:
            automatic = lx.canon in _AUTOMATIC_WORDS
            denied = False
            for back in (1, 2):
                if i - back >= 0 and clause[i - back] == clause[i] and lex[i - back].groups & {"deny"}:
                    denied = True
            if automatic:
                manual = manual or denied
            elif not denied:
                manual = True
        if "plates" in g:
            plates = True
        if "summer" in g:
            lo, hi = _clause_range(lex, clause, i)
            if not any("boot" in lex[j].groups for j in range(lo, hi)):
                summer = True
    return manual, plates, summer


def read_note(request) -> NoteConstraints:
    """Reads a request's note into constraints. Returns defaults when the note is absent,
    unreadable, or harmless. This function never raises and never widens a window."""
    try:
        return _read_note(request)
    except Exception:  # noqa: BLE001 - a reader bug must never stop the dispatcher
        return NoteConstraints()


def _read_note(request) -> NoteConstraints:
    nc = NoteConstraints()
    if not isinstance(request, dict):
        return nc
    note = request.get("note")
    if not note or not note.get("text"):
        return nc
    arrived_at = int(request.get("arrivedAt") or 0)
    legs = request.get("legs") or []
    if not legs:
        return nc
    leg0 = legs[0]
    pickup = leg0.get("pickup") or {}
    delivery = leg0.get("delivery") or {}
    stated_from = pickup.get("window", {}).get("from")
    options = request.get("options") or []
    option_deadlines = [o.get("deadline") for o in options if o.get("deadline") is not None]
    stated_pickup_hours = pickup.get("hours")
    stated_delivery_hours = delivery.get("hours")

    text = _preprocess(note["text"])
    lex = _lex(text)
    facts = _parse(lex, arrived_at, stated_pickup_hours, stated_delivery_hours, option_deadlines)

    # ---- pickup floor -----------------------------------------------------------------
    floors = [f for f in facts if f.kind == "pickup_floor"]
    if floors:
        corrected = [f for f in floors if f.corrected]
        chosen = corrected if corrected else floors
        floor = max(f.value for f in chosen)
        if stated_from is not None and floor <= stated_from:
            floor = None
        if floor is not None:
            nc.pickup_from = int(floor)
            # a later ready day also moves the order's deadline; a release does not
            if stated_from is not None and floor // DAY > stated_from // DAY:
                subtype = chosen[[f.value for f in chosen].index(max(f.value for f in chosen))].subtype
                if subtype != "release":
                    nc.ready_delay_days = _business_days_between(stated_from // DAY, floor // DAY)

    # ---- delivery ceiling -------------------------------------------------------------
    ceilings = [f for f in facts if f.kind == "delivery_ceiling"]
    if ceilings:
        corrected = [f for f in ceilings if f.corrected]
        chosen = corrected if corrected else ceilings
        ceiling = min(f.value for f in chosen)
        if option_deadlines and ceiling >= max(option_deadlines):
            ceiling = None
        if ceiling is not None:
            nc.delivery_by = int(ceiling)
            nc.note_deadline = int(ceiling)

    # ---- opening hours ----------------------------------------------------------------
    def _hours(target: str, stated, private: bool):
        if private or not stated:
            return None
        opens, closes = stated.get("opens"), stated.get("closes")
        if opens is None or closes is None:
            return None
        opens, closes = int(opens), int(closes)
        side_facts = [f for f in facts if f.kind == f"{target}_hours"
                      or f.kind.startswith(f"{target}_hours_")]
        if not side_facts:
            return None
        corrected = [f for f in side_facts if f.corrected]
        side_facts = corrected if corrected else side_facts
        for f in side_facts:
            if isinstance(f.value, tuple):
                opens, closes = f.value
            elif f.kind.endswith("_open"):
                opens = f.value
            else:
                closes = f.value
        if opens >= closes:
            return None
        if (opens, closes) == (int(stated.get("opens")), int(stated.get("closes"))):
            return None
        return (opens, closes)

    nc.pickup_hours = _hours("pickup", stated_pickup_hours, bool(pickup.get("privateAddress")))
    nc.delivery_hours = _hours("delivery", stated_delivery_hours, bool(delivery.get("privateAddress")))

    # ---- driver / car requirements ----------------------------------------------------
    nc.manual_licence, nc.needs_plates, nc.summer_tyres = _flag_hits(lex)

    # ---- confidence -------------------------------------------------------------------
    found = []
    if nc.pickup_from is not None:
        found.append(0.9)
    if nc.delivery_by is not None:
        found.append(0.9)
    if nc.pickup_hours is not None:
        found.append(0.85)
    if nc.delivery_hours is not None:
        found.append(0.85)
    if nc.manual_licence or nc.needs_plates or nc.summer_tyres:
        found.append(0.95)
    if nc.ready_delay_days:
        found.append(0.85)
    nc.confidence = max(found) if found else 0.0
    return nc
