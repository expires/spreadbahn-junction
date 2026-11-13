#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_pair2 --seeds ev-1 --parallel 1 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=90 bundle_max_drive=480 bundle_min_lead_h=3
python quick_test.py --tag q_pair3 --seeds ev-1 --parallel 1 --keep --set pair_enabled=1 pair_wait=20 bundle_max_link=90 bundle_max_drive=480 bundle_min_lead_h=3 bundle_base_ratio=1.0
echo QUICK_AB2_DONE
