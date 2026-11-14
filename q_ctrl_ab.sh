#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0 gate_pl=0.30"
python quick_test.py --tag q_ctrl_b --seeds vb-2,ev-4 --parallel 2 --keep --set $B risk_control=1 risk_target=0.03 risk_gain=30 risk_hard=0.06
python quick_test.py --tag q_resc_a --seeds vb-2,ev-1 --parallel 2 --keep --set $B rescue_h=24 rescue_mult=1.35
echo CTRL_AB_DONE
