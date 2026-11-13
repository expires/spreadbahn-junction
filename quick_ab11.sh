#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480"
python quick_test.py --tag q_sm1 --seeds ev-1 --parallel 1 --keep --set $B use_supply_model=1
python quick_test.py --tag q_sm4 --seeds ev-4 --parallel 1 --keep --set $B use_supply_model=1
echo QUICK_AB11_DONE
