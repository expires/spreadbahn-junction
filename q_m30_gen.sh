#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
X="comp_from_qm=1 gate_pl=0.30 select_net=0"
python quick_test.py --tag vb_m30 --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --keep --set $X
python quick_test.py --tag vc_m30 --seeds vc-1,vc-2,vc-3,vc-4 --parallel 2 --keep --set $X
echo M30_GEN_DONE
