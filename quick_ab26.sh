#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_g10_1 --seeds ev-1 --parallel 1 --keep --set gate_pl=0.10
python quick_test.py --tag q_g10_4 --seeds ev-4 --parallel 1 --keep --set gate_pl=0.10
python quick_test.py --tag q_g10_2 --seeds ev-2 --parallel 1 --keep --set gate_pl=0.10
echo QUICK_AB26_DONE
