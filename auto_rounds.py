"""Plays every ranking round as soon as it opens, at all levels (level 8 first), with the latest code.
Each round runs in a fresh process, so code committed between rounds is used.

    nohup python auto_rounds.py > runs/auto_rounds.log 2>&1 &
"""
import datetime
import subprocess
import sys
import time

from spread.client import Client, SpreadError

LEVELS = "8,7,6,5,4,3,2,1"


def played(client: Client) -> tuple[str, int]:
    r = client.board(8)["current"]
    me = next((t for t in r["teams"] if t["team"] == "alignas"), None)
    return r["name"], (me["played"] if me else 0)


def main() -> None:
    client = Client()
    done: set[str] = set()
    while True:
        try:
            name, n = played(client)
        except SpreadError as e:
            print(f"{datetime.datetime.now():%H:%M} board read failed: {e}", flush=True)
            time.sleep(60)
            continue
        if name not in done and n < 5:
            print(f"{datetime.datetime.now():%H:%M} round '{name}': playing levels {LEVELS}", flush=True)
            subprocess.run([sys.executable, "dispatch.py", "--board", "--levels", LEVELS])
            name2, n2 = played(client)
            print(f"{datetime.datetime.now():%H:%M} round '{name}' done: {n2} level-8 runs played", flush=True)
            done.add(name)
        elif n >= 5:
            done.add(name)
        time.sleep(60)


if __name__ == "__main__":
    main()
