#!/usr/bin/env python3
"""Host compatibility entrypoint; all meetings use the supervised VM launcher."""

import os, sys
from pathlib import Path
from live_transcript.core.config import get

if __name__ == "__main__":
    launcher = str(Path(get("paths", "root")) / "scribe_run.sh")
    os.execv(launcher, [launcher, *sys.argv[1:]])
