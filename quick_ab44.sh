#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0"
python quick_test.py --tag q_v4a --seeds ev-1,ev-4 --parallel 2 --keep --set $B gate_pl=0.40
python quick_test.py --tag q_v5a --seeds ev-1,ev-4 --parallel 2 --keep --set $B gate_pl=0.0
python quick_test.py --tag q_v6a --seeds ev-2,vb-2 --parallel 2 --keep --set $B gate_pl=0.30
echo AB44_DONE
