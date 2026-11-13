#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag qA_e4 --seeds ev-4 --parallel 1 --keep --set hold_hours=36 pair_enabled=0
python quick_test.py --tag qB_e4 --seeds ev-4 --parallel 1 --keep --set hold_hours=0 pair_enabled=0
python quick_test.py --tag qC_e4 --seeds ev-4 --parallel 1 --keep --set hold_hours=0 pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480
echo QUICK_AB15_DONE
