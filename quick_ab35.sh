#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
G="gate_pl=0.20 use_fee_model=0 price_mult=1.0"
python quick_test.py --tag q_sg7_2 --seeds vb-2 --parallel 1 --keep --set $G supply_gate=0.7
python quick_test.py --tag q_sg5_2 --seeds vb-2 --parallel 1 --keep --set $G supply_gate=0.5
python quick_test.py --tag q_sg7_4 --seeds ev-4 --parallel 1 --keep --set $G supply_gate=0.7
echo QUICK_AB35_DONE
