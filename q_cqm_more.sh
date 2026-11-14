#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag vb_cqm --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --keep --set comp_from_qm=1
python quick_test.py --tag q6_cqm_g15 --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set comp_from_qm=1 gate_pl=0.15
echo CQM_MORE_DONE
