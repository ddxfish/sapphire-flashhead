#!/usr/bin/env bash
# FlashHead as a GPU server on its own, no Sapphire on this computer.
# Other computers' Sapphires use it in Client only mode (README: One GPU,
# several Sapphires). Everything it keeps lands in ./user/ beside this file:
# the models (~8 GB), its certificate and the keys of the computers let in.
#
#   ./flashhead-server.sh               run it (builds its conda env the first time, ~5 GB)
#   ./flashhead-server.sh add <name>    let a computer in: prints its pairing string
#   ./flashhead-server.sh remove <name> shut one out
#   ./flashhead-server.sh list          who is let in
#
# PORT (5018) is the door other computers use; open it in your firewall.
# No NVIDIA GPU here? INSTALL_TEST=1 fetches and imports everything, then says it can't render.
set -euo pipefail
cd "$(dirname "$0")"
ENV=${FLASHHEAD_ENV:-flashhead-server}     # FLASHHEAD_ENV: use an env you already have
PORT=${PORT:-5018}
DOOR=user/worker_door/flashhead

command -v conda >/dev/null || { echo "conda not found: install Miniconda, then run this again"; exit 1; }
eval "$(conda shell.bash hook)"

if ! conda env list | grep -q "^$ENV "; then
    echo "Making the $ENV environment (once)..."
    conda create -y -n "$ENV" python=3.10
fi
conda activate "$ENV"

# The packages are the ones plugin.json lists, read from it: one list, never two.
# What was installed is noted inside the env, so a remade env installs again.
mkdir -p user
STAMP="$CONDA_PREFIX/flashhead-requirements.txt"
python -c "import json; print('\n'.join(json.load(open('plugin.json', encoding='utf-8'))['environment']['pip']))" > user/requirements.new
if ! cmp -s user/requirements.new "$STAMP"; then
    echo "Installing packages (the first time downloads ~5 GB)..."
    python -m pip install -r user/requirements.new
    mv user/requirements.new "$STAMP"
else
    rm user/requirements.new
fi

case "${1:-run}" in
    add)    python worker_door.py add "$DOOR" "${2:?a name, like vm-sapphire}" "$PORT" ;;
    remove) python worker_door.py remove "$DOOR" "${2:?which name}" ;;
    list)   python worker_door.py list "$DOOR" ;;
    run)
        export SAPPHIRE_ROOT="$PWD" SAPPHIRE_SERVICE_PORT=5017
        IT=false; [ "${INSTALL_TEST:-}" = 1 ] && IT=true
        export SAPPHIRE_PLUGIN_SETTINGS="{\"share\": true, \"share_port\": $PORT, \"install_test\": $IT}"
        echo "FlashHead server: computers let in reach it on port $PORT (./flashhead-server.sh add <name>)"
        exec python server.py ;;
    *) sed -n '2,13p' "$0"; exit 2 ;;
esac
