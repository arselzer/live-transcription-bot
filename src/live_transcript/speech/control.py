#!/usr/bin/env python3
"""Explicitly speak through Live Transcript: live-transcript say --lang en 'Hello'."""

import argparse
import json
from pathlib import Path
from live_transcript.core.config import get, ssh_options
import re
import shlex
import subprocess
import time
import uuid


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("text")
    ap.add_argument("--lang", choices=("en", "de"), default="en")
    a = ap.parse_args()
    if not a.text.strip() or len(a.text) > 500 or any(ord(c) < 32 for c in a.text):
        ap.error("speech must contain 1–500 characters on one line")
    meeting = json.loads((Path(get("paths", "logs")) / "current_meeting.json").read_text())
    if meeting.get("status") not in ("active", "control_lost"):
        raise SystemExit("No active meeting")
    log = Path(
        meeting.get(
            "control_log", str(Path(get("paths", "logs")) / ("join-" + meeting["run_id"] + ".log"))
        )
    ).read_text()
    paths = re.findall(r"RECORDING to (\S+)", log)
    if not paths:
        raise SystemExit("The bot has not joined yet")
    session = Path(paths[-1]).stem
    request = dict(session=session, language=a.lang, text=a.text, queued_at=time.time())
    ident = str(time.time_ns()) + "-" + uuid.uuid4().hex
    code = (
        """import sys,pathlib,json,os
root=pathlib.Path("""
        + repr(get("vm", "home"))
        + """)
request=json.load(sys.stdin)
try:
    ready=json.loads((root/'voice_ready.json').read_text())
    assert ready['session']==request['session']
    os.kill(ready['pid'],0)
except (OSError,ValueError,KeyError,AssertionError):
    raise SystemExit('Voice support requires the next Live Transcript join; no speech queued.')
p=root/'voice_requests'
p.mkdir(exist_ok=True)
"""
        + f"(p/'{ident}.tmp').write_text(json.dumps(request))\n(p/'{ident}.tmp').rename(p/'{ident}.json')"
    )
    subprocess.run(
        [
            "ssh",
            *ssh_options(),
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            get("vm", "ssh"),
            shlex.join([get("vm", "python"), "-c", code]),
        ],
        input=json.dumps(request),
        text=True,
        check=True,
        timeout=20,
    )
    print("Speech request queued:", ident)


if __name__ == "__main__":
    main()
