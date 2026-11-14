#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag vc_v8b --seeds vc-1,vc-2,vc-3,vc-4 --parallel 2 --set gate_pl=0.30 use_fee_model=0 price_mult=1.0
python quick_test.py --tag vc_g20 --seeds vc-1,vc-2,vc-3,vc-4 --parallel 2 --set gate_pl=0.20 use_fee_model=0 price_mult=1.0
python quick_test.py --tag vc_ship --seeds vc-1,vc-2,vc-3,vc-4 --parallel 2 --set gate_pl=0.15 use_fee_model=1 price_mult=1.04
echo VC_DONE
