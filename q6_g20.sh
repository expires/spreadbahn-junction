#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q6_g20 --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --set gate_pl=0.20 use_fee_model=0 price_mult=1.0
echo Q6G20_DONE
