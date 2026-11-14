#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="price_mult=1.04 use_fee_model=1"
python quick_test.py --tag q_g20c_1 --seeds ev-1 --parallel 1 --keep --set $B gate_pl=0.20
python quick_test.py --tag q_g20c_4 --seeds ev-4 --parallel 1 --keep --set $B gate_pl=0.20
python quick_test.py --tag q_pm6_4 --seeds ev-4 --parallel 1 --keep --set $B gate_pl=0.15 price_mult=1.06
python quick_test.py --tag q_pm6_2 --seeds ev-2 --parallel 1 --keep --set $B gate_pl=0.15 price_mult=1.06
echo QUICK_AB33_DONE
