#!/usr/bin/env bash
# Compatibility launcher; invitations are supplied at invocation, never embedded.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m live_transcript start "$@"
