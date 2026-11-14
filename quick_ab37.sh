#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_sn15 --seeds ev-1,ev-4 --parallel 2 --keep --set select_net=15
python quick_test.py --tag q_sn25 --seeds ev-2 --parallel 1 --keep --set select_net=25
python quick_test.py --tag q_sn15b --seeds ev-2 --parallel 1 --keep --set select_net=15
echo QUICK_AB37_DONE
