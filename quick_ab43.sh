#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0"
python quick_test.py --tag q_v1a --seeds ev-1,ev-4 --parallel 2 --keep --set $B gate_pl=0.30
python quick_test.py --tag q_v2a --seeds ev-1,ev-4 --parallel 2 --keep --set $B gate_pl=0.20 late_shadow=40
python quick_test.py --tag q_v3a --seeds ev-1,ev-4 --parallel 2 --keep --set $B gate_pl=0.20 price_mult=1.06
echo AB43_DONE
