#!/usr/bin/env bash
# Postprocess is optional; failure never removes original media.
set -euo pipefail
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
CONFIG_SHELL=$(python3 "$SCRIPT_DIR/runtime_config.py" --shell)
eval "$CONFIG_SHELL"
mkdir -p "$LT_LOGS"
WAV="$1"
LOG="$LT_LOGS/post-$(basename "$WAV" .wav).log"
exec >>"$LOG" 2>&1
V="$LT_POSTPROCESS_PYTHON"
[ -x "$V" ] || V=python3
"$V" "$LT_ROOT/postprocess.py" "$WAV"
MD="${WAV%.wav}.md"
if [ -n "$LT_VAULT" ] && [ -d "$LT_VAULT" ]; then
  mkdir -p "$LT_VAULT/meetings"
  cp "$MD" "$LT_VAULT/meetings/$(basename "$MD")"
fi
