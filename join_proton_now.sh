#!/usr/bin/env bash
# Compatibility launcher; pass a Proton invitation as the first argument.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m live_transcript start "$@"
