#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
F="pair_enabled=1 pair_wait=20 bundle_max_link=35 bundle_max_drive=280 bundle_max_dead_gap=18 gate_pl=0.30"
DISPATCH_MODULE=dispatch python quick_test.py --tag fresh_v7 --seeds vb-1,vb-2,vb-3,vb-4 --parallel 2 --set late_shadow=50
python quick_test.py --tag fresh_final --seeds vb-1,vb-2,vb-3,vb-4 --parallel 2 --set $F
echo QFRESH_DONE
