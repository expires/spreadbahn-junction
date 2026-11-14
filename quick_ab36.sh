#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
G="gate_pl=0.20 gate_dynamic=2 use_fee_model=0 price_mult=1.0"
python quick_test.py --tag q_d2_2 --seeds vb-2 --parallel 1 --keep --set $G
python quick_test.py --tag q_d2_4 --seeds ev-4 --parallel 1 --keep --set $G
python quick_test.py --tag q_d2_1 --seeds ev-1 --parallel 1 --keep --set $G
echo QUICK_AB36_DONE
