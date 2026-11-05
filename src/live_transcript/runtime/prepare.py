#!/usr/bin/env python3
"""Prepare an idle bot VM without killing unrelated displays or live meetings."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import fcntl

from live_transcript.runtime.session import assert_idle, process_args

from live_transcript.core.config import get

HOME = Path(get("vm", "home"))


def owned_process(args):
    helpers = {
        str(HOME / "caption_stream.py").encode(),
        str(HOME / "caption_render.py").encode(),
        b"caption_stream.py",
        b"caption_render.py",
    }
    if any(arg in helpers for arg in args):
        return True
    if args and args[0].rsplit(b"/", 1)[-1] == b"ffmpeg":
        return any(
            b"/tmp/capchunks_" in arg
            or (str(HOME / "rec") + "/").encode() in arg
            or arg == b"/dev/video0"
            for arg in args
        )
    return any(
        arg
        in (
            ("--user-data-dir=" + str(HOME / "chrome-prof")).encode(),
            ("--user-data-dir=" + str(HOME / "chrome-proton-prof")).encode(),
        )
        for arg in args
    )


def prepare_contents():
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--state=active,activating,deactivating",
            "--no-legend",
            "live-transcript-archive-*",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    for line in result.stdout.splitlines():
        subprocess.run(["systemctl", "--user", "stop", line.split()[0]], check=True, timeout=90)
    stale = {}
    for proc in Path("/proc").iterdir():
        try:
            if proc.name.isdigit() and proc.stat().st_uid == os.getuid():
                args = process_args(proc.name)
                if owned_process(args):
                    stale[int(proc.name)] = args
        except OSError:
            continue
    for pid in stale:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 8
    while stale and time.monotonic() < deadline:
        stale = {pid: args for pid, args in stale.items() if process_args(pid) == args}
        time.sleep(0.2)
    for pid, args in stale.items():
        if process_args(pid) == args:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    legacy = HOME / "rec" / ("legacy-sidecars-" + str(time.time_ns()))
    for name in ("notes.txt", "speakers.jsonl", "chat.txt", "voice.jsonl"):
        source = HOME / "rec" / name
        if source.exists():
            legacy.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, legacy / name)
            source.unlink()
    for name in (
        "captions.tsv",
        "captions.json",
        "nowspeaking.txt",
        "gpu.json",
        "todo.txt",
        "talktime.json",
        "voice_ready.json",
    ):
        (HOME / name).unlink(missing_ok=True)
    Path("/tmp/cap_session_start").unlink(missing_ok=True)
    result = subprocess.run(
        ["python3", str(HOME / "audioroute.py")],
        capture_output=True,
        text=True,
        timeout=30,
        env=dict(os.environ, LT_PREP_OWNER=str(os.getpid())),
    )
    if result.returncode or "PULSE-OK" not in result.stdout:
        raise RuntimeError("Audio topology could not be verified; refusing a silent recording")
    print("PREP-OK", flush=True)


def main(lease_owned=False):
    if lease_owned:
        return prepare_contents()
    assert_idle()
    with open(HOME / "logs/meeting-owner.lock", "a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return prepare_contents()


if __name__ == "__main__":
    main()
