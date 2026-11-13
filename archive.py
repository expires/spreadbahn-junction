"""Harvest the team's finished level-8 practice archive: reveal, events, snapshot, roster per world.

Reads only. No world is created, forked or deleted. Each world costs four practice tokens
(reveal, events, snapshot, roster); the archived reveal is what the offline evaluator and the
model fits need, and the end-of-week snapshot holds the recorded score for calibration.

    python archive.py                 # harvest everything that is not on disk yet
    python archive.py --status        # just count what is on the server and on disk
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from spread.client import Client, SpreadError, Unreachable

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
NAMES = ("reveal", "events", "snapshot", "roster")


def folder_of(wid: str) -> str:
    return os.path.join(DATA, "L8", wid)


def complete(wid: str) -> bool:
    return all(os.path.exists(os.path.join(folder_of(wid), f"{n}.json.gz")) for n in NAMES)


def fetch(wid: str) -> str:
    if complete(wid):
        return "skip"
    folder = folder_of(wid)
    os.makedirs(folder, exist_ok=True)
    started = time.time()
    for attempt in range(4):
        try:
            client = Client()
            world = client.world(wid)
            objs = {n: getattr(world, n)() for n in NAMES}
            break
        except Unreachable as error:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    else:  # pragma: no cover
        raise RuntimeError("unreachable")
    for name, obj in objs.items():
        with gzip.open(os.path.join(folder, f"{name}.json.gz"), "wt") as fh:
            json.dump(obj, fh)
    if not os.path.exists(os.path.join(folder, "decisions.json.gz")):
        # fit.py's fees frame reads this log; we rebuild the fee ratio from the snapshot instead.
        with gzip.open(os.path.join(folder, "decisions.json.gz"), "wt") as fh:
            json.dump([], fh)
    return f"{time.time() - started:.2f}s"


def main() -> None:
    status_only = "--status" in sys.argv
    os.makedirs(DATA, exist_ok=True)
    client = Client()
    worlds = client.worlds()
    finished = [w for w in worlds if w["level"] == 8 and w["ended"] and w["kind"] == "practice" and w["boardRun"] is None]
    boards = [w for w in worlds if w["level"] == 8 and w["ended"] and w["boardRun"] is not None]
    live = [w for w in worlds if not w["ended"]]
    with open(os.path.join(DATA, "worlds.json"), "w") as fh:
        json.dump(worlds, fh, indent=1)
    todo = [w["worldId"] for w in finished if not complete(w["worldId"])]
    print(f"worlds {len(worlds)}  finished reveal-able L8 {len(finished)}  board(no reveal) {len(boards)}  live {len(live)}  to harvest {len(todo)}", flush=True)
    if live:
        print("live worlds: " + ", ".join(w["worldId"] for w in live), flush=True)
    if status_only or not todo:
        return
    done = failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch, wid): wid for wid in todo}
        for fut in as_completed(futures):
            wid = futures[fut]
            try:
                fut.result()
                done += 1
            except (SpreadError, OSError) as error:
                failed += 1
                print(f"{wid}: FAILED {error!r}", flush=True)
            if (done + failed) % 20 == 0:
                print(f"  {done + failed}/{len(todo)} in {time.time() - t0:.0f}s (failed {failed})", flush=True)
    print(f"harvested {done} worlds, {failed} failed, {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
