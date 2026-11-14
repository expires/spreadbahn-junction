#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 gate_pl=0.15 select_net=5"
python quick_test.py --tag q_c15s1 --seeds ev-1 --parallel 1 --keep --set $B
python quick_test.py --tag q_c15s4 --seeds ev-4 --parallel 1 --keep --set $B
python quick_test.py --tag q_c15s5 --seeds ev-5 --parallel 1 --keep --set $B
echo QUICK_AB41_DONE
