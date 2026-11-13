#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480"
python quick_test.py --tag q_steer --seeds ev-1 --parallel 1 --keep --set $B steer=0.15
python quick_test.py --tag q_sup --seeds ev-1 --parallel 1 --keep --set $B sup_fee=1.5
python quick_test.py --tag q_both --seeds ev-1 --parallel 1 --keep --set $B steer=0.15 sup_fee=1.5
echo QUICK_AB7_DONE
