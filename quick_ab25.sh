#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_g20b_1 --seeds ev-1 --parallel 1 --keep --set gate_pl=0.20
python quick_test.py --tag q_g20b_2 --seeds ev-2 --parallel 1 --keep --set gate_pl=0.20
python quick_test.py --tag q_g20b_4 --seeds ev-4 --parallel 1 --keep --set gate_pl=0.20
python quick_test.py --tag q_g15b_1 --seeds ev-1 --parallel 1 --keep --set gate_pl=0.15
python quick_test.py --tag q_g15b_4 --seeds ev-4 --parallel 1 --keep --set gate_pl=0.15
echo QUICK_AB25_DONE
