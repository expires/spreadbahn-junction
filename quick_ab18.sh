#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
CB="pair_enabled=1 pair_wait=20 bundle_max_link=35 bundle_max_drive=280 bundle_max_dead_gap=18"
python quick_test.py --tag q_cb4 --seeds ev-4 --parallel 1 --keep --set $CB
python quick_test.py --tag q_cb1 --seeds ev-1 --parallel 1 --keep --set $CB
python quick_test.py --tag q_off4 --seeds ev-4 --parallel 1 --keep --set offer_h=8
python quick_test.py --tag q_off2 --seeds ev-2 --parallel 1 --keep --set offer_h=8
echo QUICK_AB18_DONE
