#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q6_base --seeds ev-1,ev-2,ev-3,ev-4,ev-5,ev-6 --parallel 2 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480
echo Q6_DONE
