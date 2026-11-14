"""Exploration runs: play practice worlds with randomised quotes and flat randomised fees, then save
everything the world reveals. The randomisation is the point: it makes the logs usable for fitting
P(win | price) and P(booked by t | fee) without the bias of a fixed policy.

    python explore.py --level 8 --seeds 20 --parallel 6
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from spread.client import Client, SpreadError
from spread.starter import DAY, WAKE_ON, Desk, steep
from spread.types import FeeStep, Order, PriceRequest

from bahn.data.reveal_io import save
from bahn.paths import DATA


class ExploreDesk(Desk):
    """The starter's bookkeeping, with randomised decisions. Each choice is logged."""

    def __init__(self, world, level, now, rng: random.Random):
        super().__init__(world, level, now)
        self.rng = rng
        self.fee_ratio: dict[str, float] = {}  # order id -> flat fee / typical fee
        self.log: list[dict] = []

    def quote(self, request: PriceRequest) -> None:
        self.requests[request["id"]] = request
        base = self.rng.uniform(0.9, 1.9)  # level 8 competitor sits ~1.26-1.54x list
        prices = {o["kind"]: int(o["listPriceCents"] * base * self.rng.uniform(0.92, 1.08)) for o in request["options"]}
        self.outbox.append({"type": "quote", "requestId": request["id"], "prices": prices})
        self.log.append({"t": "quote", "at": self.now, "requestId": request["id"], "base": base, "prices": prices})

    def fees_for(self, order: Order, cars: int) -> list[FeeStep]:
        fee = self.requests[order["id"]]["referenceFeeCents"] * cars / len(order["legs"])
        if order["id"] in self.urgent:
            return steep(self.now, fee, self.level)
        if order["id"] not in self.fee_ratio:
            self.fee_ratio[order["id"]] = r = self.rng.uniform(0.7, 2.2)
            self.log.append({"t": "fee", "at": self.now, "orderId": order["id"], "ratio": r})
        # Flat: one step, so every view is an observation at a known fee.
        return [{"at": self.now, "feeCents": max(1, int(fee * self.fee_ratio[order["id"]]))}]


def play(seed: str, level: int) -> dict:
    client = Client()
    world = client.create(level=level, seed=seed)
    rng = random.Random(f"explore:{seed}:{level}")
    t0 = time.time()
    try:
        snap = world.snapshot()
        desk, ends = ExploreDesk(world, level, snap["now"], rng), snap["endsAt"]
        while desk.now < ends:
            actions, desk.outbox = desk.outbox, []
            step = world.advance(until=WAKE_ON, max=min(ends, (desk.now // DAY + 1) * DAY), actions=actions)
            desk.read_results(actions, step["results"])
            desk.now = step["now"]
            for event in step["events"]:
                desk.handle(event)
    except BaseException:
        try:
            world.delete()
        except SpreadError:
            pass
        raise
    save(client, world, seed, level, desk.log)
    score = world.score()
    return {"seed": seed, "world": world.id, "secs": round(time.time() - t0), **score}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=8)
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--prefix", default="ex")
    ap.add_argument("--parallel", type=int, default=6, help="at most 8 practice slots per team")
    args = ap.parse_args()
    seeds = [f"{args.prefix}-{i}" for i in range(args.start, args.start + args.seeds)]
    todo = [s for s in seeds if not os.path.exists(os.path.join(DATA, f"L{args.level}", s, "reveal.json.gz"))]
    with ProcessPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(play, s, args.level): s for s in todo}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                print(f"{r['seed']:>8}  margin €{r['marginCents']/100:>11,.2f}  {r['wonOrders']:>4} orders  "
                      f"{r['onTimeShare']:6.1%} on time  {r['secs']}s", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"{futures[fut]}: FAILED {e!r}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
