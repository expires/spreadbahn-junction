#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag qS_e4 --seeds ev-4 --parallel 1 --keep --set use_supply_model=1
python quick_test.py --tag qS_e2 --seeds ev-2 --parallel 1 --keep --set use_supply_model=1
python quick_test.py --tag qG_e4 --seeds ev-4 --parallel 1 --keep --set use_supply_model=1 gate_pl=0.2
python quick_test.py --tag qG_e2 --seeds ev-2 --parallel 1 --keep --set use_supply_model=1 gate_pl=0.2
echo QUICK_AB17_DONE
