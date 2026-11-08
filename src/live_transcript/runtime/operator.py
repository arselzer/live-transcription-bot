"""Authenticated VM console, fixed-upstream noVNC proxy, and private event feed.

Only trusted VPN/loopback exposure is supported directly. Terminate TLS upstream
for untrusted networks. Guest actions use pinned SSH and a pinned Xvfb process;
neither HTTP clients nor clipboard text can select a command or target host.
"""

from collections import deque
import fcntl
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import secrets
import re
import select
import shlex
import socket
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlencode


MAX_BODY = 96 * 1024


class ConsoleAuth:
    """Optional shared login group with signed, server-expiring browser sessions."""

    def __init__(self, mode="isolated", shared_file="", session_seconds=7200):
        if (
            mode not in ("isolated", "shared")
            or type(session_seconds) is not int
            or not 60 <= session_seconds <= 86400
        ):
            raise ValueError("Invalid console authentication settings")
        self.mode, self.session_seconds = mode, session_seconds
        data = self.new_credentials()
        if mode == "shared":
            if not shared_file:
                raise ValueError("Shared console login requires a private credentials file")
            path = Path(shared_file).expanduser().absolute()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            parent = path.parent.stat()
            if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
                raise ValueError(
                    "Shared console login directory must be owned and not writable by others"
                )
            lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(lock_fd, "r+") as lock:
                self.check_private(lock.fileno())
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except FileNotFoundError:
                    from live_transcript.core.files import atomic_json

                    atomic_json(path, data)
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "r") as source:
                    self.check_private(source.fileno())
                    data = json.loads(source.read(4097))
                if (
                    not isinstance(data, dict)
                    or data.get("version") != 1
                    or any(
                        not isinstance(data.get(key), str) or not re.fullmatch(pattern, data[key])
                        for key, pattern in (
                            ("token", r"[A-Za-z0-9_-]{43}"),
                            ("session_key", r"[A-Za-z0-9_-]{43}"),
                            ("group", r"[a-f0-9]{16}"),
                        )
                    )
                ):
                    raise ValueError("Invalid shared console login file")
        self.token = data["token"]
        self.session_key = data["session_key"].encode()
        self.cookie_name = "lt_console_" + data["group"]

    @staticmethod
    def new_credentials():
        return dict(
            version=1,
            token=secrets.token_urlsafe(32),
            session_key=secrets.token_urlsafe(32),
            group=secrets.token_hex(8),
        )

    @staticmethod
    def check_private(descriptor):
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_size > 4096
        ):
            raise ValueError("Console login files must be regular, owner-only files")

    def issue(self):
        issued = int(time.time())
        payload = f"{issued}.{issued + self.session_seconds}.{secrets.token_hex(8)}"
        signature = hmac.new(self.session_key, payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + signature

    def accepts(self, value):
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{1,12}\.\d{1,12}\.[a-f0-9]{16}\.[a-f0-9]{64}", value
        ):
            return False
        issued, expires, nonce, signature = value.split(".")
        now = time.time()
        if not (
            int(issued) <= now + 30
            and now < int(expires)
            and 60 <= int(expires) - int(issued) <= 86400
        ):
            return False
        payload = ".".join((issued, expires, nonce))
        expected = hmac.new(self.session_key, payload.encode(), hashlib.sha256).hexdigest()
        return secrets.compare_digest(signature, expected)


class EventFeed:
    def __init__(self, path, emit=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        self.items = deque(maxlen=100)
        self.emit = emit

    def add(self, kind, message, level="info", **fields):
        with self.lock:
            event = dict(
                id=secrets.token_hex(8),
                utc=time.time(),
                kind=kind,
                message=message,
                level=level,
                **fields,
            )
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(event) + "\n")
                stream.flush()
            self.items.append(event)
            if self.emit:
                self.emit(event)
            return event

    def recent(self):
        with self.lock:
            return list(self.items)


class GuestBridge:
    """One bounded SSH request per action; independent of the existing console."""

    def __init__(self, bundle, display):
        from live_transcript.core.config import get, ssh_options

        self.command = [
            "ssh",
            *ssh_options(),
            "-o",
            "ConnectTimeout=5",
            "-T",
            get("vm", "ssh"),
            shlex.join([get("vm", "python"), "-u", bundle, "console", "--action"]),
        ]
        self.lock = threading.Lock()
        self.target = None
        self.diagnostics = Path(get("paths", "logs")) / "console-actions.log"
        reply = self.call(dict(op="discover", display=display))
        self.target = reply["target"]

    def call(self, data):
        with self.lock:
            payload = dict(data, target=self.target)
            result = subprocess.run(
                self.command, input=json.dumps(payload).encode(), capture_output=True, timeout=12
            )
            if result.returncode:
                self.diagnostics.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.diagnostics, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "ab") as log:
                    os.fchmod(log.fileno(), 0o600)
                    log.write(result.stderr[-8192:] + b"\n")
                # stderr may contain paths or text; keep it out of the public API.
                raise RuntimeError("VM control is unavailable; check the private console log")
            value = json.loads(result.stdout)
            if "error" in value:
                raise RuntimeError(value["error"])
            return value


class OperatorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address, upstream, bridge, events, advertised_host, *, auth=None, viewer_password=""
    ):
        from live_transcript.core.config import get

        self.upstream = ("127.0.0.1", upstream)
        self.bridge, self.events = bridge, events
        self.auth = auth or ConsoleAuth(
            get("console", "login_mode"),
            get("console", "shared_login_file"),
            get("console", "session_seconds"),
        )
        self.token, self.cookie_name = self.auth.token, self.auth.cookie_name
        self.viewer_password = viewer_password
        self.cached_status = None
        self.status_lock = threading.Lock()
        self.last_capture_state = None
        self.last_speech_error = False
        self.last_interaction = 0
        self.monitor_stop = threading.Event()
        super().__init__(address, Handler)
        self.authority = f"{advertised_host}:{self.server_port}"

    def monitor(self):
        lost = False
        while not self.monitor_stop.is_set():
            try:
                self.status()
                if lost:
                    self.events.add("vm_reconnected", "VM status connection restored")
                lost = False
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
                if not lost:
                    self.events.add(
                        "vm_unreachable",
                        "VM status is unavailable; capture may still be running",
                        level="warning",
                    )
                lost = True
            self.monitor_stop.wait(4)

    def server_close(self):
        self.monitor_stop.set()
        super().server_close()

    def status(self):
        with self.status_lock:
            if self.cached_status and time.monotonic() - self.cached_status[0] < 3:
                return self.cached_status[1]
            value = self.bridge.call(dict(op="status"))
            capture = value.get("capture")
            healthy = bool(
                capture
                and capture["age"] < 20
                and capture.get("audio", {}).get("running")
                and capture.get("screen", {}).get("running")
            )
            capture_state = "healthy" if healthy else "attention" if capture else "pending"
            if capture is None and self.last_capture_state in ("healthy", "attention"):
                capture_state = "attention"
            if capture_state != self.last_capture_state:
                self.events.add(
                    "capture_" + capture_state,
                    {
                        "healthy": "Audio and video capture are running",
                        "pending": "Waiting for capture to start on this display",
                        "attention": "No healthy audio/video capture on this display",
                    }[capture_state],
                    level="warning" if capture_state == "attention" else "info",
                )
                self.last_capture_state = capture_state
            speech = value.get("speech")
            if speech is not None:
                failed = bool(speech.get("decode_error"))
                if failed != self.last_speech_error:
                    self.events.add(
                        "speech_degraded" if failed else "speech_recovered",
                        "Recognition is delayed; source capture is independent"
                        if failed
                        else "Recognition connection recovered",
                        level="warning" if failed else "info",
                    )
                    self.last_speech_error = failed
            self.cached_status = (time.monotonic(), value)
            return value

    def session(self):
        return dict(
            authenticated=True, viewer_password=self.viewer_password, login_mode=self.auth.mode
        )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, *_):
        pass  # Never log URL credentials, clipboard data, cookies, or request bodies.

    def headers_safe(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' ws: wss:; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'",
        )

    def reply(self, code, data, mime="application/json"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(code)
        self.headers_safe()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authenticated(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return self.server.cookie_name in cookie and self.server.auth.accepts(
                cookie[self.server.cookie_name].value
            )
        except Exception:
            return False

    def valid_host(self):
        return self.headers.get("Host") == self.server.authority

    def same_origin(self):
        return self.headers.get("Origin") == "http://" + self.server.authority

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer encoding is unsupported")
        count = int(self.headers.get("Content-Length", "0"))
        if not 0 < count <= MAX_BODY:
            raise ValueError("Invalid request size")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Expected JSON")
        data = json.loads(self.rfile.read(count))
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return data

    def do_POST(self):
        self.close_connection = True
        if not self.valid_host() or not self.same_origin():
            return self.reply(403, dict(error="Console origin does not match"))
        try:
            data = self.body()
            if self.path == "/api/session":
                token = data.get("token")
                if not isinstance(token, str) or not secrets.compare_digest(
                    token.encode(), self.server.token.encode()
                ):
                    return self.reply(401, dict(error="Open the full private console link"))
                response = json.dumps(self.server.session()).encode()
                self.send_response(200)
                self.headers_safe()
                self.send_header(
                    "Set-Cookie",
                    f"{self.server.cookie_name}={self.server.auth.issue()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={self.server.auth.session_seconds}",
                )
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                return self.wfile.write(response)
            if not self.authenticated():
                return self.reply(401, dict(error="Console session expired; reopen its link"))
            if self.path == "/api/action":
                op = data.get("op")
                if op == "paste":
                    text = data.get("text")
                    if (
                        not isinstance(text, str)
                        or not 0 < len(text.encode()) <= 16384
                        or "\0" in text
                    ):
                        raise ValueError("Paste must contain 1–16384 bytes of text without NUL")
                    result = self.server.bridge.call(dict(op=op, text=text))
                    self.server.events.add(
                        "operator_paste", "Text pasted into the VM", bytes=len(text.encode())
                    )
                elif op == "key" and data.get("key") in ("Escape", "Tab", "ctrl+l"):
                    result = self.server.bridge.call(dict(op=op, key=data["key"]))
                    self.server.events.add(
                        "operator_key", "Operator used a keyboard shortcut", key=data["key"]
                    )
                else:
                    raise ValueError("Unsupported action")
                return self.reply(200, result)
            if self.path == "/api/interaction":
                now = time.monotonic()
                if now - self.server.last_interaction > 10:
                    self.server.last_interaction = now
                    self.server.events.add("operator_interaction", "Manual interaction with the VM")
                return self.reply(200, dict(ok=True))
            self.reply(404, dict(error="Unknown action"))
        except (ValueError, UnicodeError):
            self.reply(400, dict(error="Invalid action or request body"))
        except (RuntimeError, OSError, subprocess.SubprocessError):
            self.server.events.add("control_error", "VM control request failed", level="warning")
            self.reply(
                503, dict(error="VM control is unavailable. Recording may still be running.")
            )

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, dict(error="Console host does not match"))
        path = urlsplit(self.path).path
        if path == "/favicon.ico":
            return self.reply(204, b"", "image/x-icon")
        if path in ("/", "/app.js", "/style.css"):
            name = "index.html" if path == "/" else path[1:]
            mime = {
                "index.html": "text/html; charset=utf-8",
                "app.js": "text/javascript",
                "style.css": "text/css",
            }[name]
            return self.reply(
                200, files("live_transcript").joinpath("operator_ui", name).read_bytes(), mime
            )
        if path == "/api/session":
            return self.reply(
                200, self.server.session() if self.authenticated() else dict(authenticated=False)
            )
        if not self.authenticated():
            return self.reply(401, dict(error="Console session expired; reopen its link"))
        if path == "/api/consoles":
            return self.reply(
                200,
                dict(
                    consoles=getattr(
                        self.server, "consoles", [dict(id="local", label="Current VM", prefix="")]
                    )
                ),
            )
        if path == "/api/events":
            return self.reply(200, dict(events=self.server.events.recent()))
        if path == "/api/status":
            try:
                return self.reply(200, self.server.status())
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                return self.reply(
                    503, dict(error="VM status is unavailable; capture is independent")
                )
        if path.startswith("/novnc/"):
            if self.headers.get("Upgrade", "").lower() == "websocket":
                if not self.same_origin():
                    return self.reply(403, dict(error="Invalid WebSocket origin"))
                return self.proxy_websocket()
            return self.proxy_asset(path)
        self.reply(404, dict(error="Not found"))

    def proxy_asset(self, path):
        import http.client

        if ".." in path or "%" in path or "\\" in path:
            return self.reply(400, dict(error="Invalid viewer asset"))
        if path == "/novnc/package.json":
            # Debian's noVNC package omits the optional version metadata file.
            return self.reply(200, dict(name="novnc"))
        conn = http.client.HTTPConnection(*self.server.upstream, timeout=10)
        try:
            conn.request("GET", path.removeprefix("/novnc"))
            upstream = conn.getresponse()
            body = upstream.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("Oversized viewer asset")
            self.reply(
                upstream.status,
                body,
                upstream.getheader("Content-Type", "application/octet-stream"),
            )
        except (OSError, ValueError, http.client.HTTPException):
            self.reply(502, dict(error="Viewer tunnel is unavailable; try reconnecting"))
        finally:
            conn.close()

    def proxy_websocket(
        self, address=None, request_path="/websockify", authority="127.0.0.1", extra_headers=None
    ):
        if address is None and urlsplit(self.path).path != "/novnc/websockify":
            return self.reply(404, dict(error="Unknown viewer socket"))
        self.close_connection = True
        try:
            with socket.create_connection(address or self.server.upstream, timeout=5) as upstream:
                allowed = (
                    "Upgrade",
                    "Connection",
                    "Sec-WebSocket-Key",
                    "Sec-WebSocket-Version",
                    "Sec-WebSocket-Protocol",
                )
                lines = [f"GET {request_path} HTTP/1.1", f"Host: {authority}"]
                lines += [f"{key}: {self.headers[key]}" for key in allowed if self.headers.get(key)]
                lines += [f"{key}: {value}" for key, value in (extra_headers or {}).items()]
                upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
                self.wfile.flush()
                self.connection.settimeout(10)
                upstream.settimeout(10)
                while not self.server.monitor_stop.is_set() and self.authenticated():
                    ready, _, _ = select.select([self.connection, upstream], [], [], 5)
                    if not ready:
                        continue
                    for source in ready:
                        block = source.recv(65536)
                        if not block:
                            return
                        (upstream if source is self.connection else self.connection).sendall(block)
        except (OSError, ValueError):
            pass


class ConsoleHub(ThreadingHTTPServer):
    """One origin for fixed local consoles; guest capture remains independently owned."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, advertised_host, entries, auth):
        if auth.mode != "shared":
            raise ValueError("The console hub requires shared console authentication")
        self.targets = {}
        if not 1 <= len(entries) <= 32:
            raise ValueError("Configure between 1 and 32 consoles")
        for entry in entries:
            ident, separator, port = entry.partition("=")
            if (
                not separator
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", ident)
                or not port.isascii()
                or not port.isdecimal()
                or not 1 <= int(port) <= 65535
                or ident in self.targets
                or int(port) == address[1]
            ):
                raise ValueError("Each console must have a unique name and local port: name=6081")
            self.targets[ident] = int(port)
        if len(set(self.targets.values())) != len(self.targets):
            raise ValueError("Each console must use a different local port")
        self.consoles = [
            dict(id=ident, label=ident.replace("-", " ").title(), prefix="/vm/" + ident)
            for ident in self.targets
        ]
        self.auth, self.token, self.cookie_name = auth, auth.token, auth.cookie_name
        self.advertised_host = advertised_host
        self.monitor_stop = threading.Event()
        super().__init__(address, HubHandler)
        self.authority = f"{advertised_host}:{self.server_port}"

    def session(self):
        return dict(authenticated=True, login_mode=self.auth.mode, consoles=self.consoles)

    def server_close(self):
        self.monitor_stop.set()
        super().server_close()


class HubHandler(Handler):
    def target(self):
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/vm/([a-z][a-z0-9-]{0,39})(/.*)", path)
        if match and match[1] in self.server.targets:
            return self.server.targets[match[1]], match[2]
        return None

    def do_GET(self):
        if self.path.startswith("/vm/"):
            return self.forward("GET")
        if urlsplit(self.path).path in ("/api/status", "/api/events"):
            return self.reply(404, dict(error="Select a VM first"))
        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/vm/"):
            return self.forward("POST")
        if self.path != "/api/session":
            return self.reply(404, dict(error="Unknown action"))
        super().do_POST()

    def forward(self, method):
        import http.client

        if not self.valid_host() or (method == "POST" and not self.same_origin()):
            return self.reply(403, dict(error="Console origin does not match"))
        if not self.authenticated():
            return self.reply(401, dict(error="Console session expired; reopen its link"))
        target = self.target()
        if not target:
            return self.reply(404, dict(error="Unknown VM"))
        port, path = target
        socket_request = self.headers.get("Upgrade", "").lower() == "websocket"
        allowed = (
            path in ("/api/session", "/api/status", "/api/events") or path.startswith("/novnc/")
            if method == "GET"
            else path in ("/api/action", "/api/interaction")
        )
        if not allowed or any(value in path for value in ("..", "%", "\\")):
            return self.reply(404, dict(error="Unknown console endpoint"))
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        headers = dict(Cookie=f"{self.server.cookie_name}={cookie[self.server.cookie_name].value}")
        authority = f"{self.server.advertised_host}:{port}"
        headers["Origin"] = "http://" + authority
        if socket_request:
            if path != "/novnc/websockify" or not self.same_origin():
                return self.reply(403, dict(error="Invalid WebSocket origin"))
            return self.proxy_websocket(("127.0.0.1", port), path, authority, headers)
        payload = None
        if method == "POST":
            self.close_connection = True
            try:
                payload = json.dumps(self.body()).encode()
            except (ValueError, UnicodeError):
                return self.reply(400, dict(error="Invalid action or request body"))
            headers["Content-Type"] = "application/json"
        headers["Host"] = authority
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=16)
        try:
            conn.request(method, path, payload, headers)
            response = conn.getresponse()
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("Oversized console response")
            if response.status == 401:
                return self.reply(
                    503, dict(error="This console must use the same shared login file as the hub")
                )
            self.reply(
                response.status,
                body,
                response.getheader("Content-Type", "application/octet-stream"),
            )
        except (OSError, ValueError, http.client.HTTPException):
            self.reply(
                502, dict(error="This VM console is unavailable; recording may still be running")
            )
        finally:
            conn.close()


def run_hub(args, emit):
    from live_transcript.core.config import get
    from live_transcript.runtime.console import viewer_url
    import ipaddress

    address = ipaddress.ip_address(args.listen)
    if address.version != 4 or not 0 <= args.port <= 65535:
        raise ValueError("Use an IPv4 listen address and a valid port")
    if address.is_unspecified and not args.advertise_host:
        raise ValueError("Set --advertise-host when listening on 0.0.0.0")
    advertised = (args.advertise_host or args.listen).lower()
    viewer_url(advertised, args.port or 1, "validation")
    auth = ConsoleAuth(
        get("console", "login_mode"),
        get("console", "shared_login_file"),
        get("console", "session_seconds"),
    )
    server = ConsoleHub((args.listen, args.port), advertised, args.console, auth)
    emit(
        dict(
            url=f"http://{server.authority}/#" + urlencode(dict(token=auth.token)),
            consoles=server.consoles,
            note="Switching consoles does not stop VM capture",
        )
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def launch_ui(
    listen, port, advertised, upstream, bridge, event_path, password, emit=None, *, auth=None
):
    from live_transcript.runtime.console import process_stamp

    events = EventFeed(event_path, emit)
    server = OperatorServer(
        (listen, port), upstream, bridge, events, advertised, auth=auth, viewer_password=password
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://{server.authority}/#" + urlencode(dict(token=server.token, password=password))
    # Private feed intentionally includes capability URL so Hermes can share it
    # in the user's configured conversation. It never leaves this host itself.
    events.add(
        "console_ready",
        "VM console is ready",
        url=url,
        event_file=str(events.path),
        expires_with_console=True,
        login_mode=server.auth.mode,
        pid=os.getpid(),
        start_ticks=process_stamp(os.getpid()),
    )
    threading.Thread(target=server.monitor, daemon=True).start()
    return server, url


def console_notifications(state, now=None):
    """Read recent private events for the existing watchdog delivery adapter.

    This function only produces messages. Hermes owns destinations and delivery.
    It never runs commands, reads clipboard text, or causes an automatic rejoin.
    """
    from live_transcript.core.config import get
    from live_transcript.runtime.console import process_stamp

    now = time.time() if now is None else now
    seen = state.setdefault("console_events", {})
    issues = {}
    root = Path(get("paths", "logs"))
    candidates = []
    for path in root.glob("console-*.events.jsonl"):
        try:
            details = path.stat()
            if (
                not path.is_symlink()
                and details.st_uid == os.getuid()
                and not details.st_mode & 0o077
            ):
                candidates.append((details.st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True)[:8]:
        try:
            if (
                path.is_symlink()
                or path.stat().st_uid != os.getuid()
                or path.stat().st_mode & 0o077
            ):
                continue
            if now - path.stat().st_mtime > 300:
                continue
            with path.open("rb") as stream:
                first = json.loads(stream.readline(16384))
                if (
                    first.get("kind") != "console_ready"
                    or process_stamp(first["pid"]) != first["start_ticks"]
                ):
                    continue
                stream.seek(max(0, path.stat().st_size - 65536))
                if stream.tell():
                    stream.readline()
                rows = []
                for line in stream:
                    if not line.endswith(b"\n"):
                        break  # The writer may still be completing this event.
                    if line.strip():
                        rows.append(json.loads(line))
            for event in rows:
                ident = event["id"]
                if ident in seen or now - event["utc"] > 300:
                    continue
                seen[ident] = event["utc"]
                kind = event["kind"]
                if kind == "console_ready":
                    issues["console-ready-" + ident] = "Private VM console: " + first["url"]
                elif event.get("level") == "warning" or kind in (
                    "vm_reconnected",
                    "capture_healthy",
                ):
                    issues["console-" + ident] = event["message"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    state["console_events"] = {key: at for key, at in seen.items() if now - at < 600}
    return issues
