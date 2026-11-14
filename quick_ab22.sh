#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_s5_4 --seeds ev-4 --parallel 1 --keep --set select_qm=5
python quick_test.py --tag q_s5_1 --seeds ev-1 --parallel 1 --keep --set select_qm=5
python quick_test.py --tag q_s10_4 --seeds ev-4 --parallel 1 --keep --set select_qm=10
python quick_test.py --tag q_s10_1 --seeds ev-1 --parallel 1 --keep --set select_qm=10
echo QUICK_AB22_DONE
