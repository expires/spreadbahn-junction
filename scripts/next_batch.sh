#!/bin/bash
# Wait for the running frontier, carry its baseline cache over to the current settings hash (new fields are
# switched off, so results are identical), then run the next batch of candidates.
cd "$(dirname "$0")/.." && . ./.env && . .venv/bin/activate
while pgrep -f "frontier.py" > /dev/null; do sleep 10; done
python - <<'PY'
import hashlib, json, os, shutil, sys
from dataclasses import asdict
sys.path.insert(0, "scripts")
from backtest import key
from bahn.simulation.dispatch import Policy
LATER = {"lanes_v9", "late_v9", "late_v9_scale", "late_v9_extras"}   # added after batch 1 started; off by default
d = {k: v for k, v in asdict(Policy()).items() if k not in LATER}
old = "runs/bt-" + hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:10] + ".json"
new = f"runs/bt-{key(Policy())}.json"
if os.path.exists(old) and not os.path.exists(new):
    shutil.copy(old, new); print("baseline cache", old, "->", new)
else:
    print("baseline cache: old", os.path.exists(old), "new", os.path.exists(new))
PYcd scripts && python -u frontier.py --suite full "$@" 2>&1 | grep --line-buffered -vE 'rows$|^ *(fr-|bt-)'
