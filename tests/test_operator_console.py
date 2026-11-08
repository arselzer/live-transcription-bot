"""Console authentication, clipboard boundaries, and capture-independent recovery."""

import http.client
import http.cookiejar
import json
import os
import socket
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from live_transcript.runtime.console import guest_action, viewer_url, read_handshake, start_desktop
from live_transcript.runtime.operator import ConsoleAuth, ConsoleHub, EventFeed, OperatorServer
from live_transcript.runtime.operator import console_notifications


class ConsoleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = EventFeed(Path(self.temp.name) / "events.jsonl")
        self.bridge = Mock()
        self.bridge.call.return_value = dict(display=":88", control=True, capture=None)
        self.server = OperatorServer(("127.0.0.1", 0), 1, self.bridge, self.events, "127.0.0.1")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, data=None, cookie=None, origin=True, host=None, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Host": host or self.server.authority}
        if origin:
            headers["Origin"] = "http://" + self.server.authority
        if cookie:
            headers["Cookie"] = cookie
        if data is not None:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update(extra)
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def login(self):
        code, headers, _ = self.request("POST", "/api/session", dict(token=self.server.token))
        self.assertEqual(code, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_private_api_and_viewer_require_session(self):
        for path in ("/api/status", "/api/events", "/novnc/vnc.html", "/novnc/websockify"):
            self.assertEqual(self.request("GET", path)[0], 401)
        self.bridge.call.assert_not_called()

    def test_two_vm_consoles_keep_independent_logins_in_one_browser_cookie_jar(self):
        second = OperatorServer(
            ("127.0.0.1", 0),
            1,
            self.bridge,
            EventFeed(Path(self.temp.name) / "second.events.jsonl"),
            "127.0.0.1",
        )
        thread = threading.Thread(target=second.serve_forever, daemon=True)
        thread.start()
        jar = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            for server in (self.server, second):
                origin = "http://" + server.authority
                request = urllib.request.Request(
                    origin + "/api/session",
                    data=json.dumps(dict(token=server.token)).encode(),
                    headers={"Content-Type": "application/json", "Origin": origin},
                )
                with browser.open(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
            self.assertNotEqual(self.server.cookie_name, second.cookie_name)
            self.assertEqual(len(jar), 2)
            for server in (self.server, second, self.server):
                with browser.open(
                    "http://" + server.authority + "/api/status", timeout=3
                ) as response:
                    self.assertEqual(response.status, 200)
        finally:
            second.shutdown()
            second.server_close()

    def test_invalid_capability_host_and_origin_are_rejected(self):
        self.assertEqual(self.request("POST", "/api/session", dict(token="wrong"))[0], 401)
        self.assertEqual(self.request("POST", "/api/session", dict(token="你好"))[0], 401)
        self.assertEqual(
            self.request("POST", "/api/session", dict(token=self.server.token), origin=False)[0],
            403,
        )
        self.assertEqual(self.request("GET", "/", host="attacker.invalid")[0], 403)
        self.bridge.call.assert_not_called()

    def test_shared_login_opens_another_vm_without_its_token_or_password(self):
        file = Path(self.temp.name) / "shared/login.json"
        a, b = ConsoleAuth("shared", file), ConsoleAuth("shared", file)
        self.server.auth = a
        self.server.token, self.server.cookie_name = a.token, a.cookie_name
        second = OperatorServer(
            ("127.0.0.1", 0),
            1,
            self.bridge,
            EventFeed(Path(self.temp.name) / "shared.events.jsonl"),
            "127.0.0.1",
            auth=b,
            viewer_password="second-vnc",
        )
        threading.Thread(target=second.serve_forever, daemon=True).start()
        browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        try:
            origin = "http://" + self.server.authority
            request = urllib.request.Request(
                origin + "/api/session",
                data=json.dumps(dict(token=a.token)).encode(),
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            with browser.open(request, timeout=3) as response:
                self.assertTrue(json.load(response)["authenticated"])
            with browser.open("http://" + second.authority + "/api/session", timeout=3) as response:
                data = json.load(response)
                self.assertTrue(data["authenticated"])
                self.assertEqual(data["viewer_password"], "second-vnc")
            with browser.open("http://" + second.authority + "/api/status", timeout=3) as response:
                self.assertEqual(response.status, 200)
        finally:
            second.shutdown()
            second.server_close()

    def test_session_probe_keeps_vnc_password_private_until_login(self):
        self.server.viewer_password = "private-vnc-fixture"
        code, _, body = self.request("GET", "/api/session")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), dict(authenticated=False))

    def test_paste_is_forwarded_without_persisting_its_contents(self):
        cookie = self.login()
        text = "private clipboard fixture $(ignored) `ignored`\nsecond line"
        self.assertEqual(
            self.request("POST", "/api/action", dict(op="paste", text=text), cookie)[0], 200
        )
        self.bridge.call.assert_called_once_with(dict(op="paste", text=text))
        saved = self.events.path.read_text()
        self.assertNotIn(text, saved)
        self.assertEqual(json.loads(saved)["bytes"], len(text.encode()))
        self.assertEqual(self.events.path.stat().st_mode & 0o777, 0o600)

    def test_paste_limits_shortcut_allowlist_and_csrf(self):
        cookie = self.login()
        for data in (
            dict(op="paste", text="x" * 16385),
            dict(op="paste", text="\0"),
            dict(op="paste", text=""),
            dict(op="key", key="Return"),
            dict(op="command", command="unsafe"),
        ):
            self.assertEqual(self.request("POST", "/api/action", data, cookie)[0], 400)
        self.assertEqual(
            self.request("POST", "/api/action", dict(op="key", key="Escape"), cookie, origin=False)[
                0
            ],
            403,
        )
        self.bridge.call.assert_not_called()

    def test_status_failure_never_launches_recovery_or_stops_capture(self):
        cookie = self.login()
        self.bridge.call.side_effect = RuntimeError("private diagnostic")
        code, _, body = self.request("GET", "/api/status", cookie=cookie)
        self.assertEqual(code, 503)
        self.assertNotIn(b"private diagnostic", body)
        self.bridge.call.assert_called_once_with(dict(op="status"))

    def test_websocket_requires_same_origin_even_with_cookie(self):
        cookie = self.login()
        self.assertEqual(
            self.request(
                "GET",
                "/novnc/websockify",
                cookie=cookie,
                origin=False,
                extra={"Upgrade": "websocket"},
            )[0],
            403,
        )

    def test_traversal_never_reaches_upstream(self):
        cookie = self.login()
        for path in ("/novnc/../secret", "/novnc/%2e%2e/secret", "/novnc/dir\\secret"):
            self.assertEqual(self.request("GET", path, cookie=cookie)[0], 400)

    def test_monitor_reports_transitions_without_duplicate_healthy_events(self):
        self.bridge.call.return_value = dict(
            capture=dict(age=1, audio=dict(running=True), screen=dict(running=True))
        )
        self.server.status()
        self.server.cached_status = None
        self.server.status()
        self.assertEqual([e["kind"] for e in self.events.recent()], ["capture_healthy"])
        self.server.cached_status = None
        self.bridge.call.return_value = dict(capture=dict(age=21))
        self.server.status()
        self.assertEqual(self.events.recent()[-1]["kind"], "capture_attention")

    def test_operator_events_never_include_request_content(self):
        cookie = self.login()
        self.request("POST", "/api/interaction", dict(text="not recorded"), cookie)
        self.request("POST", "/api/interaction", {}, cookie)
        self.assertEqual(len(self.events.recent()), 1)
        self.assertNotIn("not recorded", self.events.path.read_text())

    def test_prejoin_has_no_false_capture_alarm_but_lost_capture_does(self):
        self.server.status()
        self.assertEqual(self.events.recent()[-1]["kind"], "capture_pending")
        self.assertEqual(self.events.recent()[-1]["level"], "info")
        self.server.cached_status = None
        self.bridge.call.return_value = dict(
            capture=dict(age=1, audio=dict(running=True), screen=dict(running=True))
        )
        self.server.status()
        self.server.cached_status = None
        self.bridge.call.return_value = dict(capture=None)
        self.server.status()
        self.assertEqual(self.events.recent()[-1]["kind"], "capture_attention")


class GuestControlTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)

    def test_url_credentials_live_only_in_fragment(self):
        url = urlsplit(viewer_url("console.example", 6080, "test password"))
        self.assertEqual(url.query, "")
        self.assertIn("password=test+password", url.fragment)
        for host in ("bad/host", "host#fragment", "user@host", "host\nheader"):
            with self.assertRaises(ValueError):
                viewer_url(host, 6080, "test")

    def test_partial_handshake_times_out_instead_of_blocking_on_readline(self):
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as stream, os.fdopen(writer, "wb", buffering=0) as output:
            output.write(b'{"token":')
            with self.assertRaises(TimeoutError):
                read_handshake(stream, timeout=0.01)

    def test_handshake_handles_eof_and_a_complete_reply(self):
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as stream:
            os.write(writer, b'{"token":"fixture"}\n')
            os.close(writer)
            self.assertEqual(read_handshake(stream), dict(token="fixture"))
        reader, writer = os.pipe()
        os.close(writer)
        with os.fdopen(reader, "rb") as stream:
            with self.assertRaises(RuntimeError):
                read_handshake(stream)

    def test_idle_browser_is_owned_by_independent_bounded_service(self):
        target = dict(display=":88", pid=1, start_ticks="fixture", auth="fixture")
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            (logs / ("desktop-" + "a" * 32 + ".json")).write_text(json.dumps(dict(target=target)))
            with (
                patch("subprocess.run") as run,
                patch("live_transcript.runtime.console.displays", return_value=[target]),
            ):
                self.assertEqual(start_desktop("a" * 32, 30, logs), target)
                args = run.call_args.args[0]
                self.assertIn("--unit=live-transcript-desktop-" + "a" * 32, args)
                self.assertIn("--property=RuntimeMaxSec=1830", args)
                self.assertIn("desktop", args)

    def test_reused_display_and_expired_control_are_rejected_before_subprocess(self):
        target = dict(display=":88", pid=1, start_ticks="old", auth="fixture")
        with (
            patch(
                "live_transcript.runtime.console.displays",
                return_value=[dict(target, start_ticks="new")],
            ),
            patch("subprocess.run") as run,
        ):
            with self.assertRaises(ValueError):
                guest_action(dict(op="paste", text="hello", target=target))
            run.assert_not_called()
        with (
            patch("live_transcript.runtime.console.displays", return_value=[target]),
            patch("live_transcript.runtime.console.operator_control_active", return_value=False),
            patch("subprocess.run") as run,
        ):
            with self.assertRaises(ValueError):
                guest_action(dict(op="key", key="Escape", target=target))
            run.assert_not_called()

    def test_paste_goes_through_stdin_and_never_sends_enter(self):
        target = dict(display=":88", pid=1, start_ticks="old", auth="fixture")
        process = Mock()
        process.poll.return_value = None
        with (
            patch("live_transcript.runtime.console.displays", return_value=[target]),
            patch("live_transcript.runtime.console.operator_control_active", return_value=True),
            patch("subprocess.Popen", return_value=process) as popen,
            patch("subprocess.run") as run,
            patch("time.sleep"),
        ):
            guest_action(dict(op="paste", text="$(literal)\nprivate", target=target))
        process.stdin.write.assert_called_once_with(b"$(literal)\nprivate")
        self.assertNotIn("private", repr(popen.call_args))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["xdotool", "key", "--clearmodifiers", "ctrl+v"])
        process.terminate.assert_called_once()


