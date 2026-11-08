"""On-demand noVNC access to the configured VM over a loopback SSH tunnel.

Viewer lifetime is independent of capture. Control leases expire when client
heartbeats stop; neither closing the viewer nor losing SSH stops a recording.
"""

import argparse
import fcntl
import json
import ipaddress
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
import sys
from urllib.parse import urlencode

from live_transcript.core.files import atomic_json
from live_transcript.worker import is_role_process


def process_stamp(pid):
    path = Path("/proc") / str(pid)
    if path.stat().st_uid != os.getuid():
        raise ValueError("Console process belongs to another account")
    return path.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]


def operator_control_active():
    from live_transcript.core.config import get

    try:
        data = json.loads((Path(get("vm", "home")) / "logs/operator-control.json").read_text())
        now = time.time()
        pid = data["pid"]
        args = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        return (
            data["display"] == os.environ.get("DISPLAY")
            and now < data["expires"] <= now + 20
            and process_stamp(pid) == data["start_ticks"]
            and is_role_process(args, {"console"})
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def displays():
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            args = [
                os.fsdecode(value)
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
            if not args or Path(args[0]).name != "Xvfb":
                continue
            display = next(
                (value for value in args[1:] if re.fullmatch(r":\d+(?:\.\d+)?", value)), None
            )
            if not display or "-auth" not in args:
                continue
            auth = Path(args[args.index("-auth") + 1])
            if (
                auth.is_symlink()
                or auth.stat().st_uid != os.getuid()
                or auth.stat().st_mode & 0o077
            ):
                continue
            result.append(
                dict(
                    display=display,
                    auth=str(auth),
                    pid=int(entry.name),
                    start_ticks=process_stamp(entry.name),
                )
            )
        except (OSError, ValueError, IndexError):
            continue
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def viewer_url(host, port, password):
    try:
        address = ipaddress.ip_address(host)
        host = "[" + str(address) + "]" if address.version == 6 else str(address)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
            raise ValueError("Invalid advertised console host")
    return f"http://{host}:{port}/vnc.html#" + urlencode(
        dict(autoconnect=1, resize="scale", password=password)
    )


def stop_process(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)


def wait_port(port, processes, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(proc.poll() is not None for proc in processes):
            raise RuntimeError("Console helper exited; inspect its private log")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("Console listener did not become ready")


def read_handshake(stream, timeout=20):
    deadline = time.monotonic() + timeout
    data = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stream], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        block = os.read(stream.fileno(), 8192 - len(data))
        if not block:
            raise RuntimeError("VM console exited; inspect the private console log")
        data += block
        if b"\n" in data:
            return json.loads(data.split(b"\n", 1)[0])
        if len(data) >= 8192:
            raise ValueError("Invalid console handshake size")
    raise TimeoutError("VM console did not become ready")


def idle_desktop(root, start):
    """A temporary diagnostic desktop, explicitly separate from meeting capture."""
    from live_transcript.core.config import get

    display = next(
        (
            ":" + str(n)
            for n in range(40, 200)
            if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists()
        ),
        None,
    )
    if not display:
        raise RuntimeError("No free diagnostic display")
    auth = root / "Xauthority"
    auth.touch(mode=0o600)
    subprocess.run(
        ["xauth", "-f", str(auth), "add", display, ".", secrets.token_hex(16)],
        check=True,
        capture_output=True,
        timeout=5,
    )
    proc = start(
        ["Xvfb", display, "-screen", "0", "1280x960x24", "-nolisten", "tcp", "-auth", str(auth)]
    )
    env = dict(os.environ, DISPLAY=display, XAUTHORITY=str(auth))
    deadline = time.monotonic() + 5
    while not Path("/tmp/.X11-unix/X" + display[1:]).exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("Diagnostic display did not start")
        time.sleep(0.1)
    if shutil.which("openbox"):
        start(["openbox"], env=env)
    executable = get("browser", "google_executable")
    if not executable:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
    profile = root / "browser-profile"
    start(
        [
            executable,
            "--no-sandbox",
            "--incognito",
            "--no-first-run",
            "--no-default-browser-check",
            "--user-data-dir=" + str(profile),
            "--window-size=1280,900",
            "about:blank",
        ],
        env=env,
    )
    return dict(display=display, auth=str(auth), pid=proc.pid, start_ticks=process_stamp(proc.pid))


def start_desktop(token, minutes, logs):
    """The diagnostic browser has its own service, never the viewer's lifetime."""
    from live_transcript.worker import command

    descriptor = logs / ("desktop-" + token + ".json")
    options = [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        "--unit=live-transcript-desktop-" + token,
        "--property=RuntimeMaxSec=" + str(minutes * 60 + 30),
        "--property=TimeoutStopSec=15s",
        "--property=KillMode=mixed",
    ]
    if os.environ.get("LIVE_TRANSCRIPT_CONFIG"):
        options.append("--setenv=LIVE_TRANSCRIPT_CONFIG=" + os.environ["LIVE_TRANSCRIPT_CONFIG"])
    subprocess.run(
        [*options, *command("desktop", "--desktop", token, "--max-min", minutes)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            data = json.loads(descriptor.read_text())
            if data.get("error"):
                raise RuntimeError("Diagnostic desktop could not acquire the idle VM")
            if data["target"] in displays():
                return data["target"]
        except (FileNotFoundError, KeyError, ValueError):
            pass
        time.sleep(0.1)
    raise TimeoutError("Diagnostic desktop did not become ready")


def own_desktop(token, minutes):
    from live_transcript.core.config import get

    if not re.fullmatch(r"[a-f0-9]{32}", token) or not 1 <= minutes <= 120:
        raise ValueError("Invalid desktop token or lifetime")
    if subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], timeout=5).returncode:
        raise RuntimeError("Diagnostic browser must run inside the configured VM")
    os.umask(0o077)
    logs = Path(get("vm", "home")) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    descriptor = logs / ("desktop-" + token + ".json")
    children = []
    with (
        (logs / "meeting-owner.lock").open("a") as lease,
        tempfile.TemporaryDirectory(prefix="live-transcript-desktop-") as temporary,
        (logs / ("desktop-" + token + ".log")).open("ab") as log,
    ):
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if displays():
                raise RuntimeError("An existing desktop already owns this VM")

            def start(args, **kwargs):
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    **kwargs,
                )
                children.append(proc)
                return proc

            target = idle_desktop(Path(temporary), start)
            deadline = time.time() + minutes * 60
            atomic_json(descriptor, dict(target=target, expires=deadline))
            stopped = False

            def stop(*_):
                nonlocal stopped
                stopped = True

            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, stop)
            while not stopped and time.time() < deadline and children[0].poll() is None:
                time.sleep(1)
        except Exception:
            atomic_json(descriptor, dict(error="desktop_unavailable"))
            raise
        finally:
            for proc in reversed(children):
                stop_process(proc)
            if descriptor.exists() and "error" not in json.loads(descriptor.read_text()):
                descriptor.unlink()


