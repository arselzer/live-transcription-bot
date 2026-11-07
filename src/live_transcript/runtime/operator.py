"""Authenticated VM console, fixed-upstream noVNC proxy, and private event feed.

Only trusted VPN/loopback exposure is supported directly. Terminate TLS upstream
for untrusted networks. Guest actions use pinned SSH and a pinned Xvfb process;
neither HTTP clients nor clipboard text can select a command or target host.
"""

from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import secrets
import select
import shlex
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlencode


MAX_BODY = 96 * 1024


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

    def __init__(self, address, upstream, bridge, events, advertised_host):
        self.upstream = ("127.0.0.1", upstream)
        self.bridge, self.events = bridge, events
        self.token = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
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
            return "lt_console" in cookie and secrets.compare_digest(
                cookie["lt_console"].value, self.server.cookie
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
                response = b'{"authenticated":true}'
                self.send_response(200)
                self.headers_safe()
                self.send_header(
                    "Set-Cookie",
                    f"lt_console={self.server.cookie}; Path=/; HttpOnly; SameSite=Strict",
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
        if not self.authenticated():
            return self.reply(401, dict(error="Console session expired; reopen its link"))
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

    def proxy_websocket(self):
        if urlsplit(self.path).path != "/novnc/websockify":
            return self.reply(404, dict(error="Unknown viewer socket"))
        self.close_connection = True
        try:
            with socket.create_connection(self.server.upstream, timeout=5) as upstream:
                allowed = (
                    "Upgrade",
                    "Connection",
                    "Sec-WebSocket-Key",
                    "Sec-WebSocket-Version",
                    "Sec-WebSocket-Protocol",
                )
                lines = ["GET /websockify HTTP/1.1", "Host: 127.0.0.1"]
                lines += [f"{key}: {self.headers[key]}" for key in allowed if self.headers.get(key)]
                upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
                self.wfile.flush()
                self.connection.settimeout(10)
                upstream.settimeout(10)
                while not self.server.monitor_stop.is_set():
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


def launch_ui(listen, port, advertised, upstream, bridge, event_path, password, emit=None):
    from live_transcript.runtime.console import process_stamp

    events = EventFeed(event_path, emit)
    server = OperatorServer((listen, port), upstream, bridge, events, advertised)
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
