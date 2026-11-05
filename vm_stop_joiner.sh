#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export XDG_RUNTIME_DIR=/run/user/$(id -u)
exec python3 "$SCRIPT_DIR"/meeting_session.py --stop-current
