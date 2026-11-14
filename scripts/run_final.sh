#!/bin/bash
# Live final / test final under a supervisor: if the bot dies, it restarts and rebuilds from the server in < 1 s.
set -o pipefail
cd "$(dirname "$0")/.." && . ./.env && . .venv/bin/activate
mkdir -p runs
until caffeinate -dims python -m bahn.simulation.dispatch --final 2>&1 | tee -a runs/final-$(date +%Y%m%d).log; do
  echo "$(date) bot exited; restarting in 1 s" | tee -a runs/final-$(date +%Y%m%d).log
  sleep 1
done
