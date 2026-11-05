#!/usr/bin/env bash
# Cleanup is allowed only when the VM meeting lease is free.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export XDG_RUNTIME_DIR=/run/user/$(id -u)
exec python3 "$SCRIPT_DIR"/vm_prepare.py
