#!/bin/sh
# Parameter sweeps on the six archived seeds ev-1..ev-6 (deterministic: same seed = same world).
# Runs two worlds at a time, never more (practice slots are shared with the partner).
set -e
cd "$(dirname "$0")"
. ./env.sh
run() {
  tag=$1; shift
  echo "=== $tag: $*"
  python dispatch.py --seeds 6 --prefix ev --tag "$tag" --parallel 2 --keep --set "$@" 2>&1 | tail -4
  echo
}
run swA late_shadow=150 sla_target=0.95
run swB late_shadow=150 min_margin=25
run swC late_shadow=150 sla_target=0.96 'p_late=[[4,0.75],[8,0.45],[16,0.2],[30,0.1],[1000000000.0,0.05]]'
echo ALL DONE
