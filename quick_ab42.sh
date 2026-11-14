#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_mid1 --seeds ev-1,ev-4 --parallel 2 --keep --set comp_from_qm=1 gate_pl=0.18 select_net=4
python quick_test.py --tag q_mid2 --seeds vb-2 --parallel 1 --keep --set comp_from_qm=1 gate_pl=0.18 select_net=4
python quick_test.py --tag q_mid3 --seeds vb-2 --parallel 1 --keep --set comp_from_qm=1 gate_pl=0.20 select_net=5
python quick_test.py --tag q_mid4 --seeds ev-1 --parallel 1 --keep --set comp_from_qm=1 gate_pl=0.20 select_net=5
echo QUICK_AB42_DONE
