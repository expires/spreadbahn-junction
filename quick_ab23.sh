#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_m1 --seeds ev-1 --parallel 1 --keep
python quick_test.py --tag q_m2 --seeds ev-2 --parallel 1 --keep
python quick_test.py --tag q_m4 --seeds ev-4 --parallel 1 --keep
echo QUICK_AB23_DONE
