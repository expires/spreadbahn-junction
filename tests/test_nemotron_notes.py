"""Offline checks of the Nemotron note reader (no network):  PYTHONPATH=src python tests/test_nemotron_notes.py

Conversion of relative days/times to world minutes, grounding, time verification, JSON
robustness, the ensemble, and the dispatcher hook's fallbacks (failure, timeout, no note)."""
import json
import time

from bahn.models import nemotron_notes as NN
from bahn.models import notes

DAY = 1440
TUE_1000 = 1 * DAY + 600  # arrival: Tuesday 10:00


def answer(*items):
    return json.dumps({"items": [{"quote": q, "kind": k, "day": d, "time": t, "payment_release": p}
                                 for q, k, d, t, p in items]})


def conv(note, *items, arrived=TUE_1000):
    obj = NN.parse_json(answer(*items))
    return NN.to_constraints(obj, note, arrived)


def test_days_and_times():
    p, _ = conv("valmis huomenna klo 13, perillä torstaina yhteen mennessä",
                ("valmis huomenna klo 13", "ready", "tomorrow", "13:00", False),
                ("perillä torstaina yhteen mennessä", "deadline", "thursday", "13:00", False))
    assert p["ready_from"] == 2 * DAY + 13 * 60  # Wednesday 13:00
    assert p["deadline"] == 3 * DAY + 13 * 60    # Thursday = 2 days after Tuesday
    # weekday semantics match notes.py: 2..8 days ahead (Wednesday seen from Tuesday is +8)
    p, _ = conv("valmis keskiviikkona", ("valmis keskiviikkona", "ready", "wednesday", "none", False))
    assert p["ready_from"] == (1 + 8) * DAY + 0
    p, _ = conv("perillä pe", ("perillä pe", "deadline", "friday", "none", False))
    assert p["deadline"] == 4 * DAY + 20 * 60   # day without a clock: end of business day
    # hours: later opening / earlier closing when two items disagree
    p, _ = conv("auki 8-16, ei kun 9-15", ("auki 8-16, ei kun 9-15", "pickup_opens", "none", "09:00", False),
                ("auki 8-16, ei kun 9-15", "pickup_closes", "none", "15:00", False))
    assert (p["pickup_opens"], p["pickup_closes"]) == (540, 900)


def test_grounding_and_verification():
    # a quote that is not in the note is dropped
    p, meta = conv("Avain tiskiltä.", ("Auto ilman kilpiä", "no_plates", "none", "none", False))
    assert not p["plates"] and meta["dropped"][0]["why"] == "quote not in note"
    # the model may silently fix a typo in its quote: still grounded
    p, _ = conv("Manuaalilvaihtienen", ("Manuaalilvaihtinen", "manual_gearbox", "none", "none", False))
    assert p["manual"]
    # the clock time must be the one the quote states
    p, meta = conv("Collect before half past two", ("Collect before half past two", "pickup_closes", "none", "13:30", False))
    assert p["pickup_closes"] == 14 * 60 + 30 and meta["time_fixed"]
    # ...but a range quote backing two items is left alone
    p, _ = conv("Receiving weekdays 7:00-tree pm only",
                ("Receiving weekdays 7:00-tree pm only", "delivery_opens", "none", "07:00", False),
                ("Receiving weekdays 7:00-tree pm only", "delivery_closes", "none", "15:00", False))
    assert p["delivery_closes"] == 900


def test_json_robustness():
    assert NN.parse_json("") is None
    assert NN.parse_json("not json") is None
    assert NN.parse_json('{"foo": 1}') is None
    assert NN.parse_json('```json\n{"items": []}\n```') == {"items": []}
    assert NN.parse_json('Sure! {"items": []} hope that helps') == {"items": []}
    p, meta = NN.to_constraints({"items": [{"kind": "warp_drive"}, 7, {"quote": "x", "kind": "ready", "day": "someday",
                                                                     "time": "25:99"}]}, "x", 0)
    assert p == notes._empty() and len(meta["dropped"]) == 3


def test_ensemble():
    r = notes._empty() | {"ready_from": 2000, "pickup_closes": 1000, "manual": True}
    l = notes._empty() | {"ready_from": 2100, "pickup_closes": 1050, "deadline": 3000, "plates": True}
    e = NN.ensemble(r, l)
    assert e["ready_from"] == 2100 and e["pickup_closes"] == 1000 and e["deadline"] == 3000
    assert e["manual"] and e["plates"]
    assert {d["field"] for d in e["disagreements"]} == {"ready_from", "pickup_closes", "deadline", "manual", "plates"}
    assert NN.ensemble(r, None)["ready_from"] == 2000


def test_hook_fallbacks():
    req = {"id": "R1", "arrivedAt": TUE_1000, "note": {"text": "Auto valmis huomenna klo 12. Ei kilpiä.", "language": "fi"}}
    rules = notes.constraints(req)
    real = NN.parse_note_llm_meta
    try:
        # 1. the endpoint fails -> rules
        NN.parse_note_llm_meta = lambda *a, **k: (None, {"error": "HTTP 503"})
        nn = NN.NemotronNotes(wait=0.5)
        assert nn.constraints(req) == rules and nn.stats["fallback_error"] == 1
        # 2. the endpoint is slow -> rules within the wait, LLM answer used once it arrives
        slow = notes._empty() | {"ready_from": rules["ready_from"] + 60, "plates": True}
        NN.parse_note_llm_meta = lambda *a, **k: (time.sleep(0.3), (dict(slow), {}))[1]
        nn = NN.NemotronNotes(wait=0.05)
        t = time.perf_counter()
        assert nn.constraints(req) == rules and time.perf_counter() - t < 0.2
        assert nn.stats["fallback_timeout"] == 1
        time.sleep(0.4)
        got = nn.constraints(req)
        assert got["ready_from"] == rules["ready_from"] + 60 and got["disagreements"]
        # 3. no note -> rules, no call
        assert nn.constraints({"id": "R2", "arrivedAt": 0, "note": None}) == notes._empty()
        # 4. stops/audience mirror notes.py
        order = {"id": "R1", "deadline": 5 * DAY, "legs": [{"car": {"premium": False, "electric": False, "chargePercent": None},
                 "pickup": {"privateAddress": False, "window": {"from": TUE_1000, "to": 5 * DAY}, "hours": None},
                 "delivery": {"privateAddress": False, "window": {"from": TUE_1000, "to": 5 * DAY}, "hours": None}}]}
        st = nn.stops_for(order, [0], req, None, TUE_1000)
        assert st[0]["withPlates"] and st[0]["window"]["from"] == rules["ready_from"] + 60
    finally:
        NN.parse_note_llm_meta = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
