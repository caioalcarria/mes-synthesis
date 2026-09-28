#!/usr/bin/env bash
set -e

if ! id -nG | grep -qw input; then
    exec sg input -c "$(printf '%q ' "$0" "$@")"
fi

DIR="$HOME/PROJECTS/mes-synthesis"
VENV="$DIR/.venv"
SP="$VENV/lib/python3.12/site-packages"

export LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HF_HOME="$DIR/.hf"
export PYTHONUNBUFFERED=1

exec "$VENV/bin/python" "$DIR/app.py" "$@"
