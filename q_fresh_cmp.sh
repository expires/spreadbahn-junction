#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag fresh_v8b --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --set gate_pl=0.30 use_fee_model=0 price_mult=1.0
python quick_test.py --tag fresh_pm8 --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --set gate_pl=0.15 price_mult=1.08 use_fee_model=0
echo FRESH_CMP_DONE
