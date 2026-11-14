#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q6_m30 --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set comp_from_qm=1 gate_pl=0.30 select_net=0
echo Q6_M30_DONE
