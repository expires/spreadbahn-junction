#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_e4np --seeds ev-4 --parallel 1 --keep --set pair_enabled=0
python quick_test.py --tag q_e4tp --seeds ev-4 --parallel 1 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=40 bundle_max_drive=330 bundle_max_dead_gap=18
python quick_test.py --tag q_e1tp --seeds ev-1 --parallel 1 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=40 bundle_max_drive=330 bundle_max_dead_gap=18
echo QUICK_AB9_DONE
