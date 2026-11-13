#!/bin/sh
# Round 2: selectivity (price multiplier) and post-immediately, on the six archived seeds ev-1..ev-6.
set -e
cd "$(dirname "$0")"
. ./env.sh
run() {
  tag=$1; shift
  echo "=== $tag: $*"
  python dispatch.py --seeds 6 --prefix ev --tag "$tag" --parallel 2 --set "$@" 2>&1 | tail -3
  echo
}
run swD hold_hours=0
run swE price_mult=1.15
run swF hold_hours=0 price_mult=1.15
run swG price_mult=1.30
echo ALL DONE
