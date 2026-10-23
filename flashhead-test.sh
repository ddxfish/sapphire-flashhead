#!/usr/bin/env bash
# Check a FlashHead server from this computer, the way a Sapphire would use it.
# Run flashhead-server.sh once first (it makes the env this uses).
#
#   ./flashhead-test.sh "<pairing string>" [face image]
#
# The pairing string comes from the server's `add <name>`. Quote it.
set -euo pipefail
cd "$(dirname "$0")"
eval "$(conda shell.bash hook)"
conda activate "${FLASHHEAD_ENV:-flashhead-server}" || { echo "No env yet: run ./flashhead-server.sh once first"; exit 1; }
exec python door_test.py "$@"
