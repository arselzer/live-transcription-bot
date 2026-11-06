"""One operator interface; Hermes and calendar automation are optional clients."""

from live_transcript.worker import command as worker_command
from live_transcript.worker import guest_command

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import urllib.request


def emit(value, as_json=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, dict) and "checks" in value:
        for item in value["checks"]:
            print(f"{item['status'].upper():5} {item['name']}: {item['detail']}")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def current():
    from live_transcript.core.config import get

    path = Path(get("paths", "logs")) / "current_meeting.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return dict(status="idle")


def remote_status(meeting):
    from live_transcript.core.config import get

    token = meeting.get("run_id")
    if not token:
        return None
    # Validate without creating host-side guest directories.
    import re

    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", token):
        raise ValueError("invalid session token")
    command = shlex.join(guest_command("session", "--status", token))
    result = subprocess.run(
        [
            "ssh",
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
    if value.get("token") != token:
        raise ValueError("VM replied with a different session token")
    return value


def doctor():
    """Read-only preflight. Never load another model or touch the archive NAS."""
    from live_transcript.core.config import get

    checks = []

    def add(name, status, detail):
        checks.append(dict(name=name, status=status, detail=detail))

    for name in ("ssh", "rsync", "ffmpeg", "nc", "systemctl", "systemd-run", "flock"):
        add(
            name,
            "pass" if shutil.which(name) else "fail",
            shutil.which(name) or "missing executable",
        )
    for name in ("model", "vad", "whisper_cli"):
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
    from urllib.parse import urlsplit

    endpoint = urlsplit(get("caption", "stt_url"))
    try:
        with socket.create_connection(
            (endpoint.hostname, endpoint.port or (443 if endpoint.scheme == "https" else 80)),
            timeout=3,
        ):
            pass
        add("speech backend", "pass", "reachable; no extra model was loaded")
    except OSError:
        add("speech backend", "warn", "unreachable; independent recording can continue")
    if get("asr", "cpu_fallback"):
        for name, url in [
            ("CPU large standby", get("asr", "cpu_url")),
            ("CPU small standby", get("asr", "small_cpu_url")),
        ]:
            if name == "CPU small standby" and not get("asr", "adaptive_model"):
                continue
            endpoint = urlsplit(url)
            try:
                health_url = url.rsplit("/", 1)[0] + "/health"
                with urllib.request.urlopen(health_url, timeout=3) as response:
                    data = json.loads(response.read(8192))
                add(name, "pass" if data.get("status") == "ok" else "warn", json.dumps(data))
            except Exception as exc:
                add(name, "warn", "fallback readiness unverified: " + type(exc).__name__)
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
            "import importlib.util,json,pathlib,shutil; print(json.dumps(dict("
            "playwright=importlib.util.find_spec('playwright') is not None,"
            "xvfb=shutil.which('xvfb-run') is not None,ffmpeg=shutil.which('ffmpeg') is not None,"
            "camera=pathlib.Path('/dev/video0').exists(),native_input=shutil.which('xdotool') is not None and shutil.which('xclip') is not None,"
            "free=shutil.disk_usage('.').free)))"
        )
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    get("vm", "ssh"),
                    "python3 -c " + shlex.quote(code),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            data = json.loads(result.stdout)
            good = (
                all(data.get(k) for k in ("playwright", "xvfb", "ffmpeg", "camera"))
                and data["free"] > 1024**3
            )
            add("VM runtime", "pass" if good else "fail", json.dumps(data))
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
    return dict(ok=not any(c["status"] == "fail" for c in checks), checks=checks)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="TOML configuration file")
    parser.add_argument(
        "--json", action="store_true", help="structured output for status/doctor/config/verify"
    )
    parser.add_argument("--version", action="version", version="live-transcript 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "config"):
        commands.add_parser(name)
    start = commands.add_parser(
        "start", help="join and record; SSH disconnect does not stop VM capture"
    )
    start.add_argument("meeting")
    start.add_argument("--max-min", type=int, default=180)
    start.add_argument("--name", help="transparent participant name; defaults to identity.name")
    start.add_argument("--passcode")
    start.add_argument("--dry", action="store_true")
    commands.add_parser("stop", help="gracefully stop the exact current session")
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
    serve.add_argument("service", choices=("captions", "asr"))
    serve.add_argument("arguments", nargs=argparse.REMAINDER)
    bundle = commands.add_parser("bundle", help="build the self-contained browser-guest program")
    bundle.add_argument("output")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["LIVE_TRANSCRIPT_CONFIG"] = str(Path(args.config).expanduser().absolute())
        if not Path(os.environ["LIVE_TRANSCRIPT_CONFIG"]).is_file():
            parser.error("configuration file does not exist")
    try:
        import live_transcript.core.config as runtime_config

        # Supports embedded callers invoking main() with another configuration.
        runtime_config.CONFIG = runtime_config.load()
        from live_transcript.core.config import get

        if args.command == "config":
            emit(runtime_config.CONFIG, args.json)
        elif args.command == "doctor":
            result = doctor()
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
            if meeting.get("status") not in ("active", "control_lost") or not meeting.get("run_id"):
                raise ValueError("no current meeting to stop")
            import re

            if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", meeting["run_id"]):
                raise ValueError("invalid session token")
            command = shlex.join(guest_command("session", "--stop", meeting["run_id"]))
            return subprocess.run(
                [
                    "ssh",
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
            from live_transcript.storage.worker import run

            result = run(get("vm", "ssh"), args.guest_wav, args.token)
            emit(result, args.json)
            return 1 if result.get("status") in ("retrieval_failed", "postprocess_failed") else 0
        elif args.command == "jobs":
            from live_transcript.storage.worker import recent_jobs

            emit({"jobs": recent_jobs()}, args.json)
        elif args.command == "process":
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

            supervise(["--dry-run"] if args.dry_run else [])
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
    except (ValueError, OSError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        emit(dict(ok=False, error=type(exc).__name__, detail=str(exc)[:400]), args.json)
        return 1
