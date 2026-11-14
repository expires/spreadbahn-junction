#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0 gate_pl=0.30"
python quick_test.py --tag q_shortoff4 --seeds vb-2 --parallel 1 --keep --set $B risk_off_short=0.04
echo SHORTOFF4_DONE
