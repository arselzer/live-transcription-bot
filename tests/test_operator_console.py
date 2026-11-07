"""Console authentication, clipboard boundaries, and capture-independent recovery."""

import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from live_transcript.runtime.console import guest_action, viewer_url, read_handshake, start_desktop
from live_transcript.runtime.operator import EventFeed, OperatorServer
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

    def test_invalid_capability_host_and_origin_are_rejected(self):
        self.assertEqual(self.request("POST", "/api/session", dict(token="wrong"))[0], 401)
        self.assertEqual(self.request("POST", "/api/session", dict(token="你好"))[0], 401)
        self.assertEqual(
            self.request("POST", "/api/session", dict(token=self.server.token), origin=False)[0],
            403,
        )
        self.assertEqual(self.request("GET", "/", host="attacker.invalid")[0], 403)
        self.bridge.call.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
