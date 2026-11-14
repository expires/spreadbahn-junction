#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q6_c15s --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set comp_from_qm=1 gate_pl=0.15 select_net=5
echo Q6C15S_DONE
