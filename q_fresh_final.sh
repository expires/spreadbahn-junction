#!/bin/sh
set -e
cd "$(dirname "$0")"
. ./env.sh
python quick_test.py --tag fresh_shipped --seeds vb-1,vb-2,vb-3,vb-4,vb-5,vb-6 --parallel 2 --keep
echo FRESH_SHIPPED_DONE
