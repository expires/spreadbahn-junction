#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_cqm1 --seeds ev-1,ev-4 --parallel 2 --keep --set comp_from_qm=1
python quick_test.py --tag q_sn5 --seeds ev-1,ev-4 --parallel 2 --keep --set select_net=5
python quick_test.py --tag q_cqm2 --seeds ev-2 --parallel 1 --keep --set comp_from_qm=1
echo QUICK_AB39_DONE
