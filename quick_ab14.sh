#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_e4v7b --seeds ev-4 --parallel 1 --set pair_enabled=0 hold_hours=36 use_notes=0 use_supply_model=0
echo QUICK_AB14_DONE
