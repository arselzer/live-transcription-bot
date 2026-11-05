#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export PULSE_SERVER=unix:$XDG_RUNTIME_DIR/pulse/native
exec python3 "$SCRIPT_DIR"/vm_prepare.py
