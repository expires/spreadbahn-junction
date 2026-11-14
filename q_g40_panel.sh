#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
X="comp_from_qm=1 select_net=0"
python quick_test.py --tag q6_g40 --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set $X gate_pl=0.40
echo G40_DONE
