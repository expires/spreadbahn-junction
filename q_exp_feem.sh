#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0 gate_pl=0.30"
python quick_test.py --tag q_exp_feem --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set $B use_fee_model=1
echo EXP_FEEM_DONE
