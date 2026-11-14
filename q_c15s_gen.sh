#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
X="comp_from_qm=1 gate_pl=0.15 select_net=5"
python quick_test.py --tag vb_c15s --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --keep --set $X
python quick_test.py --tag vc_c15s --seeds vc-1,vc-2,vc-3,vc-4 --parallel 2 --keep --set $X
echo C15S_GEN_DONE
