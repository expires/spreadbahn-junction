#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q6_pm4 --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set gate_pl=0.15 price_mult=1.04
python quick_test.py --tag q6_pm8 --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set gate_pl=0.15 price_mult=1.08
echo Q6PM_DONE
