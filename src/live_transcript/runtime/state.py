"""Atomic, owner-checked host meeting bookkeeping for Hermes and the launcher."""

import argparse
import fcntl
import json
from pathlib import Path
import time
import os
import sys
from live_transcript.capture.guard import atomic_json
from live_transcript.core.options import rejoin_options, zoom_invitation
from live_transcript.core.config import get


def begin(url, args, directory=None):
    options, minutes = rejoin_options(args)
    from live_transcript.providers.proton import is_proton_url, validate_url
    if is_proton_url(url):
        validate_url(url)
    else:
        from live_transcript.providers.google_meet import is_google_url
        if is_google_url(url):
            from live_transcript.providers.google_meet import validate_url as validate_google
            validate_google(url)
        else:
            zoom_invitation(url)
    directory = Path(directory or get("paths", "logs"))
    token = os.environ["LT_RUN_ID"]
    if not __import__("re").fullmatch(r"[A-Za-z0-9-]+", token):
        raise ValueError("invalid run token")
    expected = os.environ.get("LT_EXPECT_RUN_ID")
    path = directory / "current_meeting.json"
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if expected:
            current = json.loads(path.read_text())
            if current.get("run_id") != expected:
                raise RuntimeError("Recovery superseded by another meeting; refusing to replace it")
        secret = directory / "secrets" / (token + ".json")
        secret.parent.mkdir(exist_ok=True, mode=0o700)
        atomic_json(secret, dict(argv=args))
        safe = dict(options)
        if "--passcode" in safe:
            safe["--passcode"] = "REDACTED"
        import shlex

        now = time.time()
        state = dict(
            url=url,
            args=shlex.join([v for item in safe.items() for v in item]),
            started=now,
            deadline=float(os.environ.get("LT_DEADLINE", now + minutes * 60)),
            pid=os.getppid(),
            run_id=token,
            args_file=str(secret),
            status="active",
            dry_run="--dry" in args,
        )
        if os.environ.get("LT_CONTROL_LOG"):
            state["control_log"] = os.environ["LT_CONTROL_LOG"]
        if os.environ.get("LT_CALENDAR_JOB"):
            state["calendar_job"] = os.environ["LT_CALENDAR_JOB"]
        try:
            state["control_start_ticks"] = (
                Path(f"/proc/{state['pid']}/stat").read_text().rsplit(")", 1)[1].split()[19]
            )
        except OSError:
            pass
        atomic_json(path, state)


def update(path, pid, fields):
    path = Path(path)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        if state.get("pid") != pid:
            return False
        state.update(fields)
        atomic_json(path, state)
        return True


def main():
    if sys.argv[1:2] == ["--begin"]:
        begin(sys.argv[2], sys.argv[3:])
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("pid", type=int)
    ap.add_argument("--exit-code", type=int)
    ap.add_argument("--finished", action="store_true")
    ap.add_argument("--control-log")
    a = ap.parse_args()
    if a.control_log:
        atomic_json(Path(a.control_log + ".finished.json"), dict(utc=time.time(), exit=a.exit_code))
    try:
        state = json.loads(Path(a.path).read_text())
    except (OSError, ValueError):
        return
    if a.finished:
        fields = dict(status="finished", finished=time.time())
    elif state.get("status") == "finished":
        return
    else:
        fields = dict(
            status="control_lost", control_exit=a.exit_code, control_exited_at=time.time()
        )
        if a.control_log:
            try:
                import re

                text = Path(a.control_log).read_text()
                # Earlier denial is not terminal after a successful fallback.
                text = text[
                    max(text.rfind("ADMISSION_SUCCEEDED "), text.rfind("RECORDING to "), 0) :
                ]
                matches = re.findall(r"^ADMISSION_FAILED (.+)$", text, re.M)
                if matches:
                    failure = json.loads(matches[-1])
                    from live_transcript.providers.admission import REASONS

                    if failure.get("reason") in REASONS:
                        fields.update(
                            status="finished",
                            finished=time.time(),
                            result=failure["reason"],
                            remote_exit=4,
                        )
                        diagnostics = Path(a.control_log).parent / ("admission-" + state["run_id"])
                        if diagnostics.is_dir():
                            fields["admission_diagnostics"] = str(diagnostics)
            except (OSError, ValueError, TypeError):
                pass
    update(a.path, a.pid, fields)


if __name__ == "__main__":
    main()
