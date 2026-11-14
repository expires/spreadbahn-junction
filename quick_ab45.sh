#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0 gate_pl=0.30"
python quick_test.py --tag q_fee82 --seeds ev-1,ev-4 --parallel 2 --keep --set $B fee_open=0.82
python quick_test.py --tag q_fee82s --seeds ev-1,ev-4 --parallel 2 --keep --set $B fee_open=0.82 fee_step_scale=0.9
python quick_test.py --tag q_cap42 --seeds ev-1,ev-4 --parallel 2 --keep --set $B bundle_max_link=42 bundle_max_drive=320
echo AB45_DONE
