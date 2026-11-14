"""Scan many seeds cheaply for nasty weeks: a world advanced to its end with no actions costs ~2 requests
and still reveals its requests (bulk orders = batches of 30+ cars) and its snow forecasts.

    python stress_scan.py --prefix st --n 40      # writes runs/stress_scan.json, prints the worst seeds
"""
import argparse
import collections
import json
import os

from spread.client import Client, SpreadError

from bahn.paths import ROOT
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Mon2"]


def scan(client: Client, seed: str, level: int = 8) -> dict:
    w = client.create(level=level, seed=seed)
    try:
        w.advance(to=w.snapshot()["endsAt"])
        rv, ev, roster = w.reveal(), w.events(), w.roster()
    finally:
        try:
            w.delete()
        except SpreadError:
            pass
    batches = collections.defaultdict(list)
    for r in rv["requests"]:
        q = r["request"]
        if q["batchId"]:
            batches[q["batchId"]].append(q)
    bulk = max(batches.values(), key=len) if batches else []
    snow = [(e["from"] // 1440, sorted(e["placeIds"])) for e in ev if e["kind"] == "forecast"]
    bulk_day = bulk[0]["arrivedAt"] // 1440 if bulk else None
    bulk_yard = bulk[0]["legs"][0]["pickup"]["placeId"] if bulk else None
    homes = collections.Counter(d["homePlaceId"] for d in roster)
    snow_days = {d for d, _ in snow}
    # Stress score: big late-week bulk orders, snow on or after the bulk day, thin driver pool, few drivers at the yard.
    score = len(bulk) * (1.5 if bulk_day in (3, 4) else 1.0) + 15 * len(snow) + 10 * (bulk_day in snow_days) \
        + max(0, 800 - len(roster)) / 10 + max(0, 20 - homes.get(bulk_yard, 0))
    return {"seed": seed, "requests": len(rv["requests"]), "drivers": len(roster), "bulk_size": len(bulk),
            "bulk_day": DAYS[bulk_day] if bulk_day is not None else None, "bulk_yard": bulk_yard,
            "yard_drivers": homes.get(bulk_yard, 0), "snow": [(DAYS[d], p) for d, p in snow], "stress": round(score, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="st")
    ap.add_argument("--n", type=int, default=40)
    a = ap.parse_args()
    client = Client()
    path = os.path.join(ROOT, "runs", "stress_scan.json")
    out = json.load(open(path)) if os.path.exists(path) else {}
    for i in range(1, a.n + 1):
        seed = f"{a.prefix}-{i}"
        if seed in out:
            continue
        try:
            out[seed] = scan(client, seed)
        except SpreadError as e:
            print(f"{seed}: {e}")
            continue
        json.dump(out, open(path, "w"), indent=1)
    rows = sorted(out.values(), key=lambda r: -r["stress"])
    for r in rows[:12]:
        print(f"{r['seed']:>7} stress {r['stress']:>6}  drivers {r['drivers']:>4}  bulk {r['bulk_size']:>2} on {r['bulk_day']} from {r['bulk_yard']} "
              f"(yard drivers {r['yard_drivers']})  snow {r['snow']}")


if __name__ == "__main__":
    main()