def serve(token, control=False, display=None, idle=False, minutes=30):
    from live_transcript.core.config import get

    if not re.fullmatch(r"[a-f0-9]{32}", token) or not 1 <= minutes <= 120:
        raise ValueError("Invalid console token or lifetime")
    if subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], timeout=5).returncode:
        raise RuntimeError("Console server must run inside the configured VM")
    for name in ("x11vnc", "websockify"):
        if not shutil.which(name):
            raise RuntimeError(
                "Install x11vnc, novnc and websockify in the VM before opening its console"
            )
    webroot = Path("/usr/share/novnc")
    if not (webroot / "vnc.html").is_file():
        raise RuntimeError("VM noVNC browser client is missing")
    os.umask(0o077)
    logs = Path(get("vm", "home")) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    marker = logs / "operator-control.json"
    children = []
    with (
        (logs / "console-owner.lock").open("a") as lock,
        tempfile.TemporaryDirectory(prefix="live-transcript-console-") as temporary,
    ):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root = Path(temporary)
        with (logs / ("console-" + token + ".log")).open("ab") as log:

            def start(args, **kwargs):
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    **kwargs,
                )
                children.append(proc)
                return proc

            try:
                candidates = displays()
                if display:
                    candidates = [value for value in candidates if value["display"] == display]
                if len(candidates) > 1:
                    raise RuntimeError(
                        "Multiple VM displays are active; select --display explicitly"
                    )
                diagnostic = not candidates
                if not candidates and not idle:
                    raise RuntimeError(
                        "No browser display is active; start a meeting or use console --idle"
                    )
                target = candidates[0] if candidates else start_desktop(token, minutes, logs)
                password = secrets.token_urlsafe(8)[:8]
                auth = root / "vnc.pass"
                subprocess.run(
                    ["x11vnc", "-storepasswd", password, str(auth)],
                    stdout=log,
                    stderr=log,
                    check=True,
                    timeout=5,
                )
                vnc_port, web_port = free_port(), free_port()
                while web_port == vnc_port:
                    web_port = free_port()
                command = [
                    "x11vnc",
                    "-display",
                    target["display"],
                    "-auth",
                    target["auth"],
                    "-localhost",
                    "-rfbport",
                    str(vnc_port),
                    "-rfbauth",
                    str(auth),
                    "-forever",
                    "-shared",
                    "-noxdamage",
                ]
                if not control:
                    command.append("-viewonly")
                start(command)
                wait_port(vnc_port, children)
                start(
                    [
                        "websockify",
                        "--web",
                        str(webroot),
                        "127.0.0.1:" + str(web_port),
                        "127.0.0.1:" + str(vnc_port),
                    ]
                )
                wait_port(web_port, children)
                print(
                    json.dumps(
                        dict(
                            token=token,
                            display=target["display"],
                            web_port=web_port,
                            password=password,
                            control=control,
                            diagnostic=diagnostic,
                        )
                    ),
                    flush=True,
                )
                deadline = time.monotonic() + minutes * 60
                last_ping = time.monotonic()
                pending = b""
                while time.monotonic() < deadline and time.monotonic() - last_ping < 15:
                    if (
                        any(proc.poll() is not None for proc in children)
                        or process_stamp(target["pid"]) != target["start_ticks"]
                    ):
                        break
                    ready, _, _ = select.select([0], [], [], 1)
                    if ready:
                        block = os.read(0, 4096)
                        if not block:
                            break
                        pending += block
                        if len(pending) > 8192:
                            raise ValueError("Invalid console heartbeat")
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if line == b"PING":
                                last_ping = time.monotonic()
                    if control:
                        atomic_json(
                            marker,
                            dict(
                                token=token,
                                display=target["display"],
                                pid=os.getpid(),
                                start_ticks=process_stamp(os.getpid()),
                                expires=time.time() + 12,
                            ),
                        )
            finally:
                for proc in reversed(children):
                    stop_process(proc)
                try:
                    if json.loads(marker.read_text()).get("token") == token:
                        marker.unlink()
                except (OSError, ValueError):
                    pass