class HermesConsoleTests(unittest.TestCase):
    def test_active_events_are_deduplicated_and_interactions_stay_silent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "console-fixture.events.jsonl"
            feed = EventFeed(path)
            feed.add(
                "console_ready",
                "Ready",
                url="http://console.example/#token=fixture",
                pid=1,
                start_ticks="fixture",
            )
            feed.add("operator_interaction", "Human action")
            feed.add("vm_unreachable", "Unknown VM status", level="warning")
            state = {}
            with (
                patch("live_transcript.core.config.get", return_value=temporary),
                patch("live_transcript.runtime.console.process_stamp", return_value="fixture"),
            ):
                first = console_notifications(state)
                self.assertEqual(len(first), 2)
                self.assertTrue(any("#token=fixture" in text for text in first.values()))
                self.assertEqual(console_notifications(state), {})
                self.assertFalse(any("Human action" in text for text in first.values()))

    def test_dead_owner_or_world_readable_file_cannot_announce_a_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "console-fixture.events.jsonl"
            EventFeed(path).add(
                "console_ready",
                "Ready",
                url="http://console.example/#token=fixture",
                pid=1,
                start_ticks="old",
            )
            with (
                patch("live_transcript.core.config.get", return_value=temporary),
                patch("live_transcript.runtime.console.process_stamp", return_value="new"),
            ):
                self.assertEqual(console_notifications({}), {})
            path.chmod(0o644)
            with (
                patch("live_transcript.core.config.get", return_value=temporary),
                patch("live_transcript.runtime.console.process_stamp", return_value="old"),
            ):
                self.assertEqual(console_notifications({}), {})

    def test_incomplete_trailing_event_does_not_drop_valid_earlier_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "console-fixture.events.jsonl"
            EventFeed(path).add(
                "console_ready",
                "Ready",
                url="http://console.example/#token=fixture",
                pid=1,
                start_ticks="fixture",
            )
            with path.open("ab") as stream:
                stream.write(b'{"id":')
            with (
                patch("live_transcript.core.config.get", return_value=temporary),
                patch("live_transcript.runtime.console.process_stamp", return_value="fixture"),
            ):
                self.assertEqual(len(console_notifications({})), 1)


