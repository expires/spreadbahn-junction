#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1"
python quick_test.py --tag q_ff4 --seeds ev-4 --parallel 1 --keep --set $B fee_floor_target=0.7
python quick_test.py --tag q_ff5 --seeds ev-5 --parallel 1 --keep --set $B fee_floor_target=0.7
python quick_test.py --tag q_ff1 --seeds ev-1 --parallel 1 --keep --set $B fee_floor_target=0.7
python quick_test.py --tag q_of4 --seeds ev-4 --parallel 1 --keep --set $B offer_h=12 offer_fee_ratio=1.15 offer_radius_min=120
python quick_test.py --tag q_of5 --seeds ev-5 --parallel 1 --keep --set $B offer_h=12 offer_fee_ratio=1.15 offer_radius_min=120
echo QUICK_AB40_DONE
