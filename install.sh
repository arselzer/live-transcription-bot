#!/usr/bin/env bash
# Checkout bootstrap. The Python installer owns provisioning and resumable state.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PYTHON=${LIVE_TRANSCRIPT_BOOTSTRAP_PYTHON:-python3}
"$PYTHON" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
GLOBAL=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config)
      [ "$#" -ge 2 ] || { echo '--config needs a filename' >&2; exit 2; }
      GLOBAL+=(--config "$2"); shift 2 ;;
    --json) GLOBAL+=(--json); shift ;;
    *) break ;;
  esac
done
for ARG in "$@"; do
  if [ "$ARG" = --plan ] || [ "$ARG" = --help ]; then
    # Plan/help do not install packages or create a virtual environment.
    exec env PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m live_transcript "${GLOBAL[@]}" setup "$@"
  fi
done
[ "$(id -u)" -ne 0 ] || { echo 'Run install.sh as your normal user, with sudo access.' >&2; exit 1; }
sudo -v
if ! "$PYTHON" -m venv --help >/dev/null 2>&1; then
  command -v apt-get >/dev/null || { echo 'Install the Python venv module first.' >&2; exit 1; }
  sudo -n apt-get update
  sudo -n apt-get install -y python3-venv
fi
if ! "$PYTHON" -m venv "$ROOT/.venv"; then
  command -v apt-get >/dev/null || exit 1
  sudo -n apt-get install -y python3-venv
  "$PYTHON" -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install -e "$ROOT"
exec "$ROOT/.venv/bin/live-transcript" "${GLOBAL[@]}" setup "$@"
