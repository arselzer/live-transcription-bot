#!/usr/bin/env python3
"""Run the browser meeting independently of the SSH log-following connection."""

from live_transcript.worker import command as worker_command, is_role_process

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import hashlib
import signal
import shutil

from live_transcript.capture.guard import atomic_json
from live_transcript.core.config import get
from live_transcript.core.options import rejoin_options

HOME = Path(get("vm", "home"))


def process_args(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return []


def assert_idle():
    """Refuse VM preparation when a detached meeting still owns the browser."""
    root = HOME / "logs"
    root.mkdir(exist_ok=True)
    with open(root / "meeting-owner.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "A meeting already owns this VM; reconnect or stop that session first"
            )
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=active,activating,deactivating",
                "--no-legend",
                "live-transcript-meeting-*",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        if result.stdout.strip():
            raise RuntimeError(
                "A detached meeting service is still active; refusing to reset audio/browser"
            )
        for proc in Path("/proc").iterdir():
            if proc.name.isdigit() and is_role_process(process_args(proc.name), {"join"}):
                raise RuntimeError("A meeting worker is still active")


def status(token):
    prefix = paths(token)
    now = time.time()
    stop_file = prefix.with_suffix(".stop.json")
    stop_reason = (
        json.loads(stop_file.read_text()).get("reason", "user") if stop_file.exists() else None
    )
    result = dict(
        utc=now,
        token=token,
        phase="missing",
        joiner_alive=False,
        requested_stop=stop_reason is not None and stop_reason != "recovery",
        free_bytes=shutil.disk_usage(HOME).free,
    )
    done = prefix.with_suffix(".done.json")
    if done.exists():
        completion = json.loads(done.read_text())
        result.update(
            phase="finished" if completion["code"] == 0 else "failed", exit=completion["code"]
        )
    else:
        state = subprocess.run(
            ["systemctl", "--user", "is-active", "live-transcript-meeting-" + token],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if state in ("active", "activating", "deactivating"):
            result["phase"] = "running"
            main = subprocess.check_output(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "live-transcript-meeting-" + token,
                    "-p",
                    "MainPID",
                    "--value",
                ],
                text=True,
                timeout=5,
            ).strip()
            result["joiner_alive"] = main != "0"
    try:
        text = prefix.with_suffix(".log").read_text(errors="replace")
        names = re.findall(r"^ADMISSION_SUCCEEDED (.+)$", text, re.M)
        if names:
            try:
                result["active_name"] = json.loads(names[-1])["name"]
            except (ValueError, KeyError):
                pass
        archives = re.findall(r"^ARCHIVE to (\S+)", text, re.M)
        if archives:
            root = Path(archives[-1])
            result["archive"] = str(root)
            health = json.loads((root / "health.json").read_text())
            audio = health.get("audio", {})
            result["archive_healthy"] = (
                now - health.get("utc", 0) < 15
                and audio.get("running", False)
                and audio.get("progress_age", 99) < 20
            )
            result["archive_state"] = health.get("state", "recording")
            try:
                ui = json.loads((root / "ui-health.json").read_text())
                result["ui_health"] = ui
            except (OSError, ValueError):
                pass
            monitors = [root.with_suffix(".wav"), *root.glob("fallback-*.wav")]
            ages = [now - path.stat().st_mtime for path in monitors if path.exists()]
            result["monitor_progress_age"] = min(ages) if ages else None
            result["free_bytes"] = health.get("free_bytes")
            result["speaker_age"] = now - (root / "speakers.jsonl").stat().st_mtime
            try:
                observer = json.loads((root / "observer-health.json").read_text())
                result["speaker_error_age"] = (
                    now - observer["last_success"] if observer.get("error") else 0
                )
            except (OSError, ValueError, KeyError):
                pass
            result["sealed"] = (root / "finalized.json").exists()
    except (OSError, ValueError):
        pass
    for key, file in [("captions_age", HOME / "captions.json"), ("gpu_age", HOME / "gpu.json")]:
        try:
            result[key] = now - file.stat().st_mtime
        except OSError:
            pass
    try:
        gpu = json.loads((HOME / "gpu.json").read_text())
        result["speech_age"] = gpu.get("last_speech_age")
    except (OSError, ValueError):
        pass
    return result


def paths(token):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", token):
        raise ValueError("invalid session token")
    root = HOME / "logs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / ("session-" + token)


def worker(token, args):
    prefix = paths(token)
    code = 1
    lock = open(prefix.parent / "meeting-owner.lock", "a")
    proc = None
    stopping = False

    def graceful_child_stop():
        role = os.environ.get("LT_TEST_ROLE") or "join"
        targets = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            child_args = process_args(entry.name)
            try:
                if (
                    child_args
                    and child_args[0].rsplit(b"/", 1)[-1].startswith(b"python")
                    and is_role_process(child_args, {role})
                    and os.getpgid(int(entry.name)) == proc.pid
                ):
                    targets.append(int(entry.name))
            except ProcessLookupError:
                pass
        if targets:
            for child in targets:
                try:
                    os.kill(child, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def stop(*_):
        nonlocal code, stopping
        if stopping:
            return
        stopping = True
        code = 0 if prefix.with_suffix(".stop.json").exists() else 143
        if proc and proc.poll() is None:
            graceful_child_stop()
        raise SystemExit(code)

    old_handlers = {}
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, stop)
        test_role = os.environ.get("LT_TEST_ROLE")
        if test_role and test_role not in ("fixture-session", "fixture-stop"):
            raise ValueError("Unsupported fixture role")
        if not test_role:
            from live_transcript.runtime.prepare import main as prepare_vm

            prepare_vm(lease_owned=True)
        try:
            _, minutes = rejoin_options(args[1:]) if args else ({}, 180)
        except ValueError:
            minutes = 180
        proc = subprocess.Popen(
            [
                "xvfb-run",
                "-a",
                "-s",
                "-screen 0 1280x800x24",
                *worker_command(test_role or "join"),
                *args,
            ],
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=minutes * 60 + 900)
        except subprocess.TimeoutExpired:
            graceful_child_stop()
            code = 124
        return code
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        lock.close()
        atomic_json(prefix.with_suffix(".done.json"), dict(utc=time.time(), code=code))


def follow(token, args):
    prefix = paths(token)
    log = prefix.with_suffix(".log")
    done = prefix.with_suffix(".done.json")
    unit = "live-transcript-meeting-" + token
    identity = hashlib.sha256(json.dumps(args).encode()).hexdigest()
    with open(prefix.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started_file = prefix.with_suffix(".started.json")
        if started_file.exists():
            started = json.loads(started_file.read_text())
            if started.get("identity", identity) != identity:
                raise RuntimeError("Session token already belongs to a different invocation")
        else:
            state = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            if state in ("active", "activating", "deactivating"):
                atomic_json(started_file, dict(utc=time.time(), unit=unit, identity=identity))
            else:
                assert_idle()
        if not started_file.exists():
            _, minutes = rejoin_options(args[1:]) if args else ({}, 180)
            cmd = [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                "--unit=" + unit,
                "--property=RuntimeMaxSec=" + str(minutes * 60 + 1200),
                "--property=TimeoutStopSec=150s",
                "--property=KillSignal=SIGTERM",
                "--property=KillMode=mixed",
                "--property=StandardOutput=append:" + str(log),
                "--property=StandardError=append:" + str(log),
            ]
            for key in (
                "PULSE_SERVER",
                "XDG_RUNTIME_DIR",
                "LT_TEST_ROLE",
                "LIVE_TRANSCRIPT_CONFIG",
                "LT_DEADLINE",
            ):
                if key in os.environ:
                    cmd.append(f"--setenv={key}={os.environ[key]}")
            cmd += worker_command("session", "--worker", token, *args)
            subprocess.run(cmd, check=True, capture_output=True, timeout=10)
            atomic_json(started_file, dict(utc=time.time(), unit=unit, identity=identity))
    offset = 0
    while True:
        if log.exists():
            with open(log, errors="replace") as fh:
                fh.seek(offset)
                data = fh.read()
                offset = fh.tell()
                if data:
                    print(data, end="", flush=True)
        if done.exists():
            # Drain the last lines written just before the completion marker.
            with open(log) as fh:
                fh.seek(offset)
                print(fh.read(), end="", flush=True)
            return json.loads(done.read_text())["code"]
        state = subprocess.run(
            ["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if state not in ("active", "activating", "deactivating"):
            raise RuntimeError("Meeting service stopped unexpectedly; saved log: " + str(log))
        time.sleep(0.5)


def main():
    os.umask(0o077)
    if sys.argv[1:2] == ["--worker"]:
        return worker(sys.argv[2], sys.argv[3:])
    if sys.argv[1:2] == ["--assert-idle"]:
        assert_idle()
        return 0
    if sys.argv[1:2] == ["--status"]:
        print(json.dumps(status(sys.argv[2])))
        return 0
    if sys.argv[1:2] == ["--stop-current"]:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=active,activating,deactivating",
                "--no-legend",
                "live-transcript-meeting-*",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        units = [line.split()[0] for line in result.stdout.splitlines()]
        if not units:
            print("No supervised meeting is active")
            return 0
        if len(units) != 1:
            raise RuntimeError("Ambiguous active sessions; stop the explicit session token")
        token = units[0].removeprefix("live-transcript-meeting-").removesuffix(".service")
        sys.argv[1:] = ["--stop", token]
    if sys.argv[1:2] == ["--stop"]:
        prefix = paths(sys.argv[2])
        reason = sys.argv[4] if sys.argv[3:4] == ["--reason"] else "user"
        atomic_json(prefix.with_suffix(".stop.json"), dict(utc=time.time(), reason=reason))
        result = subprocess.run(
            ["systemctl", "--user", "stop", "live-transcript-meeting-" + sys.argv[2]],
            capture_output=True,
            timeout=155,
        )
        if result.returncode:
            # Collected/never-started units are already stopped. Refuse to
            # assume this if some other session holds the VM lease.
            assert_idle()
        return 0
    return follow(os.environ["LT_RUN_ID"], sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
