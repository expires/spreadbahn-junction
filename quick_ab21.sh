#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_g20_4 --seeds ev-4 --parallel 1 --keep --set gate_pl=0.20
python quick_test.py --tag q_g20_1 --seeds ev-1 --parallel 1 --keep --set gate_pl=0.20
python quick_test.py --tag q_g20_2 --seeds vb-2 --parallel 1 --keep --set gate_pl=0.20
echo QUICK_AB21_DONE
