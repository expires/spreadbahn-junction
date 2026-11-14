#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
G="gate_pl=0.15"
python quick_test.py --tag q_ls2 --seeds ev-2 --parallel 1 --keep --set $G late_shadow=150
python quick_test.py --tag q_ls4 --seeds ev-4 --parallel 1 --keep --set $G late_shadow=150
python quick_test.py --tag q_pm2 --seeds ev-2 --parallel 1 --keep --set $G price_mult=1.04
python quick_test.py --tag q_pm4 --seeds ev-4 --parallel 1 --keep --set $G price_mult=1.04
echo QUICK_AB28_DONE
