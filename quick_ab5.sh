#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_qm1 --seeds ev-1 --parallel 1 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480
python quick_test.py --tag q_qm2 --seeds ev-2 --parallel 1 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480
echo QUICK_AB5_DONE
