"""Observable processing state on local disk, plus an artifact-side copy."""

import os
from pathlib import Path
import time
from live_transcript.core.files import atomic_json
from live_transcript.core.files import atomic_text as atomic_text
from live_transcript.core.config import get


def publish(wav, state):
    state = dict(state, utc=time.time(), audio_retained=str(wav))
    wav = Path(wav)
    # Keep watchdog reads off the archive mount. Tests/unmanaged inputs only
    # receive their adjacent status, avoiding collisions with managed sessions.
    prefix = os.path.abspath(get("paths", "recordings")).rstrip("/") + "/"
    if os.path.abspath(wav).startswith(prefix):
        root = Path(get("paths", "logs"))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(root / ("postprocess-" + wav.stem + ".json"), state)
    atomic_json(wav.with_suffix(".postprocess-status.json"), state)
    return state
