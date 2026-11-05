#!/usr/bin/env python3
"""Durable, resumable retrieval and postprocessing; original evidence is retained."""

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from live_transcript.capture.guard import atomic_json
from live_transcript.core.config import get


def recover_once(vm, wav, token, directory=None, do_postprocess=True):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", token):
        raise ValueError("invalid recovery token")
    directory = Path(directory or get("paths", "logs"))
    state_file = directory / ("recovery-" + token + ".json")
    destination = Path(get("paths", "recordings")) / Path(wav).name
    try:
        state = json.loads(state_file.read_text())
        if not isinstance(state, dict) or state.get("vm") != vm or state.get("source") != wav:
            raise ValueError("recovery token belongs to another recording")
    except FileNotFoundError:
        state = dict(created=time.time(), vm=vm, source=wav, attempts=0, postprocess_attempts=0)
    if state.get("status") == "complete":
        return state
    # A repeated request explicitly opens another bounded retry window, while
    # keeping the existing retrieval checkpoint and prior errors observable.
    if state.get("status") in ("retrieval_failed", "postprocess_failed"):
        state.update(created=time.time(), postprocess_attempts=0)
    while time.time() - state["created"] < 9 * 3600:
        saved = (
            bool(state.get("recording_saved_at"))
            and destination.is_file()
            and destination.stat().st_size > 44
        )
        if not saved:
            state.update(
                status="retrieving", attempts=state.get("attempts", 0) + 1, utc=time.time()
            )
            atomic_json(state_file, state)
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(get("paths", "root")) / "pull_recording.py"),
                        vm,
                        wav,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                )
                if result.returncode:
                    raise RuntimeError(result.stderr[-500:] or "retrieval failed")
                if not destination.is_file() or destination.stat().st_size <= 44:
                    raise RuntimeError("retrieval returned success without a usable recording")
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                state.update(status="retrying", error=str(exc)[-500:], utc=time.time())
                atomic_json(state_file, state)
                time.sleep(30)
                continue
            state.update(
                status="recording_saved", recording_saved_at=time.time(), error="", utc=time.time()
            )
            atomic_json(state_file, state)
        if not do_postprocess:
            state.update(status="complete", utc=time.time())
            atomic_json(state_file, state)
            return state
        state.update(
            status="postprocessing",
            postprocess_attempts=state.get("postprocess_attempts", 0) + 1,
            utc=time.time(),
        )
        atomic_json(state_file, state)
        try:
            result = subprocess.run(
                [str(Path(get("paths", "root")) / "postprocess.sh"), str(destination)],
                capture_output=True,
                text=True,
                timeout=7200,
            )
            if result.returncode:
                raise RuntimeError(result.stderr[-500:] or "postprocessing failed; inspect its log")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            state.update(status="postprocess_retrying", error=str(exc)[-500:], utc=time.time())
            if state["postprocess_attempts"] >= 3:
                state["status"] = "postprocess_failed"
                atomic_json(state_file, state)
                return state
            atomic_json(state_file, state)
            time.sleep(30 * state["postprocess_attempts"])
            continue
        state.update(status="complete", error="", utc=time.time())
        atomic_json(state_file, state)
        return state
    state.update(status="retrieval_failed", utc=time.time())
    atomic_json(state_file, state)
    return state


def run(vm, wav, token, directory=None, do_postprocess=True):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", token):
        raise ValueError("invalid recovery token")
    directory = Path(directory or get("paths", "logs"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / ("recovery-" + token + ".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status="already_running")
        return recover_once(vm, wav, token, directory, do_postprocess)


if __name__ == "__main__":
    os.umask(0o077)
    run(*sys.argv[1:4])
