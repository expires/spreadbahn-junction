#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_e4h --seeds ev-4 --parallel 1 --keep --set pair_enabled=0 hold_hours=36
python quick_test.py --tag q_e4hp --seeds ev-4 --parallel 1 --keep --set pair_enabled=1 hold_hours=36 pair_wait=0 bundle_max_link=40 bundle_max_drive=330 bundle_max_dead_gap=18
echo QUICK_AB10_DONE
