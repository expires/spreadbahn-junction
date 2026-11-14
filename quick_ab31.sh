#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="gate_pl=0.15 price_mult=1.08"
python quick_test.py --tag q_cap4 --seeds ev-4 --parallel 1 --keep --set $B bundle_max_link=40 bundle_max_drive=320
python quick_test.py --tag q_cap2 --seeds ev-2 --parallel 1 --keep --set $B bundle_max_link=40 bundle_max_drive=320
python quick_test.py --tag q_p12_4 --seeds ev-4 --parallel 1 --keep --set $B price_mult=1.12
python quick_test.py --tag q_p12_2 --seeds ev-2 --parallel 1 --keep --set $B price_mult=1.12
echo QUICK_AB31_DONE
