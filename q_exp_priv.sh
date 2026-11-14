#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0 gate_pl=0.30"
python quick_test.py --tag q_exp_priv --seeds ev-1,ev-4 --parallel 2 --keep --set $B private_fee_est=1.15 tradein_fee_est=1.35
echo EXP_PRIV_DONE
