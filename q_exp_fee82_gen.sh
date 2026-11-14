#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="comp_from_qm=1 select_net=0 gate_pl=0.30"
python quick_test.py --tag vb_fee82 --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --keep --set $B fee_open=0.82
python quick_test.py --tag vc_fee82 --seeds vc-1,vc-2,vc-3,vc-4 --parallel 2 --keep --set $B fee_open=0.82
python quick_test.py --tag q_exp_priv --seeds ev-1,ev-4 --parallel 2 --keep --set $B private_fee_est=1.15 tradein_fee_est=1.35
echo FEE82_GEN_DONE
