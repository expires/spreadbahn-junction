#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0 gate_pl=0.30"
python quick_test.py --tag q_ctrl_agg --seeds vb-2,ev-4 --parallel 2 --keep --set $B risk_control=1 risk_target=0.02 risk_gain=120 risk_hard=0.04
echo CTRL_AGG_DONE
