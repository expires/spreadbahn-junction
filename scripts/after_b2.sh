#!/bin/bash
# Batch 3: the corrected "post at the win" (hold until 1000 h before last pickup = post at once), alone and with capped chains.
cd "$(dirname "$0")/.." && . ./.env && . .venv/bin/activate
while pgrep -f "next_batch.sh|frontier.py" > /dev/null; do sleep 10; done
cd scripts && python -u frontier.py --suite full "$@" 2>&1 | grep --line-buffered -vE 'rows$|^ *(fr-|bt-)'
