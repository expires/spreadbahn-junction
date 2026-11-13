#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480"
python quick_test.py --tag q_ev8 --seeds ev-1 --parallel 1 --keep --set $B ev_bar=8
python quick_test.py --tag q_ev14 --seeds ev-1 --parallel 1 --keep --set $B ev_bar=14
python quick_test.py --tag q_ev20 --seeds ev-1 --parallel 1 --keep --set $B ev_bar=20
echo QUICK_AB8_DONE
