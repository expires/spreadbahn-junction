#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag q_exp_menu --seeds ev-1,ev-4 --parallel 2 --keep --set use_quote_model=1
echo EXP_MENU_DONE
