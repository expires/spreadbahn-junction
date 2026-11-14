#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q6_g20b --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2
echo Q6G20B_DONE
