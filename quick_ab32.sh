#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
B="gate_pl=0.15"
python quick_test.py --tag q_cq1 --seeds ev-1 --parallel 1 --keep --set $B comp_from_qm=1
python quick_test.py --tag q_cq2 --seeds ev-2 --parallel 1 --keep --set $B comp_from_qm=1
python quick_test.py --tag q_cq4 --seeds ev-4 --parallel 1 --keep --set $B comp_from_qm=1
echo QUICK_AB32_DONE
