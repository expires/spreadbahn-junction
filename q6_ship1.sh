#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
X="gate_pl=0.20 price_mult=1.04 use_fee_model=1"
python quick_test.py --tag q6_ship1 --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set $X
echo Q6SHIP1_DONE
