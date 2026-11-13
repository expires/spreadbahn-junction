#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_hs1 --seeds ev-1 --parallel 1 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480 late_shadow=200 sup_fee=1.5
python quick_test.py --tag q_hs2 --seeds ev-2 --parallel 1 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=60 bundle_max_drive=480 late_shadow=200 sup_fee=1.5
echo QUICK_AB6_DONE
