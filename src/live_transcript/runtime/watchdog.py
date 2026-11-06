#!/usr/bin/env python3
"""Hermes watchdog: bounded, session-scoped repairs; unknown is never dead."""

from live_transcript.worker import guest_command

import argparse
import fcntl
import hashlib
import json
import os
from live_transcript.worker import command as worker_command
from live_transcript.runtime.launcher import (
    command as launch_command,
    environment as launch_environment,
)
from pathlib import Path
import shlex
import socket
import subprocess
import time

from live_transcript.core.options import rejoin_options
from live_transcript.capture.guard import atomic_json
from live_transcript.runtime.state import update
from live_transcript.core.config import get

VM = get("vm", "ssh")
STATE = Path(get("paths", "logs")) / "watchdog-state.json"
MJSON = Path(get("paths", "logs")) / "current_meeting.json"
COOLDOWN = 600


def probe(token):
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            VM,
            shlex.join(guest_command("session", "--status", token)),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout)
        return value if value.get("token") == token else None
    except (ValueError, AttributeError):
        return None


def decision(meeting, remote, state, now):
    if meeting.get("dry_run"):
        return "observe"
    if remote is None:
        state.pop("dead_since", None)
        state.pop("capture_dead_since", None)
        return "unknown"
    if remote.get("requested_stop") or remote.get("phase") == "finished":
        return "finish"
    if remote.get("exit") in (2, 4, 5, 6) and remote.get("phase") != "running":
        return "terminal"
    if remote.get("free_bytes") is not None and remote["free_bytes"] < 512 * 1024**2:
        return "storage_blocked"
    if remote.get("phase") == "running":
        state.pop("dead_since", None)
        capture_failed = (
            remote.get("archive")
            and not remote.get("archive_healthy")
            and remote.get("monitor_progress_age") is not None
            and remote["monitor_progress_age"] > 30
        )
        if not capture_failed:
            state.pop("capture_dead_since", None)
            return "observe"
        state["capture_dead_since"] = state.get("capture_dead_since") or now
        if now - state["capture_dead_since"] < 90:
            return "observe"
        # Both independent audio writers have stopped, confirmed across probes.
        # A caption/ASR outage alone can never take this path.
        if now < meeting.get("deadline", meeting["started"] + 180 * 60) - 60:
            return "rejoin"
        return "exhausted"
    if (
        remote.get("phase") == "missing"
        and meeting.get("control_alive")
        and now - meeting["started"] < 900
    ):
        return "observe"  # cold VM startup/preflight is not a failed in-call bot
    state["dead_since"] = state.get("dead_since") or now
    if now - state["dead_since"] < 90:
        return "observe"
    if now >= meeting.get("deadline", meeting["started"] + 180 * 60) - 60:
        return "exhausted"
    return "rejoin"


def recovery_options(meeting, now):
    if meeting.get("args_file"):
        args = json.loads(Path(meeting["args_file"]).read_text())["argv"]
    else:
        args = shlex.split(meeting.get("args", ""))
        # Legacy metadata has a separately stored passcode; support either syntax.
        if any("REDACTED" in arg for arg in args):
            secret = Path(str(Path(get("paths", "logs")) / ".passcode")).read_text().strip()
            args = [arg.replace("REDACTED", secret) for arg in args]
    options, budget = rejoin_options(args)
    if meeting.get("active_name"):
        options["--name"] = meeting["active_name"]
    deadline = meeting.get("deadline", meeting["started"] + budget * 60)
    remaining = int((deadline - now) / 60)
    if remaining < 1:
        raise ValueError("meeting budget exhausted")
    options["--max-min"] = str(remaining)
    return [part for item in options.items() for part in item], deadline