def open_console(args, emit):
    from live_transcript.core.config import get, ssh_options
    from live_transcript.runtime.deployment import build_bundle
    import hashlib
    import shlex

    if not get("vm", "ssh"):
        raise ValueError("Configure vm.ssh before opening a console")
    if not 0 <= args.port <= 65535 or not 1 <= args.max_min <= 120:
        raise ValueError("Invalid console port or lifetime")
    if args.display and not re.fullmatch(r":\d+(?:\.\d+)?", args.display):
        raise ValueError("Invalid X display")
    address = ipaddress.ip_address(args.listen)
    if address.version != 4:
        raise ValueError("Console SSH forwarding currently requires an IPv4 listen address")
    if address.is_unspecified and not args.advertise_host:
        raise ValueError("Set --advertise-host when listening on 0.0.0.0")
    advertised = (args.advertise_host or args.listen).lower()
    viewer_url(advertised, args.port or 1, "validation")
    from live_transcript.runtime.operator import ConsoleAuth

    # Validate/create host login credentials before acquiring a VM console or
    # creating a diagnostic desktop. A bad login configuration must not reserve it.
    auth = ConsoleAuth(
        get("console", "login_mode"),
        get("console", "shared_login_file"),
        get("console", "session_seconds"),
    )
    root = Path(get("paths", "logs"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex
    ssh = [
        "ssh",
        *ssh_options(),
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ExitOnForwardFailure=yes",
    ]
    with tempfile.TemporaryDirectory(prefix="live-transcript-console-") as directory:
        bundle = Path(directory) / "live-transcript.pyz"
        build_bundle(bundle)
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()[:20]
        remote_dir = str(Path(get("vm", "home")) / ".local/share/live-transcript-console" / digest)
        remote_bundle = remote_dir + "/live-transcript.pyz"
        remote = (
            "umask 077; mkdir -p "
            + shlex.quote(remote_dir)
            + " && cat > "
            + shlex.quote(remote_bundle)
        )
        subprocess.run(
            [*ssh, get("vm", "ssh"), remote],
            input=bundle.read_bytes(),
            capture_output=True,
            check=True,
            timeout=30,
        )
        options = [
            get("vm", "python"),
            "-u",
            remote_bundle,
            "console",
            token,
            "--max-min",
            str(args.max_min),
        ]
        if args.control:
            options.append("--control")
        if args.display:
            options += ["--display", args.display]
        if args.idle:
            options.append("--idle")
        server = tunnel = ui = None
        try:
            with (root / ("console-" + token + ".log")).open("ab") as log:
                os.fchmod(log.fileno(), 0o600)
                server = subprocess.Popen(
                    [*ssh, "-T", get("vm", "ssh"), shlex.join(options)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    start_new_session=True,
                )
                data = read_handshake(server.stdout)
                if (
                    data.get("token") != token
                    or type(data.get("web_port")) is not int
                    or not 1024 <= data["web_port"] <= 65535
                ):
                    raise ValueError("Invalid console handshake")
                server.stdin.write(b"PING\n")
                server.stdin.flush()
                upstream = free_port()
                forwarding = f"127.0.0.1:{upstream}:127.0.0.1:{data['web_port']}"
                tunnel = subprocess.Popen(
                    [*ssh, "-T", "-N", "-L", forwarding, get("vm", "ssh")],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                wait_port(upstream, [server, tunnel])
                from live_transcript.runtime.operator import GuestBridge, launch_ui

                bridge = GuestBridge(remote_bundle, data["display"])
                ui, url = launch_ui(
                    str(address),
                    args.port,
                    advertised,
                    upstream,
                    bridge,
                    root / ("console-" + token + ".events.jsonl"),
                    data["password"],
                    emit,
                    auth=auth,
                )
                emit(
                    dict(
                        url=url,
                        display=data["display"],
                        control=args.control,
                        diagnostic_desktop=data["diagnostic"],
                        expires_in_minutes=args.max_min,
                        note="Temporary diagnostic desktop; no recording"
                        if data["diagnostic"]
                        else "Existing VM display; capture continues independently",
                    )
                )
                deadline = time.monotonic() + args.max_min * 60
                while time.monotonic() < deadline:
                    if server.poll() is not None or tunnel.poll() is not None:
                        break
                    server.stdin.write(b"PING\n")
                    server.stdin.flush()
                    time.sleep(3)
        except KeyboardInterrupt:
            pass
        finally:
            if ui:
                ui.events.add("console_closed", "VM console closed")
                ui.shutdown()
                ui.server_close()
            if server and server.stdin:
                try:
                    server.stdin.close()
                except OSError:
                    pass
            for proc in (tunnel, server):
                if proc:
                    stop_process(proc)
    return dict(console_closed=True, meeting_stopped=False)


def main():
    if sys.argv[1:2] == ["--desktop"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--desktop", required=True)
        parser.add_argument("--max-min", type=int, default=30)
        args = parser.parse_args()
        own_desktop(args.desktop, args.max_min)
        return
    if sys.argv[1:] == ["--action"]:
        try:
            request = json.loads(sys.stdin.buffer.read(96 * 1024 + 1))
            print(json.dumps(guest_action(request)))
        except Exception:
            from live_transcript.core.config import get
            import traceback

            path = Path(get("vm", "home")) / "logs/console-actions.log"
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as log:
                os.fchmod(log.fileno(), 0o600)
                traceback.print_exc(file=log)
            print(json.dumps(dict(error="VM action failed; check target and control lease")))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("token")
    parser.add_argument("--display")
    parser.add_argument("--control", action="store_true")
    parser.add_argument("--idle", action="store_true")
    parser.add_argument("--max-min", type=int, default=30)
    args = parser.parse_args()
    serve(args.token, args.control, args.display, args.idle, args.max_min)


def guest_action(request):
    """Bounded actions for an authenticated host, never arbitrary shell input."""
    from live_transcript.core.config import get

    candidates = displays()
    if request.get("op") == "discover":
        selected = [item for item in candidates if item["display"] == request.get("display")]
        if len(selected) != 1:
            raise ValueError("Select an unambiguous active display")
        return dict(target=selected[0])
    target = request["target"]
    if target not in candidates:
        raise ValueError("The target desktop has changed")
    env = dict(os.environ, DISPLAY=target["display"], XAUTHORITY=target["auth"])
    os.environ["DISPLAY"] = target["display"]
    control = operator_control_active()
    op = request.get("op")
    if op == "status":
        now = time.time()
        latest = None
        for root in (Path(get("vm", "home")) / "rec").glob("*.archive"):
            try:
                config = json.loads((root / "config.json").read_text())
                health = json.loads((root / "health.json").read_text())
                if config.get("display") != target["display"] or health.get("stopped"):
                    continue
                if latest is None or health["utc"] > latest["utc"]:
                    latest = dict(
                        health,
                        speaker_source="Speaker evidence: screen snapshots"
                        if not config.get("cdp")
                        else "Speaker evidence: browser + screen",
                    )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        capture = None
        if latest:
            capture = {
                key: latest.get(key)
                for key in ("audio", "screen", "snapshots", "free_bytes", "speaker_source")
            }
            capture.update(
                age=max(0, now - latest["utc"]), elapsed=max(0, now - latest.get("started", now))
            )
        speech = None
        try:
            gpu = Path(get("vm", "home")) / "gpu.json"
            raw = json.loads(gpu.read_text())
            age = max(0, now - gpu.stat().st_mtime)
            if age < 20:
                speech = dict(
                    age=age,
                    queued_audio_s=raw.get("queued_audio_s"),
                    signal_flags=raw.get("audio_quality", {}).get("flags", []),
                    decode_error=bool(raw.get("decode_error")),
                )
        except (OSError, ValueError, TypeError):
            pass
        return dict(display=target["display"], control=control, capture=capture, speech=speech)
    if not control:
        raise ValueError("A live manual-control lease is required")
    if op == "key" and request.get("key") in ("Escape", "Tab", "ctrl+l"):
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", request["key"]], env=env, check=True, timeout=3
        )
        return dict(ok=True)
    if op != "paste":
        raise ValueError("Unknown VM action")
    text = request.get("text")
    if not isinstance(text, str) or not 0 < len(text.encode()) <= 16384 or "\0" in text:
        raise ValueError("Invalid paste text")
    # Foreground ownership is killed after consumption; clipboard contents are
    # never persisted to disk, echoed in errors, or passed in process arguments.
    owner = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-in", "-quiet"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        owner.stdin.write(text.encode())
        owner.stdin.close()
        time.sleep(0.1)
        if owner.poll() is not None:
            raise RuntimeError("Clipboard helper exited")
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "ctrl+v"], env=env, check=True, timeout=3
        )
        time.sleep(0.5)
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=3)
    return dict(ok=True)


if __name__ == "__main__":
    main()
