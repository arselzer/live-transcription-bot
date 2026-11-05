#!/usr/bin/env python3
"""Upgrade a v1 live meeting to pushed captions without restarting its recorder.
The sidecar exits when the original meeting process exits. New meetings use the
v2 streamer's built-in subscription and do not need this compatibility bridge.
"""

import fcntl
from pathlib import Path
import sys
import threading
import time
import live_transcript.transcription.transport as stream


def main():
    owner = Path("/proc") / sys.argv[2]
    identity = (owner / "stat").read_text().split()[21]
    lock = open("/tmp/caption_subscription.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    threading.Thread(target=stream.caption_poller, daemon=True).start()
    try:
        while (owner / "stat").read_text().split()[21] == identity:
            time.sleep(0.5)
    except FileNotFoundError:
        pass
    finally:
        stream.STOP.set()


if __name__ == "__main__":
    main()