class ConsoleAuthTests(unittest.TestCase):
    def test_shared_credentials_persist_and_other_groups_reject_the_session(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "group.json"
            a, b = ConsoleAuth("shared", file), ConsoleAuth("shared", file)
            other = ConsoleAuth("shared", Path(directory) / "other.json")
            self.assertEqual(a.token, b.token)
            self.assertEqual(a.cookie_name, b.cookie_name)
            self.assertTrue(b.accepts(a.issue()))
            self.assertFalse(other.accepts(a.issue()))
            self.assertFalse(ConsoleAuth().accepts(a.issue()))
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_sessions_expire_and_modified_signatures_are_rejected(self):
        auth = ConsoleAuth(session_seconds=60)
        with patch("time.time", return_value=1000):
            cookie = auth.issue()
            self.assertTrue(auth.accepts(cookie))
            self.assertFalse(auth.accepts(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")))
        with patch("time.time", return_value=1060):
            self.assertFalse(auth.accepts(cookie))
        with patch("time.time", return_value=900):
            self.assertFalse(auth.accepts(cookie))
        for value in ("", "bad", "你好", None):
            self.assertFalse(auth.accepts(value))

    def test_unsafe_credentials_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "login.json"
            file.write_text("private fixture")
            file.chmod(0o644)
            with self.assertRaises(ValueError):
                ConsoleAuth("shared", file)
            self.assertEqual(file.read_text(), "private fixture")
            file.chmod(0o600)
            with self.assertRaises(ValueError):
                ConsoleAuth("shared", file)
            link = Path(directory) / "link.json"
            link.symlink_to(file)
            with self.assertRaises(OSError):
                ConsoleAuth("shared", link)
            fifo = Path(directory) / "fifo.json"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                ConsoleAuth("shared", fifo)

    def test_concurrent_group_creation_uses_one_set_of_credentials(self):
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "login.json"
            with ThreadPoolExecutor(max_workers=4) as pool:
                logins = list(pool.map(lambda _: ConsoleAuth("shared", file), range(8)))
            self.assertEqual(len({a.token for a in logins}), 1)
            self.assertTrue(all(a.accepts(logins[0].issue()) for a in logins))

    def test_rotating_the_private_file_invalidates_old_sessions_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "login.json"
            old = ConsoleAuth("shared", file)
            cookie = old.issue()
            file.unlink()
            new = ConsoleAuth("shared", file)
            self.assertNotEqual(old.token, new.token)
            self.assertFalse(new.accepts(cookie))


class ConsoleHubTests(unittest.TestCase):
    request = ConsoleHTTPTests.request
    login = ConsoleHTTPTests.login

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = ConsoleAuth("shared", Path(self.temp.name) / "login.json")
        self.backends = []
        self.bridges = []
        for index in range(2):
            bridge = Mock()
            bridge.call.return_value = dict(display=f":{70 + index}", control=True, capture=None)
            backend = OperatorServer(
                ("127.0.0.1", 0),
                1,
                bridge,
                EventFeed(Path(self.temp.name) / f"events-{index}.jsonl"),
                "127.0.0.1",
                auth=self.auth,
                viewer_password=f"password-{index}",
            )
            threading.Thread(target=backend.serve_forever, daemon=True).start()
            self.backends.append(backend)
            self.bridges.append(bridge)
        self.server = ConsoleHub(
            ("127.0.0.1", 0),
            "127.0.0.1",
            [f"first={self.backends[0].server_port}", f"second={self.backends[1].server_port}"],
            self.auth,
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in [self.server, *self.backends]:
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def test_single_login_selects_both_consoles_and_their_own_passwords(self):
        self.assertEqual(self.request("GET", "/api/consoles")[0], 401)
        self.assertEqual(self.request("GET", "/vm/first/api/status")[0], 401)
        cookie = self.login()
        code, _, body = self.request("GET", "/api/session", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual([item["id"] for item in json.loads(body)["consoles"]], ["first", "second"])
        for index, ident in enumerate(("first", "second")):
            code, _, body = self.request("GET", f"/vm/{ident}/api/session", cookie=cookie)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["viewer_password"], f"password-{index}")
            code, _, body = self.request("GET", f"/vm/{ident}/api/status", cookie=cookie)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["display"], f":{70 + index}")

    def test_paste_targets_only_selected_vm_and_origin_is_checked(self):
        cookie = self.login()
        code, _, _ = self.request(
            "POST", "/vm/second/api/action", dict(op="paste", text="hello"), cookie
        )
        self.assertEqual(code, 200)
        self.bridges[1].call.assert_called_once_with(dict(op="paste", text="hello"))
        self.bridges[0].call.assert_not_called()
        code, _, _ = self.request(
            "POST", "/vm/first/api/action", dict(op="key", key="Tab"), cookie, origin=False
        )
        self.assertEqual(code, 403)
        self.bridges[0].call.assert_not_called()

    def test_unknown_targets_and_proxy_paths_are_rejected(self):
        cookie = self.login()
        for path in (
            "/vm/unknown/api/status",
            "/vm/first/api/config",
            "/vm/first/novnc/../api/status",
            "/vm/first/novnc/%2e%2e/",
            "/vm/first/http://example.com",
        ):
            self.assertEqual(self.request("GET", path, cookie=cookie)[0], 404)
        self.assertEqual(
            self.request(
                "GET",
                "/vm/first/novnc/websockify",
                cookie=cookie,
                origin=False,
                extra={"Upgrade": "websocket"},
            )[0],
            403,
        )

    def test_mismatched_auth_and_offline_console_do_not_block_other_vm(self):
        self.backends[1].auth = ConsoleAuth()
        code, _, body = self.request("GET", "/vm/second/api/status", cookie=self.login())
        self.assertEqual(code, 503)
        self.assertIn(b"same shared login", body)
        self.backends[1].shutdown()
        self.backends[1].server_close()
        cookie = self.login()
        self.assertEqual(self.request("GET", "/vm/second/api/status", cookie=cookie)[0], 502)
        self.assertEqual(self.request("GET", "/vm/first/api/status", cookie=cookie)[0], 200)

    def test_hub_websocket_reaches_selected_authenticated_console(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
            self.backends[1].upstream = listener.getsockname()
            received = []

            def echo():
                with listener.accept()[0] as connection:
                    connection.settimeout(5)
                    header = b""
                    while b"\r\n\r\n" not in header:
                        header += connection.recv(4096)
                    received.append(header)
                    connection.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                    )
                    connection.sendall(connection.recv(32))

            thread = threading.Thread(target=echo, daemon=True)
            thread.start()
            cookie = self.login()
            with socket.create_connection(self.server.server_address, timeout=5) as client:
                client.sendall(
                    (
                        f"GET /vm/second/novnc/websockify HTTP/1.1\r\nHost: {self.server.authority}\r\nOrigin: http://{self.server.authority}\r\nCookie: {cookie}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                    ).encode()
                )
                response = b""
                while b"\r\n\r\n" not in response:
                    response += client.recv(4096)
                self.assertTrue(response.startswith(b"HTTP/1.1 101"))
                client.sendall(b"vm-specific-data")
                self.assertEqual(client.recv(32), b"vm-specific-data")
            thread.join(timeout=5)
            self.assertEqual(received[0].splitlines()[0], b"GET /websockify HTTP/1.1")
            self.bridges[0].call.assert_not_called()

    def test_hub_rejects_ambiguous_or_arbitrary_upstreams(self):
        for entries in (
            ["bad/name=6001"],
            ["first=0"],
            ["first=65536"],
            ["first=http://example.com"],
            ["same=6001", "same=6002"],
            ["one=6001", "two=6001"],
            ["one=6080"],
        ):
            with self.assertRaises(ValueError):
                ConsoleHub(("127.0.0.1", 6080), "127.0.0.1", entries, self.auth)
        with self.assertRaises(ValueError):
            ConsoleHub(("127.0.0.1", 0), "127.0.0.1", ["one=6001"], ConsoleAuth())


if __name__ == "__main__":
    unittest.main()
