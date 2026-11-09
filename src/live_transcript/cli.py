"""One operator interface; Hermes and calendar automation are optional clients."""

from live_transcript.worker import command as worker_command
from live_transcript.worker import guest_command

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import urllib.request


def emit(value, as_json=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, dict) and "checks" in value:
        if "recording_ready" in value:
            print(
                "Recording: "
                + ("ready" if value["recording_ready"] else "not ready")
                + "; live transcription: "
                + (
                    "ready (fallback)"
                    if value.get("transcription_degraded")
                    else "ready"
                    if value["transcription_ready"]
                    else "not ready"
                )
            )
        for item in value["checks"]:
            print(f"{item['status'].upper():5} {item['name']}: {item['detail']}")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def current():
    from live_transcript.runtime.state import current as read

    return read()


def runtime_info():
    from importlib.metadata import version, PackageNotFoundError
    from live_transcript.core.config import CONFIG, get

    try:
        installed_version = version("live-transcription-bot")
    except PackageNotFoundError:
        installed_version = "source checkout"
    revision = None
    source = Path(__file__).resolve().parents[2]
    if (source / "PUBLIC_SOURCE_FILES.txt").is_file() and (source / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if isinstance(result.stdout, str) and re.fullmatch(r"[a-f0-9]{40}\s*", result.stdout):
                revision = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return dict(
        version=installed_version,
        source_revision=revision,
        package=str(Path(__file__).resolve().parent),
        python=sys.executable,
        config=CONFIG["_file"],
        data_root=get("paths", "root"),
        vm_name=get("vm", "name"),
    )


def remote_status(meeting):
    from live_transcript.core.config import get, ssh_options

    token = meeting.get("run_id")
    if not token:
        return None
    # Validate without creating host-side guest directories.
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", token):
        raise ValueError("invalid session token")
    command = shlex.join(guest_command("session", "--status", token))
    result = subprocess.run(
        [
            "ssh",
            *ssh_options(),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            get("vm", "ssh"),
            command,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        return dict(phase="unknown", reason="VM control unreachable; capture may still be running")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("VM replied with an invalid session status")
    if value.get("token") != token:
        raise ValueError("VM replied with a different session token")
    return value


def check_readiness(*, recording_only=False):
    """Read-only preflight. Never load another model or touch archive storage."""
    from live_transcript.core.config import get, ssh_options

    checks = []

    def add(name, status, detail):
        checks.append(dict(name=name, status=status, detail=detail))

    tools = ["ssh", "rsync", "ffmpeg", "nc", "flock"]
    if get("runtime", "supervisor") == "systemd":
        tools += ["systemctl", "systemd-run"]
    else:
        from live_transcript.runtime.application import ready

        add("application supervisor", "pass" if ready() else "fail", "durable background worker")
    for name in tools:
        add(
            name,
            "pass" if shutil.which(name) else "fail",
            shutil.which(name) or "missing executable",
        )
    for name in () if recording_only else ("model", "vad", "whisper_cli"):
        path = Path(get("paths", name))
        good = path.is_file() and path.stat().st_size > 0
        add(name, "pass" if good else "fail", str(path))
    # A new installation may not have created its data workspace yet. Check
    # its existing ancestor without creating state during a read-only preflight.
    workspace = Path(get("paths", "root"))
    while not workspace.exists() and workspace.parent != workspace:
        workspace = workspace.parent
    free = shutil.disk_usage(workspace).free
    add(
        "host disk reserve",
        "pass" if free > get("storage", "host_free_reserve_bytes") else "fail",
        f"{free / 1024**3:.1f} GiB free",
    )
    for name, endpoint in [("caption daemon", (get("caption", "host"), get("caption", "port")))]:
        try:
            with socket.create_connection(endpoint, timeout=3) as sock:
                sock.sendall(b"GETGPU\n")
                with sock.makefile("rb") as reader:
                    payload = json.loads(reader.readline(65536))
                if not isinstance(payload, dict) or "util" not in payload:
                    raise ValueError("unexpected protocol reply")
            add(name, "pass", "protocol replies")
        except Exception as exc:
            add(name, "warn", "live captions unavailable: " + type(exc).__name__)
    from live_transcript.transcription.service import readiness

    labels = {
        "primary": "speech backend",
        "cpu-large": "CPU large standby",
        "cpu-small": "CPU small standby",
    }
    speech_backends = readiness()
    for role, result in speech_backends.items():
        add(labels[role], "pass" if result["ready"] else "warn", result["detail"])
        if role == "primary" and not result["verified"]:
            add(
                "speech device verification",
                "warn",
                "Readiness does not establish the configured model/device",
            )
    if not get("model", "enabled"):
        add("local model", "info", "disabled in this profile")
    else:
        models = get("model", "url").removesuffix("/chat/completions") + "/models"
        try:
            with urllib.request.urlopen(models, timeout=3) as response:
                payload = json.loads(response.read(65536))
            add(
                "local model",
                "pass" if payload.get("data") else "warn",
                "summaries and recovery planner",
            )
        except Exception as exc:
            add("local model", "warn", "optional service unavailable: " + type(exc).__name__)
    if not get("vm", "ssh"):
        add("VM", "fail", "configure vm.ssh")
    else:
        code = (
            "import importlib.util,json,pathlib,shutil,subprocess; "
            "r=subprocess.run(['pactl','list','short','sinks'],capture_output=True,text=True,timeout=3); "
            "sinks=[line.split()[1] for line in r.stdout.splitlines() if len(line.split())>1]; "
            "print(json.dumps(dict("
            "playwright=importlib.util.find_spec('playwright') is not None,"
            "xvfb=shutil.which('xvfb-run') is not None,ffmpeg=shutil.which('ffmpeg') is not None,"
            "camera=pathlib.Path('/dev/video0').exists(),native_input=shutil.which('xdotool') is not None and shutil.which('xclip') is not None,"
            "audio=" + repr(get("audio", "sink")) + " in sinks,"
            "free=shutil.disk_usage('.').free)))"
        )
        try:
            result = subprocess.run(
                [
                    "ssh",
                    *ssh_options(),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    get("vm", "ssh"),
                    shlex.join([get("vm", "python"), "-c", code]),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            data = json.loads(result.stdout)
            good = (
                all(data.get(k) for k in ("playwright", "xvfb", "ffmpeg", "audio"))
                and data["free"] > 1024**3
            )
            add("VM runtime", "pass" if good else "fail", json.dumps(data))
            add(
                "caption camera",
                "pass" if data.get("camera") else "warn",
                "device available"
                if data.get("camera")
                else "optional caption camera is unavailable; source audio capture is independent",
            )
            if get("google", "input_mode") == "native":
                add(
                    "Google native input",
                    "pass" if data.get("native_input") else "warn",
                    "xdotool and xclip available"
                    if data.get("native_input")
                    else "install xdotool and xclip in the VM for Google joins",
                )
        except Exception as exc:
            add("VM runtime", "fail", type(exc).__name__ + "; capture readiness unverified")
    add(
        "archive storage",
        "info",
        "not probed on preflight; asynchronous backup has its own health state",
    )
    meeting = current()
    if meeting.get("status") in ("active", "control_lost"):
        remote = remote_status(meeting)
        add(
            "active recording",
            "pass" if remote and remote.get("archive_healthy") else "warn",
            "archive healthy"
            if remote and remote.get("archive_healthy")
            else "capture health unverified; inspect status",
        )
    capture_checks = {
        "ssh",
        "rsync",
        "ffmpeg",
        "nc",
        "systemctl",
        "systemd-run",
        "flock",
        "host disk reserve",
        "VM",
        "VM runtime",
    }
    recording_ready = not any(c["status"] == "fail" for c in checks if c["name"] in capture_checks)
    caption_ready = any(c["name"] == "caption daemon" and c["status"] == "pass" for c in checks)
    speech_ready = any(
        c["name"] in ("speech backend", "CPU large standby", "CPU small standby")
        and c["status"] == "pass"
        for c in checks
    )
    transcription_ready = recording_ready and caption_ready and speech_ready
    return dict(
        ok=not any(c["status"] == "fail" for c in checks)
        and (recording_only or transcription_ready),
        recording_ready=recording_ready,
        transcription_ready=transcription_ready,
        transcription_degraded=transcription_ready
        and not speech_backends.get("primary", {}).get("ready", False),
        speech_backends=speech_backends,
        checks=checks,
        mode="recording_only" if recording_only else "transcription",
        runtime=runtime_info(),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="TOML configuration file")
    parser.add_argument(
        "--json", action="store_true", help="structured output for status/check/config/verify"
    )
    parser.add_argument("--version", action="version", version="live-transcript 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)
    from live_transcript.runtime.installation import setup_options

    setup_options(
        commands.add_parser(
            "setup", help="provision a standalone installation and isolated browser VM"
        )
    )
    from live_transcript.runtime.containers import export_options, application_options

    export_options(
        commands.add_parser("backend", help="export a portable Compose caption/speech backend")
    )
    application_options(
        commands.add_parser(
            "deployment", help="export the container application with an isolated browser VM"
        )
    )
    stack = commands.add_parser("stack", help="manage an owned Compose application release")
    stack.add_argument("directory")
    stack.add_argument(
        "action",
        choices=(
            "hold-lock",
            "status",
            "capacity",
            "guard-stop",
            "url",
            "prepare-update",
            "commit-update",
            "abort-update",
            "rollback",
        ),
    )
    stack.add_argument("--running", nargs="*", default=[])
    stack.add_argument("--stop-guard", action="store_true", help=argparse.SUPPRESS)
    for name in ("check", "status", "config", "info"):
        command = commands.add_parser(name)
        if name == "check":
            command.add_argument(
                "--recording-only",
                action="store_true",
                help="check capture readiness without requiring speech models",
            )
    notifications = commands.add_parser(
        "notifications", help="read pending alerts or acknowledge delivered IDs"
    )
    notifications.add_argument("--limit", type=int, default=100)
    notifications.add_argument("--ack", nargs="+", metavar="ID")
    start = commands.add_parser(
        "start", help="join and record; SSH disconnect does not stop VM capture"
    )
    start.add_argument("meeting")
    start.add_argument("--max-min", type=int, default=180)
    start.add_argument("--name", help="transparent participant name; defaults to identity.name")
    start.add_argument("--passcode")
    start.add_argument("--dry", action="store_true")
    stop = commands.add_parser("stop", help="gracefully stop the exact current session")
    stop.add_argument("--run-id", help="refuse to stop a different meeting")
    workspace = commands.add_parser(
        "workspace", help="persistent meeting controls and recording library"
    )
    workspace.add_argument("--listen", default="127.0.0.1")
    workspace.add_argument("--port", type=int, default=6080)
    workspace.add_argument("--advertise-host")
    workspace.add_argument(
        "--public-url", help="external HTTP(S) origin when using a reverse proxy"
    )
    workspace.add_argument("--view-only", action="store_true")
    workspace.add_argument("--health", action="store_true")
    capacity = commands.add_parser("capacity", help="read-only recording headroom estimates")
    capacity.add_argument("--max-min", type=int, default=0)
    recordings = commands.add_parser("recordings", help="browse retained recordings")
    recordings.add_argument("--refresh", action="store_true")
    console = commands.add_parser(
        "console", help="view the VM browser through a private local noVNC tunnel"
    )
    console.add_argument(
        "--control",
        action="store_true",
        help="enable keyboard/mouse and pause automated UI recovery",
    )
    console.add_argument("--display", help="explicit VM X display when multiple are active")
    console.add_argument(
        "--idle",
        action="store_true",
        help="open a temporary diagnostic desktop if no meeting display exists",
    )
    console.add_argument(
        "--port", type=int, default=0, help="host loopback port; default chooses a free port"
    )
    console.add_argument("--max-min", type=int, default=30)
    console.add_argument(
        "--listen",
        default="127.0.0.1",
        help="host listen address; use 0.0.0.0 explicitly for VPN/LAN access",
    )
    console.add_argument("--advertise-host", help="VPN/LAN address to put in the viewer link")
    hub = commands.add_parser("console-hub", help="view several existing VM consoles in one UI")
    hub.add_argument(
        "--console",
        action="append",
        required=True,
        help="named local console, e.g. main=6081; repeat for each VM",
    )
    hub.add_argument("--listen", default="127.0.0.1")
    hub.add_argument("--port", type=int, default=6080)
    hub.add_argument("--advertise-host", help="same VPN/LAN host used by the existing consoles")
    say = commands.add_parser("say", help="explicit TTS request, never automatic")
    say.add_argument("text")
    say.add_argument("--lang", choices=("en", "de"), default="en")
    verify = commands.add_parser("verify", help="verify a finalized archive inventory")
    verify.add_argument("archive")
    verify.add_argument("--evidence-only", action="store_true")
    recover = commands.add_parser(
        "recover", help="retry retrieval and postprocessing for a session"
    )
    recover.add_argument("token")
    recover.add_argument("guest_wav")
    watchdog = commands.add_parser("watchdog", help="one bounded supervision tick")
    watchdog.add_argument("--dry-run", action="store_true")
    watchdog.add_argument("--ack-after-print", action="store_true")
    calendar = commands.add_parser(
        "calendar", help="reconcile optional ICS calendar and exact launch timers"
    )
    calendar.add_argument("--dry-run", action="store_true")
    commands.add_parser("jobs", help="show recent recording retrieval and processing jobs")
    process = commands.add_parser(
        "process", help="transcribe and summarize an existing 16 kHz mono WAV"
    )
    process.add_argument("wav")
    serve = commands.add_parser("serve", help="run the caption daemon or a local speech service")
    serve.add_argument("service", choices=("captions", "asr", "app"))
    serve.add_argument("arguments", nargs=argparse.REMAINDER)
    bundle = commands.add_parser("bundle", help="build the self-contained browser-guest program")
    bundle.add_argument("output")
    lab = commands.add_parser("lab", help="isolated Jitsi tests and accuracy regression reports")
    actions = lab.add_subparsers(dest="lab_action", required=True)
    jitsi_lab = actions.add_parser("jitsi", help="borrow an idle VM for an owned test room")
    jitsi_lab.add_argument(
        "--server", required=True, help="allowlisted self-owned HTTPS Jitsi origin"
    )
    jitsi_lab.add_argument("--output", required=True, help="new private results directory")
    jitsi_lab.add_argument(
        "--scenario", choices=("clean", "quiet", "noise", "overlap", "all"), default="clean"
    )
    jitsi_lab.add_argument("--endpoint", help="optional live inference endpoint override")
    jitsi_lab.add_argument(
        "--asr-mode",
        choices=("existing", "gpu", "cpu"),
        default="existing",
        help="reuse endpoints or run an owned full-model inference server",
    )
    jitsi_lab.add_argument("--plan", help="optional reference JSON and local PCM WAV phrases")
    jitsi_lab.add_argument("--baseline", help="previous report.json using the same reference")
    jitsi_lab.add_argument(
        "--silence-finalize", type=float, help="lab-only silence boundary in seconds (0.5–10)"
    )
    jitsi_lab.add_argument(
        "--no-final", action="store_true", help="skip independent full-model final recognition"
    )
    collect_lab = actions.add_parser(
        "collect", help="retrieve an existing run without joining again"
    )
    collect_lab.add_argument("directory", help="scenario directory containing receipt.json")
    score_lab = actions.add_parser("report", help="score retained reference and transcript JSON")
    score_lab.add_argument("--reference", required=True)
    score_lab.add_argument("--transcript", required=True)
    score_lab.add_argument("--output", required=True)
    score_lab.add_argument("--baseline")
    args = parser.parse_args(argv)
    if args.command == "setup":
        from live_transcript.runtime.installation import run as install

        try:
            emit(install(args, args.config), args.json)
            return 0
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            emit(dict(ok=False, error=str(exc)), args.json)
            return 1
    if args.config:
        os.environ["LIVE_TRANSCRIPT_CONFIG"] = str(Path(args.config).expanduser().absolute())
        if not Path(os.environ["LIVE_TRANSCRIPT_CONFIG"]).is_file():
            parser.error("configuration file does not exist")
    try:
        import live_transcript.core.config as runtime_config

        # Supports embedded callers invoking main() with another configuration.
        runtime_config.CONFIG = runtime_config.load()
        from live_transcript.core.config import get, ssh_options

        if args.command == "lab":
            from live_transcript.runtime.lab import run

            report = run(args)
            emit(report, args.json)
            return 0 if report.get("ok", report.get("comparison", {}).get("ok", True)) else 1
        elif args.command == "stack":
            from live_transcript.runtime.stack import run

            emit(run(args), args.json)
        elif args.command == "deployment":
            from live_transcript.runtime.containers import export_application

            emit(export_application(args), args.json)
        elif args.command == "backend":
            from live_transcript.runtime.containers import export_backend

            emit(export_backend(args), args.json)
        elif args.command == "workspace":
            from live_transcript.runtime.workspace import run

            return run(args, lambda value: print(json.dumps(value), flush=True))
        elif args.command == "capacity":
            from live_transcript.runtime.capacity import report, guest_free_bytes

            try:
                free = guest_free_bytes()
            except (OSError, ValueError, subprocess.SubprocessError):
                free = None
            emit(report(args.max_min, guest_free=free), args.json)
        elif args.command == "recordings":
            from live_transcript.runtime.library import scan, listing

            if args.refresh:
                scan()
            emit(listing(), args.json)
        elif args.command == "console":
            if get("runtime", "supervisor") == "app" and not os.environ.get("LT_APPLICATION_JOB"):
                from live_transcript.runtime.application import submit, follow

                values = []
                for flag in ("control", "idle"):
                    if getattr(args, flag):
                        values.append("--" + flag)
                for flag in ("display", "port", "max_min", "listen", "advertise_host"):
                    value = getattr(args, flag)
                    if value is not None:
                        values += ["--" + flag.replace("_", "-"), str(value)]
                return follow(submit("console", values))
            from live_transcript.runtime.console import open_console

            def console_emit(value):
                if args.json:
                    print(json.dumps(value, ensure_ascii=False), flush=True)
                else:
                    emit(value)

            console_emit(open_console(args, console_emit))
        elif args.command == "console-hub":
            from live_transcript.runtime.operator import run_hub

            run_hub(args, lambda value: print(json.dumps(value), flush=True))
        elif args.command == "config":
            emit(runtime_config.CONFIG, args.json)
        elif args.command == "info":
            emit(runtime_info(), args.json)
        elif args.command == "notifications":
            from live_transcript.integrations.notifications import pending, acknowledge

            emit(acknowledge(args.ack) if args.ack else pending(args.limit), args.json)
        elif args.command == "check":
            result = (
                check_readiness(recording_only=True) if args.recording_only else check_readiness()
            )
            emit(result, args.json)
            return 0 if result["ok"] else 1
        elif args.command == "status":
            meeting = current()
            emit(
                dict(
                    status=meeting.get("status"),
                    run_id=meeting.get("run_id"),
                    started=meeting.get("started"),
                    deadline=meeting.get("deadline"),
                    finished=meeting.get("finished"),
                    result=meeting.get("result"),
                    remote_exit=meeting.get("remote_exit"),
                    admission_diagnostics=meeting.get("admission_diagnostics"),
                    active_name=meeting.get("active_name"),
                    remote=remote_status(meeting)
                    if meeting.get("status") in ("active", "control_lost")
                    else None,
                ),
                args.json,
            )
        elif args.command == "verify":
            from live_transcript.storage.mirror import finalized
            from live_transcript.capture.guard import verify_inventory

            if not finalized(args.archive):
                raise ValueError("archive is not finalized; evidence may still be recording")
            emit(
                dict(ok=True, files_verified=verify_inventory(args.archive, args.evidence_only)),
                args.json,
            )
        elif args.command == "start":
            name = args.name if args.name is not None else get("identity", "name")
            options = [args.meeting, "--max-min", str(args.max_min), "--name", name]
            if args.passcode:
                options += ["--passcode", args.passcode]
            if args.dry:
                options += ["--dry"]
            from live_transcript.runtime.launcher import start

            return start(*options)
        elif args.command == "stop":
            meeting = current()
            if args.run_id and args.run_id != meeting.get("run_id"):
                raise ValueError("current meeting changed; refusing stale stop")
            if meeting.get("status") not in ("active", "control_lost") or not meeting.get("run_id"):
                raise ValueError("no current meeting to stop")
            import re

            if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", meeting["run_id"]):
                raise ValueError("invalid session token")
            command = shlex.join(guest_command("session", "--stop", meeting["run_id"]))
            return subprocess.run(
                [
                    "ssh",
                    *ssh_options(),
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "ServerAliveInterval=5",
                    "-o",
                    "ServerAliveCountMax=2",
                    get("vm", "ssh"),
                    command,
                ],
                timeout=180,
            ).returncode
        elif args.command == "say":
            return subprocess.call(worker_command("tts", "--lang", args.lang, args.text))
        elif args.command == "recover":
            if get("runtime", "supervisor") == "app" and not os.environ.get("LT_APPLICATION_JOB"):
                from live_transcript.runtime.application import submit, follow

                return follow(submit("recover", [get("vm", "ssh"), args.guest_wav, args.token]))
            from live_transcript.storage.worker import run

            result = run(get("vm", "ssh"), args.guest_wav, args.token)
            emit(result, args.json)
            return 1 if result.get("status") in ("retrieval_failed", "postprocess_failed") else 0
        elif args.command == "jobs":
            from live_transcript.storage.worker import recent_jobs

            result = {"jobs": recent_jobs()}
            if get("runtime", "supervisor") == "app":
                from live_transcript.runtime.application import Queue

                queue = Queue()
                try:
                    result["application_jobs"] = [
                        {key: row[key] for key in ("id", "status", "due")}
                        for row in queue.db.execute(
                            "SELECT id,status,due FROM jobs ORDER BY rowid DESC LIMIT 50"
                        )
                    ]
                finally:
                    queue.close()
            emit(result, args.json)
        elif args.command == "process":
            if get("runtime", "supervisor") == "app" and not os.environ.get("LT_APPLICATION_JOB"):
                from live_transcript.runtime.application import submit, follow

                return follow(submit("process", [args.wav]))
            from live_transcript.runtime.postprocess import run_logged

            run_logged([args.wav])
        elif args.command == "serve":
            from live_transcript.worker import main as run_service

            return run_service([args.service, *args.arguments])
        elif args.command == "bundle":
            from live_transcript.runtime.deployment import build_bundle

            members = build_bundle(args.output)
            emit({"bundle": args.output, "files": len(members)}, args.json)
        elif args.command == "watchdog":
            from live_transcript.runtime.watchdog import main as supervise

            supervise(
                (["--dry-run"] if args.dry_run else [])
                + (["--json"] if args.json else [])
                + (["--ack-after-print"] if args.ack_after_print else [])
            )
        elif args.command == "calendar":
            from live_transcript.integrations.calendar import main as reconcile

            reconcile(["--dry-run"] if args.dry_run else [])
        return 0
    except KeyboardInterrupt:
        emit(
            dict(
                ok=False,
                detail="Control interrupted; capture may still be running. Use status or stop.",
            ),
            args.json,
        )
        return 130
    except (
        ValueError,
        OSError,
        KeyError,
        ImportError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        emit(dict(ok=False, error=type(exc).__name__, detail=str(exc)[:400]), args.json)
        return 1
