#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
DISPATCH_MODULE=dispatch python quick_test.py --tag q_v7e4 --seeds ev-4 --parallel 1 --set late_shadow=50
DISPATCH_MODULE=dispatch python quick_test.py --tag q_v7e1 --seeds ev-1 --parallel 1 --set late_shadow=50
echo QUICK_AB13_DONE
