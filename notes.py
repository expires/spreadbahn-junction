"""Dealer-note reader for Spread.

``parse_note(text, language, arrived_at)`` turns a free-text dealer note (Finnish, Swedish,
English, often mixed and with typos) into the structured constraints the world enforces:

    ready_from       absolute minute or None  earliest pickup (ready time or payment release)
    released         bool                     the ready time comes from a payment/handover release
    pickup_opens     minute-of-day or None    pickup site opens later than stated (weekdays)
    pickup_closes    minute-of-day or None    pickup site closes earlier than stated (weekdays)
    delivery_opens   minute-of-day or None    delivery site opens later than stated (weekdays)
    delivery_closes  minute-of-day or None    delivery site closes earlier than stated (weekdays)
    deadline         absolute minute or None  delivery deadline stated in the note
    manual           bool                     manual gearbox -> driver needs a manual licence
    plates           bool                     car has no plates -> gig stop needs withPlates
    summer_tyres     bool                     car is on summer tyres (cannot be collected in snow)

A note only ever narrows what the request states, so combine with the request by
max()/min() (see ``apply_to_stops``).  Hours in the note apply on weekdays; Saturday hours
are never changed by a note.  Every fact in a note concerns the FIRST leg of the request
(multi-leg requests are "new car out + trade-in back"; the note is about the new car).

Pure stdlib, ~0.03 ms per note (worst seen < 1 ms with a cold typo cache).  Measured against the reveal ground truth by eval_notes.py.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

DAY = 1440
HOUR = 60

# ----------------------------------------------------------------------------------------
# Vocabulary.  All text is lower-cased and folded to ASCII (ä->a, ö->o, å->a) first.
# ----------------------------------------------------------------------------------------

# word hours: working hour with that number (9..17)
_FI_NUM_FORMS: dict[int, list[str]] = {
    9: ["yhdeksan", "yhdeksalta", "yhdeksasta", "yhdeksaan", "yhdeksaa"],
    10: ["kymmenen", "kymmenelta", "kymmenesta", "kymmeneen", "kymmenta"],
    11: ["yksitoista", "yhdeltatoista", "yhdestatoista", "yhdentoista", "yhteentoista", "yhtatoista"],
    12: ["kaksitoista", "kahdeltatoista", "kahdestatoista", "kahdentoista", "kahteentoista", "kahtatoista"],
    13: ["yksi", "yhdelta", "yhdesta", "yhden", "yhteen", "yhta"],
    14: ["kaksi", "kahdelta", "kahdesta", "kahden", "kahteen", "kahta"],
    15: ["kolme", "kolmelta", "kolmesta", "kolmen", "kolmeen", "kolmea"],
    16: ["nelja", "neljalta", "neljasta", "neljan", "neljaan", "neljaa"],
    17: ["viisi", "viidelta", "viidesta", "viiden", "viiteen", "viitta"],
}
# forms that are unambiguous clock times on their own ("at ten", "from three")
_FI_STRONG_SUFFIX = ("lta", "sta", "ltatoista", "statoista")
_SV_NUM = {"nio": 9, "tio": 10, "elva": 11, "tolv": 12, "ett": 13, "tva": 14, "tre": 15, "fyra": 16, "fem": 17}
_EN_NUM = {"nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "one": 13, "two": 14, "three": 15, "four": 16,
           "five": 17}

WORD_HOUR: dict[str, tuple[int, str]] = {}  # word -> (hour, lang)
for _h, _forms in _FI_NUM_FORMS.items():
    for _f in _forms:
        WORD_HOUR[_f] = (_h, "fi")
for _w, _h in _SV_NUM.items():
    WORD_HOUR[_w] = (_h, "sv")
for _w, _h in _EN_NUM.items():
    WORD_HOUR[_w] = (_h, "en")
NOON = {"noon", "midday", "keskipaivalla", "keskipaiva", "middag", "lunch"}
HALF = {"puoli", "puolta", "puolen", "half", "halv"}
CLOCK_WORDS = {"klo", "kl", "kello", "klockan", "kloo", "kelo"}
TIME_PREP = {"at", "by", "from", "before", "after", "until", "till", "ennen", "jalkeen", "alkaen", "fran", "past",
             "efter", "fore", "senast", "tidigast", "noon", "latest", "earliest", "around", "about"}
TIME_POST = {"jalkeen", "mennessa", "alkaen", "saakka", "asti", "am", "pm", "latest", "lattest",
             "oclock", "o'clock", "and", "och", "only", "onwards"}

# Day words: offset in days from the arrival day
DAY_WORDS = {
    "tanaan": 0, "idag": 0, "today": 0, "tonight": 0, "tanaanilla": 0,
    "huomenna": 1, "imorgon": 1, "tomorrow": 1, "tmrw": 1, "tmr": 1, "tmrow": 1, "tomoz": 1,
    "huomisaamuna": 1, "ylihuomenna": 2, "iovermorgon": 2, "overmorgon": 2, "morgon": 1,
}
# weekday names: 0=Mon .. 6=Sun.  "long" forms always count, short forms only next to a time.
WEEKDAY_LONG = {}
for _i, _names in enumerate([
    ["maanantai", "maanantaina", "maanantaihin", "maanantaiksi", "mandag", "mandagen", "monday"],
    ["tiistai", "tiistaina", "tiistaihin", "tisdag", "tisdagen", "tuesday"],
    ["keskiviikko", "keskiviikkona", "keskiviikkoon", "onsdag", "onsdagen", "wednesday"],
    ["torstai", "torstaina", "torstaihin", "torsdag", "torsdagen", "thursday"],
    ["perjantai", "perjantaina", "perjantaihin", "fredag", "fredagen", "friday"],
    ["lauantai", "lauantaina", "lordag", "lordagen", "saturday"],
    ["sunnuntai", "sunnuntaina", "sondag", "sondagen", "sunday"],
]):
    for _n in _names:
        WEEKDAY_LONG[_n] = _i
WEEKDAY_SHORT = {
    "ma": 0, "mon": 0, "man": 0, "ti": 1, "tue": 1, "tues": 1, "tis": 1, "ke": 2, "wed": 2, "ons": 2,
    "to": 3, "thu": 3, "thur": 3, "thurs": 3, "tors": 3, "pe": 4, "fri": 4, "fre": 4,
    "la": 5, "sat": 5, "lor": 5, "su": 6, "sun": 6, "son": 6,
}

# Clause-classification keyword stems (prefix match on corrected tokens)
DELIVERY_STEMS = ("kohde", "kohtee", "vastaanot", "perilla", "perille", "toimitus", "destination", "receiv",
                  "recipient", "deliver", "arrival", "arrive", "mottag", "framme", "leverans", "levere")
PRIVATE_STEMS = ("kotona", "home", "hemma", "omistaja", "myyja", "seller", "owner", "saljare")
RELEASE_STEMS = ("maksu", "maksa", "maksettu", "maksun", "payment", "pays", "paid", "betal", "rahoitus",
                 "financ", "kauppa", "luovutus", "handover", "released", "release", "utlamning", "lamnas")
CLOSE_STEMS = ("kiinni", "sulke", "sulje", "suljet", "close", "closes", "closed", "closing", "stanger",
               "stang", "nobody", "noone")
WEAK_CLOSE_STEMS = ("asti", "saakka", "until", "till")  # "won't be ready until 2pm" is a ready time
CLOSE_PHRASES = ("ei enaa", "no more", "ingen utlamning", "open until", "paikalla vain", "no one here",
                 "nobody here", "ei ketaan", "ingen har", "ingen pa plats")
READY_STEMS = ("valmis", "valmistu", "valmii", "ready", "klar", "alkaen", "from", "fran", "jalkeen", "after",
               "efter", "aikaisintaan", "earliest", "tidigast", "framat", "onwards", "noudettavissa",
               "haettavissa", "vasta", "available")
READY_PHRASES = ("not before", "ei ennen", "inte fore", "voi noutaa", "saa hakea", "kan hamtas", "auton saa",
                 "no pickup before")
BEFORE_STEMS = ("ennen", "before", "fore", "innan", "senast", "viimeistaan", "viim", "latest", "by",
                "mennessa", "haku", "haettava", "nouto")
CUSTOMER_PICK = ("asiakas noutaa", "asiakas tulee hakemaan", "asiakas hakee", "customer picks",
                 "customer collects", "kunden hamtar", "must be there by then", "oltava silloin perilla")
OPENS_STEMS = ("avautuu", "opens", "oppnar", "aukeaa")

NEGATORS = {"ei", "not", "inte", "eika", "no"}

FLAG_MANUAL = re.compile(r"manu+a+l|manuell|kasivaihte|manual|stick ?shift")
FLAG_PLATES = re.compile(
    r"kilp|kilv|rekkar|plates|plate\b|skylt|unregist|unrgeist|rekisterista|rekisterist|deregist|"
    r"rekisterikilv|ilman rekister|avregistr")
FLAG_SUMMER_ON = re.compile(
    r"kesarenkailla|kesarenkaat alla|kesarenkaat on|vielakesa|viela kesarenka|ei talvirenka|"
    r"talvirenkaita ei|talvet puuttu|talvirenkaat puuttu|summer tyres on|summer tires on|still on summer|"
    r"no winters|no winter tyres|sommardack pa|har sommardack(?!en i)|fortfarande sommardack|sommardacken pa|inga vinterdack|"
    r"talvirenkaita ei viela|ei viela talvirenka|on summer tyres|on summer tires")

# ----------------------------------------------------------------------------------------
# Fuzzy correction of typos ("huoomenna", "befre", "klomelta", "Unrgeistered", ...)
# ----------------------------------------------------------------------------------------
_VOCAB = set(WORD_HOUR) | NOON | HALF | CLOCK_WORDS | set(DAY_WORDS) | set(WEEKDAY_LONG) | set("""
valmis valmistuu valmiina ready alkaen jalkeen aikaisintaan earliest tidigast noudettavissa haettavissa noutaa
hakea vasta before after ennen efter mennessa viimeistaan viim perilla perille toimitus toimitusosoite kohde
kohteen sulkee kiinni auki open oppet vastaanotto vastaanottava vastaanottaja destination receiving closes close
latest until maksun maksaa maksettu payment luovutus release released korjaus correction eikun huolto kesken
workshop liike arkisin arkipaivisin weekdays pickup collect mottagning mottagaren framme senast hamtas hamtning
klar forst framat saakka asti aukioloajat nouto haku haettava oltava asiakas hakemaan noudettavaksi stanger
deliver arrival manuaali manuaalia manuaalikortin manuaalivaihteet manuaalivaihteinen manual manuell gearbox
transmission kasivaihteet kasivaihteinen kesarenkailla kesarenkaat rahoitus vahvistuu ostaja buyer betalning
utlamning opening hours only rekkareita kilvet kilpia siirtokilvet koekilvet rekisterikilvet irrotettu poistettu
rekisterista unregistered plates removed skyltar provskyltar saknar summer tyres winters talvirenkaita talvet
sommardack fortfarande viela alla registered nobody morgon overmorgon fore from fran klockan picks customer
receiv vardagar luovutuksia enaa noudettavissa paikalla vastaanottajalle mean sorry menar rattelse tarkoitan
eiku rekisteriote registreringsbevis takakontissa tavaratilassa kesarengasta vanteineen trunk bakluckan
servicebok huoltokirja kesarenkaat sommardacken plats """.split())
_KEEP = set("""
advance after aikaisintaan ajaa ajettu akku alkaen alla along am an and anna any apukaynnistyksen arkipaivisin
arkisin arrival arrive asap asiakas asiakkaalle asiakkaan asti at auki aukioloajat auto automaatti automatic auton
autossa avaimet avain avainboksissa away back bagaget bakluckan bay be before bilen book booking boot box br bring
bumper by call car cheers close closes code collect comes correction customer dacken dacksats desk destination
docs driver earliest edessa efter ei eikun en ennen ensin etukateen extra foljer for fore forhand forsaljningen
forst framme fredag fri friday from fuel glovebox go haettava haettavissa hakea hakemaan halsningar hamtas hamtning
hanskalokerossa har has hei heikko hej hejsan hello here heti hi hour huollon huollosta huolto huoltokirja huom
huomenna hyvaa if ilman ilmoita imorgon in innan irrotettu is issues it ja jalkeen jos kahdeltatoista kan
kauppakirja kello kesarengasta kesarenkaat kesarenkailla kesken keskiviikkona key keys kiinni kiire kiitos kiitti
kilpia kilvet kl klar klo klockan kod koekilvet kohde kohteen kolhu kolmelta kontissa koodi korjaus kotona kuin
kuljettajalla kuljettajalle kulkee kun kunden kuski kuvaa kyydissa lahdet lahella latest licence ligger liike
liikkeelle liikkeen liikkeeseen lisaksi lisarenkaat low luovutus ma maanantaina maksaa maksettu maksun mandag
manuaali manuaalia manuaalikortin manuaalivaihteet manuaalivaihteinen manual mean med meidan meille menee mennessa
min moi moikka mon monday mottagaren mottagning mukaan mukana must mvh myyja myyjalta myynti needs no normaali
not noudettavaksi noudettavissa noutaa noutaessa nouto noutoa now nyckelbox nyckeln nycklar obs och odottaa of oik
oikeus old olet oloa oltava on ongelmia only onnistuu open oppet order ordernr osia ostaja ota our ovessa pa
paikalla paivanjatkoa paljon paperit papers payment pe perilla perille perjantaina pesematta pesu picks pickup
pieni pihalla pihasta pitaa plates pm poistettu puoli puuttuu ready rear receiving reception ref referens reg
rekisteriote rekisterista rekkareita released removed rengassarja rengasta renkaat renkaita right ring ruudussa
saa saakka saapumista sales sarja sarjaa scratch second senast service set shop siirto siirtokilvet silloin sita
ska soita sommardacken sopii sorry spare special stanger star sulkee summer taas tack takaisin takakontissa
takapenkilla take talvirenkaita tanaan tankissa tarvii tarvita tarvitaan tavallinen tavaratilassa terve terveisin
thank thanks the then there thu thursday thx tidigast tilausnro tilausnumero timme tiskilta tkm tmrw to toimistosta
toimitus toimitusosoite toinen tomorrow torstaina trade transfer trunk tulee tulet tuntia tuo tyres unregistered
until up us uusi vaan vahan vaihtari vaihtoauto valmiina valmis vanha vanteilla vanteineen vara varaus vardagar
vasta vastaanottajalle vastaanottava vastaanotto vi vie viela viim viimeistaan viite viitenro voi waiting we
wednesday weekdays wheels winters with won't workshop yard yhdelta ylihuomenna ylimaarainen you
bil batteri battery dar gamla garden hemma ingen inte kundens may need once paid saljaren sen standard start svagt
ta tio tolv fem ett tre fyra nio elva tva transport vain vanlig weak kassan liten oss plats sitten still than
that till tillbaka today var vid more tree form fort ford none tone note gone done ones mens mena men tanne kana
kohta tassa olla ollaan meilla taalla avoinna dent door small photos nothing first direkt noon later rest test best
west east also into onto what when then them they this those these where were with without your yours some same
sure sale sales seat date dates data """.split())
_KEEP |= set(WEEKDAY_SHORT) | set(DAY_WORDS) | CLOCK_WORDS
_FUZZY_CACHE: dict[str, str] = {}


def _dedouble(word: str) -> str:
    return re.sub(r"(.)\1+", r"\1", word)


# Index of the vocabulary with doubled letters collapsed, and of every one-letter drop of that.
_DD: dict[str, set[str]] = {}
_DROP: dict[str, set[str]] = {}
for _w in _VOCAB | _KEEP:  # typos of ordinary words resolve to themselves ("anan" -> "anna")
    _d = _dedouble(_w)
    _DD.setdefault(_d, set()).add(_w)
    if len(_d) >= 4:
        for _i in range(len(_d)):
            _DROP.setdefault(_dedouble(_d[:_i] + _d[_i + 1:]), set()).add(_w)
_BY_FIRST: dict[str, list[str]] = {}
for _w in _VOCAB | _KEEP:  # typos of ordinary words resolve to themselves ("anan" -> "anna")
    if len(_w) >= 5:
        _BY_FIRST.setdefault(_w[0], []).append(_w)
_SUB: dict[str, set[str]] = {}  # substitution index for long words: word with one char wildcarded
for _w in _VOCAB | _KEEP:  # typos of ordinary words resolve to themselves ("anan" -> "anna")
    if len(_w) >= 6:
        for _i in range(len(_w)):
            _SUB.setdefault(_w[:_i] + "?" + _w[_i + 1:], set()).add(_w)


def _osa(a: str, b: str) -> int:
    """Optimal-string-alignment (restricted Damerau-Levenshtein) edit distance."""
    la, lb = len(a), len(b)
    prev2: list[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        prev2, prev = prev, cur
    return prev[lb]


def _correct(word: str) -> str:
    """Map a typo to its vocabulary word.  Dealers' typos are a doubled letter, a dropped letter
    or two swapped neighbours ("huoomenna", "befre", "klomelta", "rceeiving"); these are undone
    (also combined with doubling).  Long words also tolerate one substituted letter.  Short words
    and frequent ordinary words are never changed."""
    if word in _VOCAB or word in _KEEP or len(word) < 3 or not word.isalpha():
        return word
    hit = _FUZZY_CACHE.get(word)
    if hit is not None:
        return hit
    if len(_FUZZY_CACHE) > 100_000:
        _FUZZY_CACHE.clear()
    d = _dedouble(word)
    cands: set[str] = set(_DD.get(d, ()))
    if not cands:
        for v in (word, d):  # swapped neighbours
            for i in range(len(v) - 1):
                cands |= _DD.get(_dedouble(v[:i] + v[i + 1] + v[i] + v[i + 2:]), set())
    if not cands and len(d) >= 3:  # dropped letter
        cands |= _DROP.get(d, set())
    if not cands and len(word) >= 6:  # one substituted letter
        for i in range(len(word)):
            cands |= _SUB.get(word[:i] + "?" + word[i + 1:], set())
    if not cands and len(word) >= 6:  # two typos in one long word ("Destiantin", "Maualai")
        scored = sorted((_osa(word, c), c) for c in _BY_FIRST.get(word[0], ()) if abs(len(c) - len(word)) <= 2)
        if scored and scored[0][0] <= 2 and (len(scored) == 1 or scored[1][0] > scored[0][0]):
            cands = {scored[0][1]}
    best = word
    if len(word) <= 3 and len(cands) > 1:  # "hom": huom or home?  too short to guess
        cands = set()
    if cands:
        best = min(cands, key=lambda c: (abs(len(c) - len(word)), c))
    _FUZZY_CACHE[word] = best
    return best


# ----------------------------------------------------------------------------------------
# Tokenizer
# ----------------------------------------------------------------------------------------
_TOKEN = re.compile(r"\d{1,2}[:.]\d{2}|\d+|[a-z']+|[.,!?;/()\-:]")
_ABBREV = {"viim", "n", "t", "klo", "kl", "no", "nro", "ref", "obs", "huom", "esim", "ma", "ti", "ke", "to",
           "pe", "la", "su", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun", "man",
           "tis", "ons", "tors", "fre", "lor", "son", "mvh", "br", "a", "p"}


def _fold(text: str) -> str:
    text = text.lower().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[‐-―−]", "-", text)
    text = text.replace("’", "'").replace("`", "'")
    text = text.replace("o'clock", "oclock").replace("a.m.", "am").replace("p.m.", "pm")
    # "8:sta 21:een", "9:stä 17:ään" -> "8-21"
    text = re.sub(r"(\d{1,2}(?:[:.]\d{2})?)\s*:?\s*(?:sta|tsa)\b\s*(\d{1,2}(?:[:.]\d{2})?)\s*:?\s*(?:een|aan|iin|n)\b",
                  r"\1-\2", text)
    # "18:aan saakka", "14:ään", "16:een" -> bare hour
    text = re.sub(r"(\d)\s*:\s*(sta|een|aan|iin|n|lta|ta)\b", r"\1", text)
    # "9am", "4pm", "10:30am"
    text = re.sub(r"(\d)(am|pm)\b", r"\1 \2", text)
    text = text.replace("i morgon", "imorgon").replace("i overmorgon", "iovermorgon")
    text = text.replace("day after tomorrow", "iovermorgon").replace("the iovermorgon", "iovermorgon")
    text = re.sub(r"\bi dag\b", "idag", text)
    return text


class Tok:
    __slots__ = ("kind", "text", "val")

    def __init__(self, kind: str, text: str, val: Any = None):
        self.kind, self.text, self.val = kind, text, val

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.kind}:{self.text}"


def _tokenize(text: str) -> list[Tok]:
    out: list[Tok] = []
    for m in _TOKEN.finditer(text):
        s = m.group(0)
        if s[0].isdigit():
            if len(s) > 2 and (":" in s or "." in s):
                h, mi = int(s[:-3]), int(s[-2:])
                out.append(Tok("hm", s, (h, mi)))
            else:
                out.append(Tok("num", s, int(s)))
        elif s[0].isalpha() or s[0] == "'":
            w = s.strip("'")
            if w.endswith("'s"):
                w = w[:-2]
            out.append(Tok("w", _correct(w) if w else w))
        else:
            out.append(Tok("p", s))
    return out


def _sentences(toks: list[Tok]) -> list[list[Tok]]:
    """Split at . ! ? ; / unless the period closes an abbreviation (viim., to., ma., n.)."""
    sents: list[list[Tok]] = []
    cur: list[Tok] = []
    for i, t in enumerate(toks):
        if t.kind == "p" and t.text in ".!?;/":
            if t.text == "." and cur and cur[-1].kind == "w" and cur[-1].text in _ABBREV:
                nxt = toks[i + 1] if i + 1 < len(toks) else None
                # "to. klo 10", "viim. huomenna", "ma. 18.00" keep going; "obs manuell." ends
                if nxt is not None and (nxt.kind in ("hm", "num") or (nxt.kind == "w" and (
                        nxt.text in CLOCK_WORDS or nxt.text in DAY_WORDS or nxt.text in WEEKDAY_LONG
                        or nxt.text in WORD_HOUR or nxt.text in HALF or cur[-1].text in ("viim", "n", "t")))):
                    continue
            if cur:
                sents.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        sents.append(cur)
    return sents


def _clauses(sent: list[Tok]) -> list[list[Tok]]:
    out: list[list[Tok]] = [[]]
    for t in sent:
        if t.kind == "p" and t.text == ",":
            out.append([])
        else:
            out[-1].append(t)
    return [c for c in out if c]


# ----------------------------------------------------------------------------------------
# Time / day extraction inside a clause
# ----------------------------------------------------------------------------------------

def _word_hour(tok: Tok, prev: Optional[Tok], nxt: Optional[Tok]) -> Optional[int]:
    if tok.kind != "w":
        return None
    w = tok.text
    if w in NOON:
        return 12 * HOUR
    if w not in WORD_HOUR:
        return None
    h, lang = WORD_HOUR[w]
    pw = prev.text if prev is not None and prev.kind == "w" else ""
    nw = nxt.text if nxt is not None and nxt.kind == "w" else ""
    if lang == "fi":
        strong = w.endswith(_FI_STRONG_SUFFIX)
        if strong or pw in CLOCK_WORDS or pw in HALF or pw in TIME_PREP or nw in TIME_POST or pw in DAY_WORDS \
                or pw in WEEKDAY_LONG:
            return h * HOUR
        return None
    if pw in CLOCK_WORDS or pw in HALF or pw in TIME_PREP or pw in DAY_WORDS or pw in WEEKDAY_LONG \
            or nw in ("oclock", "pm", "am"):
        return h * HOUR
    return None


def _ampm(h: int, nxt: Optional[Tok]) -> int:
    if nxt is not None and nxt.kind == "w":
        if nxt.text == "pm" and h < 12:
            return h + 12
        if nxt.text == "am" and h == 12:
            return 0
    return h


class TimeRef:
    __slots__ = ("minute", "denied", "corr", "idx")

    def __init__(self, minute: int, denied: bool, corr: bool, idx: int):
        self.minute, self.denied, self.corr, self.idx = minute, denied, corr, idx


def _scan(cl: list[Tok], arrival_wd: int) -> tuple[list[TimeRef], list[tuple[int, int, int]], list[tuple[int, bool, int]], bool]:
    """Returns (times, ranges, days, has_correction).  days: (offset, corrected, idx)."""
    times: list[TimeRef] = []
    ranges: list[tuple[int, int, int]] = []
    days: list[tuple[int, bool, int]] = []
    corr = False
    n = len(cl)
    i = 0
    words = [t.text if t.kind == "w" else "" for t in cl]

    def is_corr_at(j: int) -> int:
        w = words[j]
        if w in ("eikun", "eiku", "korjaus", "correction", "rattelse", "tarkoitan", "sorry", "anteeksi", "forlat",
                 "ursakta", "oops", "actually"):
            return 1
        if w == "ei" and j + 1 < n and words[j + 1] in ("kun", "kuin"):
            return 2
        if w in ("mean", "men", "meant", "man") and j > 0 and words[j - 1] == "i":
            return 1
        if w == "menar" and j > 0 and words[j - 1] == "jag":
            return 1
        return 0

    def single_at(j: int) -> Optional[tuple[int, int]]:
        """Parse a time starting at token j -> (minute, next index)."""
        t = cl[j]
        prev = cl[j - 1] if j > 0 else None
        nxt = cl[j + 1] if j + 1 < n else None
        pw = prev.text if prev is not None and prev.kind == "w" else ""
        if t.kind == "hm":
            h, m = t.val
            if h > 24 or m > 59:
                return None
            h = _ampm(h, nxt)
            k = j + 1 + (1 if nxt is not None and nxt.kind == "w" and nxt.text in ("am", "pm") else 0)
            return h * HOUR + m, k
        if t.kind == "num":
            v = t.val
            nw = nxt.text if nxt is not None and nxt.kind == "w" else ""
            if len(t.text) in (3, 4) and pw in CLOCK_WORDS:
                h, m = divmod(v, 100)
                if h <= 24 and m <= 59:
                    return h * HOUR + m, j + 1
                return None
            if len(t.text) > 2 or v > 24:
                return None
            if nw in ("min", "mins", "minutes", "minuuttia", "minuutin", "minuter", "h", "tuntia", "km",
                      "rengasta", "sarjaa", "kpl", "st"):
                return None
            ok = pw in CLOCK_WORDS or pw in TIME_PREP or pw in DAY_WORDS or pw in WEEKDAY_LONG \
                or pw in WEEKDAY_SHORT or nw in TIME_POST or pw in ("noin", "ca", "viim", "viimeistaan",
                                                                     "mennessa", "kello", "vasta")
            if not ok:
                return None
            h = _ampm(v, nxt)
            k = j + 1 + (1 if nw in ("am", "pm") else 0)
            return h * HOUR, k
        if t.kind == "w" and t.text in ("half", "quarter") and nxt is not None and nxt.kind == "w" \
                and nxt.text in ("past", "to") and j + 2 < n:
            # English "half past two" (14:30), "quarter past ten", "quarter to three"
            t2 = cl[j + 2]
            base = None
            if t2.kind == "num" and t2.val <= 24:
                base = _ampm(t2.val, cl[j + 3] if j + 3 < n else None) * HOUR
            else:
                base = _word_hour(t2, Tok("w", "klo"), cl[j + 3] if j + 3 < n else None)
            if base is not None:
                off = 30 if t.text == "half" else (15 if nxt.text == "past" else -15)
                return base + off, j + 3
            return None
        if t.kind == "w" and t.text in HALF and nxt is not None:
            m = _word_hour(nxt, t, cl[j + 2] if j + 2 < n else None)
            if m is not None:
                return m - 30, j + 2
            return None
        m = _word_hour(t, prev, nxt)
        if m is not None:
            return m, j + 1
        return None

    pending_corr = False
    while i < n:
        t = cl[i]
        c = is_corr_at(i)
        if c:
            corr = pending_corr = True
            i += c
            continue
        w = words[i]
        # days
        if w in DAY_WORDS:
            days.append((DAY_WORDS[w], pending_corr, i))
            i += 1
            continue
        if w in WEEKDAY_LONG or w in WEEKDAY_SHORT:
            wd = WEEKDAY_LONG.get(w, WEEKDAY_SHORT.get(w))
            short = w not in WEEKDAY_LONG
            nxt = cl[i + 1] if i + 1 < n else None
            prv = cl[i - 1] if i > 0 else None
            span = (nxt is not None and nxt.kind == "p" and nxt.text in "-") or (
                prv is not None and prv.kind == "p" and prv.text == "-")
            if not span:
                ok = True
                if short:
                    j = i + 1
                    if j < n and cl[j].kind == "p" and cl[j].text == ".":
                        j += 1
                    ok = j < n and (cl[j].kind in ("hm", "num") or words[j] in CLOCK_WORDS
                                    or words[j] in WORD_HOUR or words[j] in HALF or words[j] in NOON) \
                        or (pending_corr and (j >= n or cl[j].kind == "p"))
                    # English "to"/"la" etc. inside an English sentence: require a clock right after
                    if w in ("to",) and j < n and cl[j].kind == "num" and not (
                            j + 1 < n and words[j + 1] in TIME_POST):
                        ok = j + 1 >= n or cl[j + 1].kind == "p" and cl[j + 1].text != "-"
                if ok:
                    k = (wd - arrival_wd) % 7
                    if k < 2:
                        k += 7
                    days.append((k, pending_corr, i))
                    i += 1
                    continue
        # times / ranges
        s = single_at(i)
        if s is None and t.kind == "num" and i + 1 < n and cl[i + 1].kind == "p" and cl[i + 1].text == "-":
            # bare-number range start "9-16", "9 - 18"
            s = (_ampm(t.val, None) * HOUR, i + 1) if t.val <= 24 and len(t.text) <= 2 else None
        if s is not None:
            m1, k = s
            # range?
            j = k
            between = any(x in ("between", "valilla", "valissa", "mellan") for x in words)
            if j < n and cl[j].kind == "p" and cl[j].text == "-" or (
                    j < n and (words[j] in ("to", "till", "until") or (between and words[j] in ("and", "ja", "och")))
                    and j + 1 < n and (cl[j + 1].kind in ("hm", "num") or words[j + 1] in CLOCK_WORDS)):
                j += 1
                if j < n and words[j] in CLOCK_WORDS:
                    j += 1
                s2 = None
                if j < n:
                    if cl[j].kind == "num" and cl[j].val <= 24 and len(cl[j].text) <= 2:
                        nx = cl[j + 1] if j + 1 < n else None
                        s2 = (_ampm(cl[j].val, nx) * HOUR, j + 1 + (1 if nx is not None and nx.kind == "w" and nx.text in ("am", "pm") else 0))
                    else:
                        s2 = single_at(j)
                if s2 is not None:
                    m2, k2 = s2
                    # "7am - 2pm": first part without am/pm but second pm -> first stays am
                    if m1 < m2:
                        ranges.append((m1, m2, i))
                    i = k2
                    continue
            # negation directly before the time: "ei klo 13", "not 17:00", "(inte kl 21)"
            j = i - 1
            while j >= 0 and (words[j] in CLOCK_WORDS or words[j] in ("at", "by") or (cl[j].kind == "p" and cl[j].text == "(")):
                j -= 1
            denied = j >= 0 and words[j] in NEGATORS and not (j + 1 < n and words[j + 1] in ("kun", "kuin"))
            times.append(TimeRef(m1, denied, pending_corr, i))
            i = k
            continue
        i += 1
    return times, ranges, days, corr


# ----------------------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------------------

def _has(words: list[str], stems: tuple[str, ...]) -> bool:
    for w in words:
        if w and w.startswith(stems):
            return True
    return False


def _phrase(text: str, phrases: tuple[str, ...]) -> bool:
    return any(p in text for p in phrases)


def _classify_single(words: list[str]) -> Optional[str]:
    text = " " + " ".join(w for w in words if w) + " "
    if _has(words, PRIVATE_STEMS) and not _has(words, ("perilla", "destination", "framme")):
        return None
    deliv = _has(words, DELIVERY_STEMS)
    custpick = _phrase(text, CUSTOMER_PICK)
    release = _has(words, RELEASE_STEMS) and not deliv
    close = _has(words, CLOSE_STEMS) or _phrase(text, CLOSE_PHRASES)
    weak_close = _has(words, WEAK_CLOSE_STEMS)
    if deliv and not custpick:
        # "Soita asiakkaalle tuntia ennen perillä oloa" has no clock, so we never get here for it.
        pickup_ctx = _has(words, ("valmis", "valmistu", "noudettavissa", "noutaa", "nouto", "hakea", "haettav",
                                  "ready", "pickup", "collect", "hamta", "klar", "aikaisintaan", "earliest",
                                  "tidigast", "alkaen"))
        if pickup_ctx and not _has(words, ("perilla", "perille", "framme", "destination", "mennessa",
                                           "kohde", "kohtee", "vastaanot", "receiv", "mottag", "deliver")):
            deliv = False
    if deliv or custpick:
        if (close or weak_close) and not custpick:
            return "delivery_closes"
        if _has(words, OPENS_STEMS) or (_has(words, ("auki", "open", "oppet")) and _has(words, ("alkaen", "from", "fran"))):
            return "delivery_opens"
        return "deadline"
    if _phrase(text, CLOSE_PHRASES):  # "ingen utlämning efter 15", "ei enää luovutuksia"
        return "pickup_closes"
    if release:
        return "ready"
    if close and not _phrase(text, (" ei ennen", "not before", "inte fore")):
        return "pickup_closes"
    if _has(words, READY_STEMS) or _phrase(text, READY_PHRASES):
        return "ready"
    if weak_close or _has(words, BEFORE_STEMS):
        return "pickup_closes"
    return None


def _classify_range(words: list[str]) -> Optional[str]:
    if _has(words, PRIVATE_STEMS):
        return None
    if _has(words, ("kohde", "kohtee", "vastaanot", "destination", "receiv", "recipient", "mottag", "toimitusos",
                    "perilla", "perille", "leverans", "deliver")):
        return "delivery"
    return "pickup"


def _released(words: list[str]) -> bool:
    return _has(words, RELEASE_STEMS)


# ----------------------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------------------

def _empty() -> dict:
    return {"ready_from": None, "released": False, "pickup_opens": None, "pickup_closes": None,
            "delivery_opens": None, "delivery_closes": None, "deadline": None,
            "manual": False, "plates": False, "summer_tyres": False}


def parse_note(text: Optional[str], language: Optional[str] = None, arrived_at: int = 0) -> dict:
    """Parse a dealer note into constraints (see module docstring).  `language` is advisory only:
    notes mix languages ("Not ready before tomorrow 12:00 / Auto valmis vasta huomenna klo 12")."""
    out = _empty()
    if not text:
        return out
    folded = _fold(text)
    arr_day = arrived_at // DAY
    arr_wd = arr_day % 7

    # ---- flags (whole-note regexes on the folded text; decoys like "kesärenkaat takakontissa",
    #      "rekisteriote autossa", "summer tyres in the trunk" do not match)
    toks = _tokenize(folded)
    corrected = " ".join(t.text for t in toks)
    flag_text = folded + " || " + corrected
    if FLAG_MANUAL.search(flag_text) and not re.search(r"\b(ei|not|no|inte|isn't|eika) (a |an |en )?(manuaali|manual|manuell)", flag_text) \
            and not re.search(r"automaatti, ei manuaali|automatic, not manual", flag_text):
        out["manual"] = True
    if FLAG_PLATES.search(flag_text):  # "rekisteriote"/"registreringsbevis" (papers) do not match
        out["plates"] = True
    if FLAG_SUMMER_ON.search(flag_text):
        out["summer_tyres"] = True

    # ---- timed facts
    facts: list[dict] = []  # each: {field, minute|None, day|None, range}
    last: Optional[dict] = None
    for sent in _sentences(toks):
        pending_words: list[str] = []
        pending_days: list[tuple[int, bool, int]] = []
        carry_corr = False
        for cl in _clauses(sent):
            words = [t.text if t.kind == "w" else "" for t in cl]
            times, ranges, days, corr = _scan(cl, arr_wd)
            if carry_corr:  # "..., sorry, ylihuomenna": the marker stood in a clause of its own
                corr = True
                for t in times:
                    t.corr = True
                days = [(d[0], True, d[2]) for d in days]
            carry_corr = corr and not times and not days and not ranges
            live = [t for t in times if not t.denied]
            kw_words = pending_words + words
            # ranges -> opening hours
            if ranges:
                kind = _classify_range(kw_words)
                if corr and last is not None and "opens" in last and _classify_range(words) == "pickup":
                    last["opens"], last["closes"] = ranges[-1][0], ranges[-1][1]
                elif kind is not None:
                    o, c = ranges[-1][0], ranges[-1][1]
                    f = {"field": kind + "_hours", "opens": o, "closes": c, "words": kw_words}
                    facts.append(f)
                    last = f
                pending_words, pending_days = [], []
                continue
            if not live and not days:
                if corr or not times:
                    pending_words += words
                continue
            if not live and days:
                # day-only clause: a correction of the day ("sorry, I mean Monday", "eikun ylihuomenna")
                if corr and last is not None and last.get("minute") is not None and "day" in last:
                    last["day"] = days[-1][0]
                    continue
                pending_words += words
                pending_days += days
                continue
            # clause with a (non-denied) time
            corr_times = [t for t in live if t.corr]
            own_kind = _classify_single(words)
            if corr_times and last is not None and "minute" in last and own_kind in (None, last["field"]):
                # correction of the previous fact: "ei kun klo 16.30", "Correction: 10:00",
                # "sorry, I mean 16:00", "haettava ennen sitä, ei kun klo 15.30"
                last["minute"] = corr_times[-1].minute
                cd = [d for d in days if d[1]] or days
                if cd:
                    last["day"] = cd[-1][0]
                pending_words, pending_days = [], []
                continue
            kind = _classify_single(kw_words)
            if kind is None and not any(w for w in words if w not in CLOCK_WORDS) and last is not None \
                    and "minute" in last:
                # a bare time right after a fact ("Korjaus: 12.00" handled above)
                last["minute"] = live[0].minute
                if days:
                    last["day"] = days[-1][0]
                continue
            if kind is None:
                pending_words += words
                pending_days += days
                continue
            # choose the time: a corrected one wins, else the first non-denied one
            tm = corr_times[-1] if corr_times else live[0]
            all_days = pending_days + days
            corr_days = [d for d in all_days if d[1]]
            day = corr_days[-1][0] if corr_days else (all_days[0][0] if all_days else None)
            if kind == "ready" and _released(kw_words):
                out["released"] = True
            f = {"field": kind, "minute": tm.minute, "day": day, "words": kw_words}
            facts.append(f)
            last = f
            pending_words, pending_days = [], []
        if pending_days:
            # a day without a clock time: "valmis keskiviikkona" -> from the start of that day;
            # "perillä torstaina" -> by the end of that business day (20:00)
            kind = _classify_single(pending_words)
            if kind in ("ready", "deadline"):
                cd = [d for d in pending_days if d[1]] or pending_days
                f = {"field": kind, "minute": 0 if kind == "ready" else 20 * HOUR, "day": cd[-1][0],
                     "words": pending_words}
                if kind == "ready" and _released(pending_words):
                    out["released"] = True
                facts.append(f)
                last = f

    # ---- combine facts
    ready = deadline = None
    for f in facts:
        k = f["field"]
        if k.endswith("_hours"):
            side = k[:-6]
            if f["opens"] > 0:
                cur = out[side + "_opens"]
                out[side + "_opens"] = f["opens"] if cur is None else max(cur, f["opens"])
            cur = out[side + "_closes"]
            out[side + "_closes"] = f["closes"] if cur is None else min(cur, f["closes"])
            continue
        minute = f["minute"]
        if k in ("pickup_closes", "delivery_closes"):
            cur = out[k]
            out[k] = minute if cur is None else min(cur, minute)
        elif k == "delivery_opens":
            cur = out[k]
            out[k] = minute if cur is None else max(cur, minute)
        elif k == "ready":
            t = (arr_day + (f["day"] or 0)) * DAY + minute
            ready = t if ready is None else max(ready, t)
        elif k == "deadline":
            t = (arr_day + (f["day"] or 0)) * DAY + minute
            deadline = t if deadline is None else min(deadline, t)
    out["ready_from"] = ready
    out["deadline"] = deadline
    return out


# ----------------------------------------------------------------------------------------
# How the dispatcher uses it
# ----------------------------------------------------------------------------------------
#
#   class Dispatcher(Desk):
#       def post(self, order, legs, retry=starter.Retry()):
#           if not legs:
#               return
#           req = self.requests[order["id"]]
#           stops = starter.without_windows(notes.stops_for(order, legs, req, retry, self.now), self.now)
#           audience = notes.audience_for(order, legs, req, retry)
#           self.outbox.append({"type": "post", "stops": stops, "audience": audience,
#                               "schedule": self.fees_for(order, len(legs))})
#           if len(legs) > 1:                      # same trade-in split as Desk.post
#               self.send()
#               if not self.gigs_of(order["id"]):
#                   for leg in legs:
#                       self.post(order, [leg], retry)
#
# and, for timing/pricing, ``notes.constraints(request)`` gives the parsed note (cached).

_PARSE_CACHE: dict[str, dict] = {}


def constraints(request: dict) -> dict:
    """parse_note for a PriceRequest, cached by request id (the note never changes)."""
    rid = request.get("id")
    hit = _PARSE_CACHE.get(rid) if rid is not None else None
    if hit is None:
        note = request.get("note")
        hit = parse_note(note["text"], note.get("language"), request["arrivedAt"]) if note else _empty()
        if rid is not None:
            _PARSE_CACHE[rid] = hit
    return hit


def _day_open(day: int, h: dict) -> Optional[tuple[int, int]]:
    wd = day % 7
    if wd in h["weekdays"]:
        return day * DAY + h["opens"], day * DAY + h["closes"]
    if wd == 5 and h.get("saturday"):
        return day * DAY + h["saturday"]["opens"], day * DAY + h["saturday"]["closes"]
    return None


def effective_hours(stated: Optional[dict], opens: Optional[int], closes: Optional[int]) -> Optional[dict]:
    """The stated hours narrowed by the note on weekdays (a note never changes Saturday hours,
    and never widens hours).  None for a private address."""
    if stated is None:
        return None
    h = dict(stated)
    if opens is not None and opens > h["opens"]:
        h["opens"] = opens
    if closes is not None and closes < h["closes"]:
        h["closes"] = closes
    if h["opens"] >= h["closes"]:  # nonsense; keep what the dealer stated
        return dict(stated)
    return h


def first_open_interval(start: int, h: dict, days: int = 8) -> Optional[tuple[int, int]]:
    """The first [from, to] at or after `start` in which a site with hours `h` is open."""
    for d in range(start // DAY, start // DAY + days):
        iv = _day_open(d, h)
        if iv is not None and start < iv[1]:
            return max(start, iv[0]), iv[1]
    return None


def last_open_minute(end: int, h: dict, days: int = 8) -> Optional[int]:
    """The latest minute <= `end` at which a site with hours `h` is open."""
    for d in range(end // DAY, end // DAY - days, -1):
        iv = _day_open(d, h)
        if iv is not None and iv[0] <= end:
            return min(end, iv[1])
    return None


def apply_to_stops(order: dict, leg: int, parsed: dict, pickup_window: dict, delivery_window: dict,
                   now: int = 0) -> tuple[dict, dict, dict]:
    """Narrow one leg's stop windows by a parsed note; return (pickup_window, delivery_window, flags).

    ``pickup_window`` / ``delivery_window`` are what the dispatcher would use without the note
    (the order's pickup window, possibly moved by a retry; delivery {window.from, order deadline}).
    Only leg 0 is affected: the note is about the new car, leg 1 is the trade-in coming back.

      * pickup.from >= ready_from (ready time or payment release).  Never before the order's window.
        At a dealer site the window may then end after the order's window (rules allow it); at a
        private address it stays inside the seller's slot.
      * Pickup hours narrowed by the note (e.g. "Liike kiinni klo 15.30"): drivers plan by the
        STATED hours, so a later arrival would fail as `closed`.  We cut the window to the first
        real open interval at or after max(from, now).  When that interval ends the gig expires
        (`gig_expired`) and the next post picks the next day.
      * Delivery hours narrowed ("Vastaanotto klo 16 asti"): delivery.to is cut back to the
        last real-open minute (a driver arriving after the real closing waits and the car counts
        only next morning -> late).
      * delivery.to = min(delivery.to, note deadline).
      * flags: withPlates (pickup StopRef), manualLicence (gig audience), summerTyres (avoid
        collecting while snow falls at pickup/delivery: no API flag for it).
    """
    pw, dw = dict(pickup_window), dict(delivery_window)
    flags = {"withPlates": False, "manualLicence": False, "summerTyres": False}
    if leg != 0 or not parsed:
        return pw, dw, flags
    L = order["legs"][leg]
    pick, deliv = L["pickup"], L["delivery"]
    flags["withPlates"] = bool(parsed.get("plates"))
    flags["manualLicence"] = bool(parsed.get("manual"))
    flags["summerTyres"] = bool(parsed.get("summer_tyres"))

    rf = parsed.get("ready_from")
    if rf is not None and rf > pw["from"]:
        if pick.get("privateAddress"):
            pw["from"] = min(rf, pw["to"])
        else:
            pw["from"] = rf
            pw["to"] = max(pw["to"], rf)

    ph = effective_hours(pick.get("hours"), parsed.get("pickup_opens"), parsed.get("pickup_closes"))
    if ph is not None and not pick.get("privateAddress") and ph != pick.get("hours"):
        iv = first_open_interval(max(pw["from"], now), ph)
        if iv is not None:
            pw = {"from": max(pw["from"], iv[0]), "to": iv[1]}

    dl = parsed.get("deadline")
    if dl is not None and dl < dw["to"]:
        dw["to"] = dl
    dh = effective_hours(deliv.get("hours"), parsed.get("delivery_opens"), parsed.get("delivery_closes"))
    if dh is not None and not deliv.get("privateAddress") and dh != deliv.get("hours"):
        last = last_open_minute(dw["to"], dh)
        if last is not None:
            dw["to"] = last
    dw["to"] = max(dw["to"], dw["from"])
    return pw, dw, flags


def stops_for(order: dict, legs: list[int], request: dict, retry: Any = None, now: int = 0) -> list[dict]:
    """Drop-in for ``spread.starter.stops_for`` that reads the note with ``parse_note``.
    Same output shape: a pickup and a delivery StopRef per leg (charging for EVs under 30%),
    a window only where it is narrower than the order's own."""
    parsed = constraints(request)
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
        pw, dw, flags = apply_to_stops(order, leg, parsed, pw, dict(base_d), now)
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


def audience_for(order: dict, legs: list[int], request: dict, retry: Any = None) -> dict:
    """Gig audience: experienced for a premium car, manual licence when the note says manual
    (only leg 0 carries the note) or a retry after a `licence` failure."""
    parsed = constraints(request)
    return {"experienced": any(order["legs"][i]["car"]["premium"] for i in legs),
            "manualLicence": bool(getattr(retry, "manual_licence", False)) or (0 in legs and bool(parsed.get("manual")))}


if __name__ == "__main__":  # quick manual check
    import json
    import sys
    for line in sys.argv[1:] or ["Auto valmis vasta huomenna klo 12.30, ei kun klo 13. Kohde sulkee klo 16."]:
        print(line, json.dumps(parse_note(line, "fi", 600)))
