#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag fresh_g20 --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --set gate_pl=0.20 use_fee_model=0 price_mult=1.0
python quick_test.py --tag fresh_ship20 --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --set gate_pl=0.20 use_fee_model=1 price_mult=1.04
echo FRESH_CMP2_DONE
