#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
G="gate_pl=0.15"
python quick_test.py --tag q_sel8_1 --seeds ev-1 --parallel 1 --keep --set $G select_qm=8
python quick_test.py --tag q_sel8_4 --seeds ev-4 --parallel 1 --keep --set $G select_qm=8
python quick_test.py --tag q_sel8_2 --seeds ev-2 --parallel 1 --keep --set $G select_qm=8
echo QUICK_AB27_DONE
