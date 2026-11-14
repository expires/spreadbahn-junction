#!/bin/bash
# Wait for the running frontier, carry its baseline cache over to the current settings hash (new fields are
# switched off, so results are identical), then run the next batch of candidates.
cd "$(dirname "$0")/.." && . ./.env && . .venv/bin/activate
while pgrep -f "frontier.py" > /dev/null; do sleep 10; done
python - <<'PY'
import glob, json, os, shutil, sys
sys.path.insert(0, "scripts")
from backtest import key
from bahn.simulation.dispatch import Policy
new = f"runs/bt-{key(Policy())}.json"
full = [f for f in glob.glob("runs/bt-??????????.json") if len(json.load(open(f))) >= 24]
if not os.path.exists(new) and full:
    src = max(full, key=os.path.getmtime)
    shutil.copy(src, new); print("baseline cache", src, "->", new)
PY
cd scripts && python -u frontier.py --suite full "$@" 2>&1 | grep --line-buffered -vE 'rows$|^ *(fr-|bt-)'
