#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag ship_verify --seeds ev-1,ev-4 --parallel 2 --keep
echo SHIP_VERIFY_DONE
