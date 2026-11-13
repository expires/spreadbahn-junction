#!/bin/sh
# Pair-bundling A/B on the archived seeds. One world at a time (the partner sweeps on the same key).
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_imm  --seeds ev-1,ev-2 --parallel 1 --set pair_enabled=0
python quick_test.py --tag q_pair --seeds ev-1,ev-2 --parallel 1 --set pair_enabled=1
echo QUICK_AB_DONE
