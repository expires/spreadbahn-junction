"""Restart test: play to a minute, drop the desk, rebuild from the server, play on. Compare with an
uninterrupted run on the same seed.   python test_restart.py ev-1 3600"""
import sys

import dispatch
from dispatch import DAY, WAKE_ON, Policy, Road, rebuild
from spread.client import Client


def step_to(world, desk, until: int) -> None:
    while desk.now < until:
        actions, desk.outbox = desk.outbox, []
        step = world.advance(until=WAKE_ON + ["order_lost"], max=min(until, (desk.now // DAY + 1) * DAY, max(desk.now + 1, desk.next_timer())), actions=actions)
        desk.read_results(actions, step["results"])
        desk.now = step["now"]
        for e in step["events"]:
            desk.handle(e)
        desk.fire_timers()


def main() -> None:
    seed, cut = sys.argv[1], int(sys.argv[2])
    client, pol = Client(), Policy()
    road = Road(client.map())
    world = client.create(level=8, seed=seed)
    dispatch.LIVE.add(world.id)
    snap = world.snapshot()
    desk = dispatch.Dispatcher(world, 8, snap["now"], pol, road)
    desk.init_supply(world.roster())
    step_to(world, desk, cut)
    before = (len(desk.orders), dict(desk.trust), round(desk.log_comp, 3))
    desk, _, ends = rebuild(world, pol, road, 8)   # the old desk is gone
    after = (len(desk.orders), dict(desk.trust), round(desk.log_comp, 3))
    print("orders before/after rebuild:", before[0], after[0], "| trust equal:", before[1] == after[1], "| log comp", before[2], after[2])
    step_to(world, desk, ends)
    dispatch.LIVE.discard(world.id)
    s = world.score()
    print(f"restarted run: margin €{s['marginCents']/100:,.0f}  {s['wonOrders']} orders  {s['onTimeShare']:.1%} on time")


if __name__ == "__main__":
    main()
