#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_hf1 --seeds ev-1 --parallel 1 --keep --set use_supply_fee=0
python quick_test.py --tag q_hf2 --seeds ev-2 --parallel 1 --keep --set use_supply_fee=0
python quick_test.py --tag q_hf4 --seeds ev-4 --parallel 1 --keep --set use_supply_fee=0
echo QUICK_AB24_DONE
