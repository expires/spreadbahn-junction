#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0 gate_pl=0.30"
python quick_test.py --tag q_shortoff --seeds vb-2,ev-4 --parallel 2 --keep --set $B risk_off_short=0.05
python quick_test.py --tag pilot_bt1 --seeds bt-1 --parallel 1 --keep
echo SAFETY_PILOT_DONE
