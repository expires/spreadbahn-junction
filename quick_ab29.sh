#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
G="gate_pl=0.15"
python quick_test.py --tag q_pm8_1 --seeds ev-1 --parallel 1 --keep --set $G price_mult=1.08
python quick_test.py --tag q_pm8_2 --seeds ev-2 --parallel 1 --keep --set $G price_mult=1.08
python quick_test.py --tag q_pm8_4 --seeds ev-4 --parallel 1 --keep --set $G price_mult=1.08
echo QUICK_AB29_DONE
