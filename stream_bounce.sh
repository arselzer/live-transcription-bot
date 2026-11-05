#!/usr/bin/env bash
# Let the owning meeting supervisor restart captions with the correct display/session.
set -euo pipefail
python3 - <<'PYINNER'
import os,signal
from pathlib import Path
pid=int(Path('/tmp/caption_stream.pid').read_text())
args=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
assert any(arg.rsplit(b'/',1)[-1]==b'caption_stream.py' for arg in args)
os.kill(pid,signal.SIGTERM)
print('Caption restart requested; independent archival recording is untouched')
PYINNER
