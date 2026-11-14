#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="gate_pl=0.15 price_mult=1.08"
python quick_test.py --tag q_fee4 --seeds ev-4 --parallel 1 --keep --set $B use_fee_model=1
python quick_test.py --tag q_fee2 --seeds ev-2 --parallel 1 --keep --set $B use_fee_model=1
python quick_test.py --tag q_offb4 --seeds ev-4 --parallel 1 --keep --set $B offer_h=12 offer_fee_ratio=1.15 offer_radius_min=120
python quick_test.py --tag q_offb2 --seeds ev-2 --parallel 1 --keep --set $B offer_h=12 offer_fee_ratio=1.15 offer_radius_min=120
echo QUICK_AB30_DONE
