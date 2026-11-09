"""Persistent authenticated meeting workspace; viewers never own recordings."""

from copy import deepcopy
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlencode
import uuid

from live_transcript.core import config as runtime_config
from live_transcript.core.config import DEFAULTS, get, ssh_options
from live_transcript.core.files import atomic_json, atomic_text
from live_transcript.runtime.console import free_port, stop_process, process_stamp
from live_transcript.runtime.operator import (
    ConsoleAuth,
    EventFeed,
    GuestBridge,
    Handler,
    HubHandler,
    OperatorServer,
    validate_status,
)
from live_transcript.worker import command, command_env


def public_origin(value):
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid public workspace URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Public workspace URL must be an HTTP(S) origin")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        address = ipaddress.ip_address(hostname)
        hostname = "[" + str(address) + "]" if address.version == 6 else str(address)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", hostname):
            raise ValueError("Invalid public hostname")
    default = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    return (
        parsed.scheme
        + "://"
        + hostname
        + (":" + str(port) if port is not None and port != default else "")
    )


class Manager:
    """One selected guest and one disposable viewer, with bounded control leases."""

    def __init__(self, auth, host, view_only=False):
        self.auth, self.host, self.view_only = auth, host, view_only
        self.lock = threading.RLock()
        self.manual_until = 0
        self.viewer = None
        self.port = None
        self.password = ""
        self.generation = 0
        self.target_generation = secrets.token_hex(16)
        self.target = None
        self.bridge = None
        self.bundle = None
        self.updated = time.time()
        self.free_bytes = None
        self.unreachable = True
        self.snapshot = dict(
            display=None, control=False, capture=None, speech=None, admission=None, session=None
        )
        self.root = Path(get("paths", "state")) / "workspace"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile = self.root / "viewer.toml"
        values = {k: dict(runtime_config.CONFIG[k]) for k in DEFAULTS}
        values["console"].update(login_mode="shared", shared_login_file=str(self.auth_file()))
        atomic_text(
            self.profile,
            "\n\n".join(
                "[" + k + "]\n" + "\n".join(n + " = " + json.dumps(v) for n, v in row.items())
                for k, row in values.items()
            )
            + "\n",
        )

    @staticmethod
    def auth_file():
        return (
            get("console", "shared_login_file")
            if get("console", "login_mode") == "shared"
            else str(Path(get("paths", "state")) / "workspace-login.json")
        )

    def ssh(self):
        return [
            "ssh",
            *ssh_options(),
            "-o",
            "ConnectTimeout=4",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-T",
            get("vm", "ssh"),
        ]

    def deploy(self):
        from live_transcript.runtime.deployment import build_bundle
        import hashlib

        bundle = self.root / "live-transcript.pyz"
        build_bundle(bundle)
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()[:20]
        remote = get("vm", "home") + "/.local/share/live-transcript-console/" + digest
        subprocess.run(
            [
                *self.ssh(),
                "umask 077; mkdir -p "
                + shlex.quote(remote)
                + " && cat > "
                + shlex.quote(remote + "/live-transcript.pyz"),
            ],
            input=bundle.read_bytes(),
            capture_output=True,
            check=True,
            timeout=15,
        )
        self.bundle = remote + "/live-transcript.pyz"

    def inventory(self):
        if not self.bundle:
            self.deploy()
        result = subprocess.run(
            [*self.ssh(), shlex.join([get("vm", "python"), self.bundle, "console", "--action"])],
            input=b'{"op":"inventory"}',
            capture_output=True,
            timeout=12,
            check=True,
        )
        if len(result.stdout) > 32768:
            raise ValueError("Oversized guest inventory")
        value = json.loads(result.stdout)
        candidates = value["displays"]
        if not isinstance(candidates, list) or len(candidates) > 8:
            raise ValueError("Invalid guest displays")
        if type(value["free_bytes"]) is not int or value["free_bytes"] < 0:
            raise ValueError("Invalid guest storage")
        return candidates, value["free_bytes"]

    def close_viewer(self):
        if self.viewer:
            stop_process(self.viewer)
            self.viewer = None
        self.password, self.port = "", None
        self.generation += 1

    def poll(self):
        # Lease expiry is independent of guest reachability.
        with self.lock:
            if self.viewer and self.viewer_control and time.time() >= self.manual_until:
                self.close_viewer()
        candidates, free = self.inventory()
        with self.lock:
            self.free_bytes = free
            target = candidates[0] if len(candidates) == 1 else None
            control = not self.view_only and time.time() < self.manual_until
            if target != self.target:
                self.close_viewer()
                self.manual_until = 0
                self.target_generation = secrets.token_hex(16)
                control = False
                self.target, self.bridge = target, None
                if target:
                    self.bridge = GuestBridge(self.bundle, target["display"])
            if self.bridge:
                value = validate_status(self.bridge.call(dict(op="status")))
                self.snapshot = value
            else:
                self.snapshot = dict(
                    display=None,
                    control=False,
                    capture=None,
                    speech=None,
                    admission=None,
                    session=None,
                )
            self.updated, self.unreachable = time.time(), False
            if self.viewer and (self.viewer.poll() is not None or control != self.viewer_control):
                self.close_viewer()
            if target and not self.viewer:
                self.port = free_port()
                self.viewer_control = control
                args = [
                    sys.executable,
                    "-u",
                    "-m",
                    "live_transcript",
                    "--config",
                    str(self.profile),
                    "--json",
                    "console",
                    "--display",
                    target["display"],
                    "--port",
                    str(self.port),
                    "--advertise-host",
                    self.host,
                    "--max-min",
                    "120",
                ]
                if control:
                    args.append("--control")
                env = dict(os.environ, LT_APPLICATION_JOB="workspace-viewer")
                env = command_env(sys.executable, base=env) or env
                self.viewer_log = self.root / "viewer.log"
                with self.viewer_log.open("wb") as log:
                    self.viewer = subprocess.Popen(
                        args,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
            if self.viewer and not self.password:
                # The child must prove readiness using our private login capability.
                with self.viewer_log.open("rb") as log:
                    log.seek(max(0, self.viewer_log.stat().st_size - 65536))
                    for line in log:
                        try:
                            value = json.loads(line)
                            url = urlsplit(value.get("url", ""))
                            fragment = parse_qs(url.fragment)
                            if (
                                url.port == self.port
                                and fragment.get("token") == [self.auth.token]
                                and fragment.get("password")
                            ):
                                self.password = fragment["password"][0]
                                self.generation += 1
                        except (ValueError, AttributeError):
                            pass

    def status(self):
        with self.lock:
            result = deepcopy(self.snapshot)
            elapsed = max(0, time.time() - self.updated)
            for key in ("capture", "speech"):
                if result.get(key):
                    result[key]["age"] += elapsed
            result["control"] = bool(
                result.get("control")
                and not self.view_only
                and time.time() < self.manual_until
                and self.password
                and self.viewer_control
            )
            return result

    def control(self, enabled, target_generation=None):
        if self.view_only or type(enabled) is not bool:
            raise ValueError("Manual control is unavailable")
        with self.lock:
            if enabled and (
                self.target is None
                or not isinstance(target_generation, str)
                or target_generation != self.target_generation
            ):
                raise ValueError("Desktop changed; refresh before taking control")
            self.manual_until = time.time() + 15 if enabled else 0

    def action(self, data):
        with self.lock:
            if not self.status()["control"] or not self.bridge:
                raise RuntimeError("Manual control is not active on this display")
            bridge = self.bridge
        return bridge.call(data)


class SnapshotBridge:
    def __init__(self, manager):
        self.manager = manager

    def call(self, data):
        if data.get("op") == "status":
            return self.manager.status()
        return self.manager.action(data)

    def open_audio(self):
        with self.manager.lock:
            if not self.manager.bridge or self.manager.unreachable:
                raise RuntimeError("Meeting audio is unavailable")
            return self.manager.bridge.open_audio()


def current_run():
    from live_transcript.runtime.state import current

    value = current()
    return {key: value.get(key) for key in ("run_id", "status", "started", "deadline")}


def schedule(kind, args, *, key=None, deadline=None):
    ident = key or kind + "-" + uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", ident):
        raise ValueError("Invalid workspace request identity")
    env = {"LT_DEADLINE": str(deadline)} if deadline is not None else {}
    if get("runtime", "supervisor") == "app":
        from live_transcript.runtime.application import Queue, identity

        queue = Queue()
        try:
            # An HTTP retry retains the original absolute meeting deadline.
            row = queue.row(ident)
            if row:
                if (row["kind"], row["arguments"], row["identity"]) != (kind, args, identity()):
                    raise ValueError("Request identity belongs to different work")
            else:
                row = queue.submit(kind, args, key=ident, env=env)
            return dict(id=row["id"], status=row["status"], kind=kind)
        finally:
            queue.close()
    # A durable receipt precedes systemd submission: ambiguous timeouts must
    # never make the same HTTP request join a second meeting.
    root = Path(get("paths", "state")) / "workspace/requests"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fingerprint = hashlib.sha256(json.dumps([kind, args]).encode()).hexdigest()
    receipt = root / (ident + ".json")
    with (root / "submission.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if receipt.exists():
            row = json.loads(receipt.read_text())
            if row["fingerprint"] != fingerprint:
                raise ValueError("Request identity belongs to different work")
            return {k: row[k] for k in ("id", "kind", "status")}
        row = dict(id=ident, kind=kind, status="submission_pending", fingerprint=fingerprint)
        atomic_json(receipt, row)
        cli = [
            sys.executable,
            "-m",
            "live_transcript",
            "--config",
            runtime_config.CONFIG["_file"],
            kind if kind != "launch" else "start",
            *args,
        ]
        process_env = command_env(sys.executable) or os.environ
        options = [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            "--unit=live-transcript-workspace-" + ident,
        ]
        if process_env.get("PYTHONPATH"):
            options.append("--setenv=PYTHONPATH=" + process_env["PYTHONPATH"])
        options += ["--setenv=" + k + "=" + v for k, v in env.items()]
        subprocess.run(
            [*options, *cli], capture_output=True, check=True, timeout=10, env=process_env
        )
        row["status"] = "queued"
        atomic_json(receipt, row)
        return {k: row[k] for k in ("id", "kind", "status")}


class WorkspaceServer(OperatorServer):
    def __init__(self, address, host, manager, events):
        self.manager = manager
        self.advertised_host = host
        self.media_slots = threading.BoundedSemaphore(4)
        self.operation_lock = threading.Lock()
        super().__init__(address, 0, SnapshotBridge(manager), events, host, auth=manager.auth)
        self.RequestHandlerClass = WorkspaceHandler

    def session(self):
        return dict(
            authenticated=True,
            persistent=True,
            release=os.environ.get("LIVE_TRANSCRIPT_RELEASE", ""),
            login_mode=self.auth.mode,
            viewer_password=self.manager.password,
            control_enabled=not self.manager.view_only,
        )

    def status(self):
        from live_transcript.runtime.capacity import report

        value = super().status()
        value["workspace"] = dict(
            viewer_ready=bool(self.manager.password),
            generation=self.manager.generation,
            target_generation=self.manager.target_generation,
            control_lease_active=time.time() < self.manager.manual_until,
            viewer_password=self.manager.password,
            unreachable=self.manager.unreachable,
            control_enabled=not self.manager.view_only,
            run=current_run(),
        )
        if get("runtime", "supervisor") == "app":
            from live_transcript.runtime.application import heartbeat_ready

            value["workspace"]["application_ready"] = heartbeat_ready()
        value["capacity"] = report(guest_free=self.manager.free_bytes)
        return value

    def monitor(self):
        last_capacity, lost = None, False
        scan_at, scanner = 0, None
        while not self.monitor_stop.is_set():
            try:
                self.manager.poll()
                if lost:
                    self.events.add("vm_reconnected", "VM status connection restored")
                lost = False
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError):
                self.manager.unreachable = True
                self.manager.bundle = None
                if not lost:
                    self.events.add(
                        "vm_unreachable",
                        "VM status is unavailable; capture may still be running",
                        level="warning",
                    )
                lost = True
            with self.status_lock:
                self.cached_status = None
            try:
                value = self.status()
                capacity = value["capacity"]["status"]
                if capacity != last_capacity and capacity == "blocked":
                    self.events.add(
                        "capacity_attention",
                        "Recording capacity is below its reserve; active capture is retained",
                        level="warning",
                    )
                last_capacity = capacity
            except (OSError, ValueError, RuntimeError):
                pass
            if scanner and scanner.poll() is None and time.monotonic() - scan_at > 25:
                stop_process(scanner)
            if not scanner or scanner.poll() is not None:
                if time.monotonic() - scan_at >= 30:
                    scan_at = time.monotonic()
                    with (self.manager.root / "library.log").open("ab") as log:
                        scanner = subprocess.Popen(
                            command("library", "--scan"),
                            env=command_env(sys.executable),
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                        )
            atomic_json(
                self.manager.root / "health.json",
                dict(checked_at=time.time(), pid=os.getpid(), ticks=process_stamp(os.getpid())),
            )
            self.monitor_stop.wait(3)
        if scanner and scanner.poll() is None:
            stop_process(scanner)


class WorkspaceHandler(HubHandler):
    def target(self):
        if not self.server.manager.password or not self.server.manager.port:
            return None
        return self.server.manager.port, urlsplit(self.path).path

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/novnc/"):
            return self.forward("GET")
        if path.startswith("/api/recordings") or path == "/api/jobs":
            if not self.valid_host() or not self.authenticated():
                return self.reply(401, dict(error="Open the private workspace link"))
            try:
                from live_transcript.runtime import library

                if path == "/api/recordings":
                    query = parse_qs(urlsplit(self.path).query)
                    return self.reply(
                        200,
                        library.listing(
                            query.get("q", [""])[0], int(query.get("offset", ["0"])[0])
                        ),
                    )
                match = re.fullmatch(r"/api/recordings/([a-f0-9]{32})(?:/([a-z]+))?", path)
                if match:
                    if match[2]:
                        return self.media(match[1], match[2])
                    return self.reply(200, library.detail(match[1]))
                if path == "/api/jobs":
                    return self.reply(200, dict(jobs=self.jobs()))
                return self.reply(404, dict(error="Unknown recording"))
            except (ValueError, sqlite3.Error, TypeError, IndexError):
                return self.reply(400, dict(error="Recording request is unavailable"))
            except (OSError, KeyError):
                return self.reply(404, dict(error="Recording is unavailable"))
        # Skip HubHandler's select-a-VM requirement for this single VM workspace.
        return Handler.do_GET(self)

    def jobs(self):
        if get("runtime", "supervisor") != "app":
            return []
        from live_transcript.runtime.application import Queue, identity

        queue = Queue()
        try:
            rows = queue.db.execute(
                "SELECT state FROM jobs ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
            return [
                {k: row.get(k) for k in ("id", "kind", "status", "cancel_requested")}
                for (raw,) in rows
                if (row := json.loads(raw)).get("identity") == identity()
            ]
        finally:
            queue.close()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in (
            "/api/meeting/start",
            "/api/meeting/stop",
            "/api/control",
            "/api/jobs/cancel",
        ):
            return Handler.do_POST(self)
        self.close_connection = True
        if not self.valid_host() or not self.same_origin() or not self.authenticated():
            return self.reply(403, dict(error="Authenticated workspace origin required"))
        if self.server.manager.view_only:
            return self.reply(403, dict(error="This workspace is view only"))
        try:
            data = self.body()
            with self.server.operation_lock:
                if path == "/api/control":
                    self.server.manager.control(data.get("enabled"), data.get("target_generation"))
                    return self.reply(200, dict(ok=True))
                if path == "/api/jobs/cancel":
                    if get("runtime", "supervisor") != "app":
                        raise ValueError("Processing cancellation needs app supervision")
                    from live_transcript.runtime.application import request_cancel

                    request_cancel(data.get("id"), allowed_kind="process")
                    return self.reply(202, dict(ok=True))
                if path == "/api/meeting/stop":
                    run = current_run()
                    if (
                        not run.get("run_id")
                        or data.get("run_id") != run["run_id"]
                        or run.get("status") not in ("active", "control_lost")
                    ):
                        raise ValueError("Meeting changed; refresh before stopping")
                    result = schedule(
                        "stop", ["--run-id", run["run_id"]], key="workspace-stop-" + run["run_id"]
                    )
                else:
                    from live_transcript.runtime.state import validate_invitation

                    validate_invitation(data.get("url"))
                    minutes = data.get("minutes", 180)
                    if type(minutes) is not int or not 1 <= minutes <= 480:
                        raise ValueError("Meeting duration must be 1–480 minutes")
                    if self.server.manager.unreachable:
                        raise RuntimeError("VM status is unavailable; inspect it before joining")
                    if self.server.manager.target is not None or current_run().get("status") in (
                        "active",
                        "control_lost",
                    ):
                        raise RuntimeError("A browser or meeting already owns this VM")
                    from live_transcript.runtime.capacity import preflight

                    preflight(minutes)
                    ident = data.get("request_id")
                    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
                        raise ValueError("Invalid request identity")
                    result = schedule(
                        "launch",
                        [data["url"], "--max-min", str(minutes), "--name", get("identity", "name")],
                        key="workspace-launch-" + ident,
                        deadline=time.time() + minutes * 60,
                    )
                self.server.events.add(
                    "workspace_" + ("stop" if path.endswith("stop") else "start"),
                    "Meeting stop requested" if path.endswith("stop") else "Meeting start queued",
                )
                self.reply(202, result)
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.reply(400, dict(error="Invalid request or meeting state changed"))
        except (RuntimeError, OSError, subprocess.SubprocessError):
            self.reply(
                409, dict(error="Request could not be queued; check VM ownership and capacity")
            )

    def media(self, ident, kind):
        from live_transcript.runtime.library import artifact

        if not self.server.media_slots.acquire(blocking=False):
            return self.reply(429, dict(error="Too many playback streams"))
        sent = False
        try:
            with artifact(ident, kind) as (stream, details, mime):
                size = details.st_size
                etag = (
                    '"'
                    + str(details.st_ino)
                    + "-"
                    + str(details.st_mtime_ns)
                    + "-"
                    + str(size)
                    + '"'
                )
                start, end, partial = 0, size - 1, False
                requested = self.headers.get("Range")
                if requested and self.headers.get("If-Range", etag) == etag:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                    if not match or not (match[1] or match[2]):
                        raise ValueError("Unsupported byte range")
                    if match[1]:
                        start = int(match[1])
                        end = min(end, int(match[2])) if match[2] else end
                    else:
                        start = max(0, size - int(match[2]))
                    if start > end or start >= size:
                        self.send_response(416)
                        self.headers_safe()
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    partial = True
                stream.seek(start)
                self.send_response(206 if partial else 200)
                self.headers_safe()
                self.send_header("Content-Type", mime)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", etag)
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                sent = True
                left = end - start + 1
                while left > 0 and not self.server.monitor_stop.is_set() and self.authenticated():
                    block = stream.read(min(65536, left))
                    if not block:
                        break
                    self.wfile.write(block)
                    left -= len(block)
                if left > 0:
                    self.close_connection = True
        except (OSError, ValueError):
            if not sent:
                self.reply(404, dict(error="Recording media is unavailable"))
        finally:
            self.server.media_slots.release()


def run(args, emit):
    root = Path(get("paths", "state")) / "workspace"
    if args.health:
        try:
            value = json.loads((root / "health.json").read_text())
            return (
                0
                if 0 <= time.time() - value["checked_at"] < 20
                and process_stamp(value["pid"]) == value["ticks"]
                else 1
            )
        except (OSError, ValueError, KeyError):
            return 1
    address = ipaddress.ip_address(args.listen)
    if (
        address.version != 4
        or not 0 <= args.port <= 65535
        or address.is_unspecified
        and not args.advertise_host
    ):
        raise ValueError("Select IPv4 listen, port, and advertised host")
    host = (args.advertise_host or args.listen).lower()
    from live_transcript.runtime.console import viewer_url

    viewer_url(host, args.port or 1, "validation")
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "owner.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        auth = ConsoleAuth("shared", Manager.auth_file(), get("console", "session_seconds"))
        manager = Manager(auth, host, args.view_only)
        events = EventFeed(
            Path(get("paths", "logs")) / ("console-" + uuid.uuid4().hex + ".events.jsonl"), emit
        )
        server = WorkspaceServer((args.listen, args.port), host, manager, events)
        server.external_origin = (
            public_origin(args.public_url)
            if getattr(args, "public_url", None)
            else "http://" + server.authority
        )
        server.authority = urlsplit(server.external_origin).netloc
        url = server.external_origin + "/#" + urlencode(dict(token=auth.token))
        events.add(
            "console_ready",
            "Meeting workspace is ready",
            url=url,
            event_file=str(events.path),
            pid=os.getpid(),
            start_ticks=process_stamp(os.getpid()),
            expires_with_console=True,
        )
        monitor = threading.Thread(target=server.monitor, daemon=True)
        monitor.start()

        def stop(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            server.server_close()
            monitor.join(timeout=20)
            with manager.lock:
                manager.close_viewer()
        return 0