def ensure_backend():
    from urllib.parse import urlsplit

    endpoints = [(get("caption", "stt_url"), "whisper-stt.service")]
    if get("asr", "cpu_fallback"):
        endpoints.append((get("asr", "cpu_url"), "whisper-stt-cpu.service"))
        if get("asr", "adaptive_model"):
            endpoints.append((get("asr", "small_cpu_url"), "whisper-stt-cpu-small.service"))
    for url, unit in endpoints:
        endpoint = urlsplit(url)
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port or 80), timeout=2):
                continue
        except OSError:
            # Remote speech services have their own supervisor; never start an
            # unrelated local engine because a remote endpoint is unavailable.
            if endpoint.hostname in ("localhost", "127.0.0.1", "::1"):
                result = subprocess.run(
                    ["systemctl", "--user", "start", "--no-block", unit],
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode:
                    raise RuntimeError("could not start " + unit)


def pending_alive(state, now):
    item = state.get("pending_rejoin", {})
    try:
        pid = item["pid"]
        value = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        return value == item["start_ticks"] and now - item["at"] < 300
    except (OSError, KeyError):
        return False


def rejoin(meeting, state, now):
    token = meeting["run_id"]
    current = json.loads(MJSON.read_text())
    if current.get("run_id") != token or current.get("pid") != meeting["pid"]:
        return False
    key = time.strftime("%Y%m%d") + ":" + hashlib.sha256(meeting["url"].encode()).hexdigest()
    history = state.setdefault("rejoins", {})
    if (
        history.get(key, 0) >= 3
        or now - state.get("last_rejoin", 0) < 90
        or pending_alive(state, now)
    ):
        return False
    args, deadline = recovery_options(meeting, now)
    stopped = subprocess.run(
        [
            "ssh",
            "-o",
            "ConnectTimeout=5",
            VM,
            shlex.join(guest_command("session", "--stop", token, "--reason", "recovery")),
        ],
        capture_output=True,
        text=True,
        timeout=165,
    )
    if stopped.returncode:
        return False  # control failure is not permission to kill arbitrary PIDs
    env = dict(
        os.environ, LT_WAIT_FOR_LOCK="180", LT_EXPECT_RUN_ID=token, LT_DEADLINE=str(deadline)
    )
    with open(str(Path(get("paths", "logs")) / "rejoin.log"), "ab") as log:
        proc = subprocess.Popen(
            launch_command(meeting["url"], *args),
            env=launch_environment(env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    try:
        ticks = Path(f"/proc/{proc.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except OSError:
        ticks = ""
    state.update(last_rejoin=now, pending_rejoin=dict(pid=proc.pid, start_ticks=ticks, at=now))
    history[key] = history.get(key, 0) + 1
    return True


def queue_recovery(remote, token):
    if not remote.get("archive"):
        return
    job = Path(get("paths", "logs")) / ("recovery-" + token + ".json")
    if job.exists():
        return
    wav = str(Path(remote["archive"]).with_suffix(".wav"))
    subprocess.run(
        [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            "--setenv=LIVE_TRANSCRIPT_CONFIG="
            + __import__("live_transcript.core.config", fromlist=["*"]).CONFIG["_file"],
            "--unit=live-transcript-recovery-" + token,
            "--property=Restart=on-failure",
            "--property=RestartSec=30s",
            *worker_command("recover", VM, wav, token),
        ],
        capture_output=True,
        timeout=5,
    )


def recovery_issues():
    issues = {}
    for path in sorted(Path(get("paths", "logs")).glob("postprocess-*.json"))[-20:]:
        try:
            value = json.loads(path.read_text())
            if value.get("status") in (
                "quality_failed",
                "stt_failed",
                "stt_empty_prior_retained",
                "processing_failed",
                "complete_with_warnings",
            ):
                issues["transcript-" + path.stem.removeprefix("postprocess-")] = (
                    value["status"] + "; original audio retained, inspect processing quality report"
                )
        except (OSError, ValueError):
            pass
    for path in sorted(Path(get("paths", "logs")).glob("recovery-*.json"))[-20:]:
        try:
            value = json.loads(path.read_text())
            if value.get("status") in ("postprocess_failed", "retrieval_failed"):
                issues["recovery-" + path.stem.removeprefix("recovery-")] = (
                    value["status"] + "; original evidence retained, inspect " + str(path)
                )
        except (OSError, ValueError):
            pass
    return issues


def tick(state, dry_run=False):
    now = time.time()
    issues = {}
    try:
        meeting = json.loads(MJSON.read_text())
    except (OSError, ValueError):
        return {"state-unreadable": "meeting bookkeeping unavailable; no automatic join attempted"}
    if meeting.get("status") not in ("active", "control_lost"):
        if (
            meeting.get("result")
            in ("zoom_automation_blocked", "captcha_required", "host_denied", "meeting_locked")
            and now - meeting.get("finished", 0) < 3600
        ):
            return {
                "admission-needs-operator": meeting["result"]
                + "; no automatic rejoin, inspect scoped admission diagnostics"
            }
        return issues
    try:
        ticks = Path(f"/proc/{meeting['pid']}/stat").read_text().rsplit(")", 1)[1].split()[19]
        meeting["control_alive"] = ticks == meeting.get("control_start_ticks")
    except OSError:
        meeting["control_alive"] = False
    token = meeting.get("run_id")
    if not token:
        return {
            "legacy-session": "meeting uses legacy bookkeeping; inspect manually before restarting"
        }
    if state.get("run_id") != token:
        state["run_id"] = token
        state.pop("dead_since", None)
        state.pop("pending_rejoin", None)
    try:
        remote = probe(token)
    except (OSError, subprocess.TimeoutExpired):
        remote = None
    if pending_alive(state, now):
        return issues
    if remote and remote.get("active_name"):
        name = remote["active_name"]
        if (
            isinstance(name, str)
            and name.strip()
            and len(name) <= 64
            and not any(ord(c) < 32 for c in name)
        ):
            if not dry_run and name != meeting.get("active_name"):
                update(MJSON, meeting["pid"], dict(active_name=name))
            meeting["active_name"] = name
    action = decision(meeting, remote, state, now)
    if action == "unknown":
        return {
            "control-unreachable": "VM status unavailable; recording may still be running, no rejoin attempted"
        }
    if action == "storage_blocked":
        return {
            "recording-disk-full": "VM storage is exhausted; automatic rejoin deferred to avoid repeated recording failures"
        }
    if action in ("finish", "terminal", "exhausted"):
        if not dry_run:
            update(
                MJSON,
                meeting["pid"],
                dict(
                    status="finished", finished=now, result=action, remote_exit=remote.get("exit")
                ),
            )
            # The live orchestrator already handles its own final retrieval.
            if meeting.get("status") == "control_lost":
                queue_recovery(remote, token)
        if action != "finish":
            issues["meeting-ended"] = f"meeting stopped ({action}); source evidence retained"
    elif action == "rejoin":
        if dry_run:
            issues["would-rejoin"] = "confirmed stopped meeting eligible for bounded recovery"
        elif rejoin(meeting, state, now):
            issues["rejoining"] = (
                "meeting process failed; restarting the same invitation within its original time budget"
            )
        else:
            issues["rejoin-deferred"] = (
                "recovery unavailable or retry limit reached; source evidence retained"
            )
    if remote and remote.get("phase") == "running" and remote.get("archive"):
        ui = remote.get("ui_health", {})
        if ui.get("state") == "needs_operator" and now - ui.get("utc", 0) < 60:
            issues["ui-needs-operator"] = (
                "Meeting UI recovery needs inspection: "
                + str(ui.get("reason"))
                + "; audio is left running; failure screenshots are in the session archive"
            )
        if not remote.get("archive_healthy"):
            issues["archive-degraded"] = (
                "archival audio is stalled or its supervisor is unavailable"
            )
        if max(remote.get("speaker_age", 0), remote.get("speaker_error_age", 0)) > 45:
            issues["speaker-evidence-stale"] = (
                "speaker evidence stale; audio recording is left running"
            )
        if remote.get("free_bytes") is not None and remote["free_bytes"] < 1024**3:
            issues["recording-disk-low"] = "VM has less than 1 GB free for recordings"
        if (
            remote.get("speech_age") is not None
            and remote["speech_age"] < 30
            and remote.get("captions_age", 0) > 300
        ):
            issues["captions-stale"] = (
                "speech detected but captions have not updated; source recording continues"
            )
        try:
            # The watchdog must remain responsive when a hard NFS mount stalls.
            mirror_status = (
                Path(get("paths", "safety_copies"))
                / Path(remote["archive"]).name
                / "backup-status.json"
            )
            backup = json.loads(mirror_status.read_text())
            if now - backup.get("utc", 0) > 150:
                issues["backup-stale"] = (
                    "recording backup has not reported progress for more than 150 seconds"
                )
            safety = backup
            if safety.get("status") not in (None, "ok"):
                issues["safety-copy-degraded"] = (
                    "local safety copy: " + safety["status"] + "; primary archive retained"
                )
        except (OSError, ValueError):
            if now - meeting["started"] > 120:
                issues["backup-unverified"] = (
                    "live backup status is unavailable; original VM evidence retained"
                )
    # Background jobs are observable even after the live meeting is finished.
    return issues


def deliver(state, issues, dry_run=False):
    now = time.time()
    old = state.get("issues", {})
    current = {}
    messages = []
    for key, value in issues.items():
        previous = old.get(key, {})
        if now - previous.get("at", 0) >= COOLDOWN:
            messages.append(f"Live Transcript [{key}]: {value}")
            previous = dict(at=now)
        current[key] = previous
    state["issues"] = current
    if not dry_run:
        atomic_json(STATE, state)
    if messages:
        print("\n".join(messages))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(str(STATE) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            state = json.loads(STATE.read_text())
        except (OSError, ValueError):
            state = {}
        history = state.get("rejoins", {})
        for key in list(history):
            if ":https://" in key:
                day, url = key.split(":", 1)
                hashed = day + ":" + hashlib.sha256(url.encode()).hexdigest()
                history[hashed] = max(history.get(hashed, 0), history.pop(key))
        backend_issues = {}
        if not a.dry_run:
            try:
                ensure_backend()
            except Exception as exc:
                backend_issues["speech-backend-start"] = (
                    f"{type(exc).__name__}; archival audio is independent"
                )
        try:
            issues = tick(state, a.dry_run)
        except Exception as exc:
            issues = {"watchdog-error": f"{type(exc).__name__}; no process-wide cleanup attempted"}
        issues.update(backend_issues)
        issues.update(recovery_issues())
        deliver(state, issues, a.dry_run)


if __name__ == "__main__":
    main()
