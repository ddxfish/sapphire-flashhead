#!/usr/bin/env bash
# How fast this GPU renders her face (bench.py). Run the server once first:
# it makes the env and downloads the models this uses.
#
#   ./flashhead-bench.sh                     eager, compiled, sdpa
#   ./flashhead-bench.sh compiled autotune   only these
#
# Inside Sapphire: FLASHHEAD_ENV=sapphire-plugin-flashhead ./flashhead-bench.sh
set -euo pipefail
cd "$(dirname "$0")"
eval "$(conda shell.bash hook)"
conda activate "${FLASHHEAD_ENV:-flashhead-server}" || { echo "No env yet: run ./flashhead-server.sh once first"; exit 1; }
exec python bench.py "$@"
