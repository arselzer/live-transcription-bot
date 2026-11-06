#!/usr/bin/env python3
"""Exact-time calendar dispatch, verified again against the live shared feed.

No messages are sent by this worker. Hermes retains its existing reminders.
Scheduling is separate from confirmed joining; no successful service launch is
ever described as admission to a meeting.
"""

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from live_transcript.runtime.launcher import (
    command as launch_command,
    environment as launch_environment,
)
from pathlib import Path
import subprocess
import sys
from live_transcript.worker import command as worker_command
import time

from live_transcript.capture.guard import atomic_json
from live_transcript.core.config import get
from live_transcript.storage.layout import maintenance_active

ROOT = Path(get("paths", "logs")) / "calendar"


def key(event):
    return hashlib.sha256(f"{event['uid']}|{int(event['start'].timestamp())}".encode()).hexdigest()[
        :24
    ]


def duration(event):
    end = event.get("end")
    if end is None:
        return 180
    # Keep the original three-hour allowance even for short calendar entries;
    # an ordinary meeting overrun must not truncate the user's recording.
    return max(180, min(480, math.ceil((end - event["start"]).total_seconds() / 60) + 30))


def schedule(event, now):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ROOT / (key(event) + ".json")
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return json.loads(path.read_text()) if path.exists() else None
        return schedule_locked(event, now)


def schedule_locked(event, now):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ident = key(event)
    path = ROOT / (ident + ".json")
    if path.exists():
        current = json.loads(path.read_text())
        if current.get("status") in ("started", "complete", "cancelled", "terminal", "missed"):
            return current
        for suffix in (".timer", ".service"):
            active = subprocess.run(
                ["systemctl", "--user", "is-active", current["unit"] + suffix],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if active.returncode == 0:
                current.update(
                    verified_at=now,
                    snapshot=dict(
                        url=event["url"],
                        passcode=event.get("passcode"),
                        end=event["end"].timestamp() if event.get("end") else None,
                    ),
                )
                atomic_json(path, current)
                return current
    launch_at = max(now + 1, event["start"].timestamp() - 120)
    if now > event["start"].timestamp() + 600:
        return None
    unit = "live-transcript-calendar-" + ident
    at = dt.datetime.fromtimestamp(launch_at, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    job = dict(
        uid=event["uid"],
        start=event["start"].timestamp(),
        launch_at=launch_at,
        summary=event["summary"],
        unit=unit,
        status="scheduling",
        created=now,
        verified_at=now,
        snapshot=dict(
            url=event["url"],
            passcode=event.get("passcode"),
            end=event["end"].timestamp() if event.get("end") else None,
        ),
    )
    atomic_json(path, job)
    # A retry/reconciliation adopts a previously created timer instead of
    # counting a systemd duplicate-unit error as a successful join.
    active = subprocess.run(
        ["systemctl", "--user", "is-active", unit + ".timer"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if active.returncode:
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                "--unit=" + unit,
                "--setenv=LIVE_TRANSCRIPT_CONFIG="
                + __import__("live_transcript.core.config", fromlist=["*"]).CONFIG["_file"],
                "--on-calendar=" + at,
                "--timer-property=AccuracySec=1s",
                "--timer-property=Persistent=true",
                "--property=TimeoutStartSec=infinity",
                "--property=Nice=0",
                *worker_command("calendar-dispatch", path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            job.update(status="schedule_failed", error="systemd rejected exact launch timer")
            atomic_json(path, job)
            raise RuntimeError(job["error"])
    job["status"] = "scheduled"
    atomic_json(path, job)
    return job


def reconcile(events, now):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    valid = {key(ev): ev for ev in events if ev.get("url")}
    for path in ROOT.glob("*.json"):
        try:
            job = json.loads(path.read_text())
        except ValueError:
            continue
        if job.get("status") == "scheduled" and path.stem not in valid:
            with open(str(path) + ".lock", "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                job = json.loads(path.read_text())
                if job.get("status") != "scheduled":
                    continue
                subprocess.run(
                    ["systemctl", "--user", "stop", job["unit"] + ".timer"],
                    capture_output=True,
                    timeout=5,
                )
                job.update(status="cancelled", cancelled_at=now)
                atomic_json(path, job)
    for ev in valid.values():
        if now - 600 <= ev["start"].timestamp() <= now + 86400:
            schedule(ev, now)


def dispatch(path):
    from live_transcript.integrations.calendar import events, fetch_ics, meeting_active

    path = Path(path)
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        job = json.loads(path.read_text())
        if job.get("status") in ("complete", "started", "cancelled", "terminal", "missed"):
            return
        deadline = job["start"] + 600
        while time.time() <= deadline:
            try:
                current = next(
                    (ev for ev in events(fetch_ics()) if key(ev) == path.stem and ev.get("url")),
                    None,
                )
            except Exception as exc:
                # A timer may fire during a brief calendar outage. Use only a
                # recently revalidated private snapshot; a confirmed deletion
                # still cancels immediately and a stale snapshot cannot join.
                latest = json.loads(path.read_text())
                cached = latest.get("snapshot", {})
                if cached.get("url") and time.time() - latest.get("verified_at", 0) <= 900:
                    current = dict(
                        uid=latest["uid"],
                        start=dt.datetime.fromtimestamp(latest["start"], dt.timezone.utc),
                        end=dt.datetime.fromtimestamp(cached["end"], dt.timezone.utc)
                        if cached.get("end")
                        else None,
                        url=cached["url"],
                        passcode=cached.get("passcode"),
                    )
                    job["calendar_fallback"] = "recently revalidated snapshot"
                else:
                    job.update(status="calendar_retry", error=type(exc).__name__)
                    atomic_json(path, job)
                    time.sleep(15)
                    continue
            if current is None:
                job.update(
                    status="cancelled", reason="event missing, cancelled or moved in current feed"
                )
                atomic_json(path, job)
                return
            if time.time() < job["start"] - 120:
                time.sleep(min(15, job["start"] - 120 - time.time()))
                continue
            if maintenance_active() or meeting_active():
                job.update(status="waiting_for_idle")
                atomic_json(path, job)
                time.sleep(10)
                continue
            job.update(
                status="dispatching", attempted_at=time.time(), attempts=job.get("attempts", 0) + 1
            )
            atomic_json(path, job)
            env = dict(os.environ, LT_CALENDAR_JOB=str(path))
            args = [current["url"], "--max-min", str(duration(current))]
            if current.get("passcode"):
                args += ["--passcode", current["passcode"]]
            with open(ROOT / (path.stem + ".log"), "ab") as log:
                proc = subprocess.Popen(
                    launch_command(*args),
                    env=launch_environment(env),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    close_fds=True,
                )
                code = proc.wait()
            try:
                state = json.loads(
                    (Path(get("paths", "logs")) / "current_meeting.json").read_text()
                )
            except (OSError, ValueError):
                state = {}
            if state.get("calendar_job") == str(path) and state.get("run_id"):
                # The ordinary session watchdog owns all post-launch recovery.
                job.update(
                    status="complete" if code == 0 else "started", run_id=state["run_id"], exit=code
                )
                atomic_json(path, job)
                return
            if code in (2, 4, 5, 6) or job["attempts"] >= 3:
                job.update(status="terminal", exit=code)
                atomic_json(path, job)
                return
            time.sleep(15)
        job.update(status="missed", finished=time.time())
        atomic_json(path, job)


if __name__ == "__main__":
    os.umask(0o077)
    dispatch(sys.argv[1])
