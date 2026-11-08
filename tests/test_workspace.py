from copy import deepcopy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from live_transcript.core import config
from live_transcript.runtime import workspace, library
from live_transcript.runtime.operator import ConsoleAuth, EventFeed


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        values = deepcopy(config.DEFAULTS)
        values["paths"].update(
            root=str(self.root),
            state=str(self.root / "state"),
            logs=str(self.root / "logs"),
            recordings=str(self.root / "recordings"),
            safety_copies=str(self.root / "safety"),
        )
        self.patch = patch.object(config, "CONFIG", values)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.current = patch.object(workspace, "current_run", return_value={})
        self.current_mock = self.current.start()
        self.addCleanup(self.current.stop)
        auth = ConsoleAuth()
        self.manager = SimpleNamespace(
            auth=auth,
            password="",
            generation=0,
            target_generation=0,
            view_only=False,
            unreachable=False,
            free_bytes=100 * 1024**3,
            manual_until=0,
            port=None,
            target=None,
        )
        self.manager.status = lambda: dict(
            display=None, control=False, capture=None, speech=None, session=None
        )
        self.manager.control = Mock()
        self.manager.action = Mock()
        self.server = workspace.WorkspaceServer(
            ("127.0.0.1", 0), "127.0.0.1", self.manager, EventFeed(self.root / "events.jsonl")
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, data=None, cookie=None, origin=True, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Host": self.server.authority}
        if origin:
            headers["Origin"] = "http://" + self.server.authority
        if cookie:
            headers["Cookie"] = cookie
        if extra:
            headers.update(extra)
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def login(self):
        code, headers, _ = self.request("POST", "/api/session", dict(token=self.server.token))
        self.assertEqual(code, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_workspace_reads_the_same_idle_state_as_the_cli(self):
        self.current.stop()
        self.assertEqual(workspace.current_run()["status"], "idle")

    def test_public_https_origin_is_explicit_and_session_cookies_are_secure(self):
        self.server.external_origin = "https://" + self.server.authority
        code, headers, _ = self.request(
            "POST",
            "/api/session",
            dict(token=self.server.token),
            extra={"Origin": self.server.external_origin},
        )
        self.assertEqual(code, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])
        self.assertEqual(
            self.request("POST", "/api/session", dict(token=self.server.token))[0], 403
        )
        self.assertEqual(workspace.public_origin("https://example.com:443/"), "https://example.com")
        for value in (
            "https://example.com/path",
            "https://user:secret@example.com",
            "https://example.com/#token",
        ):
            with self.assertRaises(ValueError):
                workspace.public_origin(value)

    def test_idle_workspace_is_authenticated_and_available_without_a_viewer(self):
        self.assertEqual(self.request("GET", "/api/recordings")[0], 401)
        cookie = self.login()
        code, _, body = self.request("GET", "/api/status", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["workspace"]["viewer_ready"])
        self.assertTrue(
            json.loads(self.request("GET", "/api/session", cookie=cookie)[2])["persistent"]
        )

    def test_meeting_controls_validate_origin_invitation_and_exact_run(self):
        cookie = self.login()
        with (
            patch.object(
                workspace, "schedule", return_value=dict(id="fixture", status="queued")
            ) as schedule,
            patch("live_transcript.runtime.capacity.preflight", return_value={}),
        ):
            body = dict(url="https://meet.google.com/abc-abcd-efg", minutes=90, request_id="a" * 32)
            self.assertEqual(
                self.request("POST", "/api/meeting/start", body, cookie, origin=False)[0], 403
            )
            self.assertEqual(
                self.request(
                    "POST",
                    "/api/meeting/start",
                    dict(body, url="http://example.invalid/command"),
                    cookie,
                )[0],
                400,
            )
            self.assertEqual(self.request("POST", "/api/meeting/start", body, cookie)[0], 202)
            schedule.assert_called_once()
            self.current_mock.return_value = dict(run_id="new-meeting", status="active")
            self.assertEqual(
                self.request("POST", "/api/meeting/stop", dict(run_id="old-meeting"), cookie)[0],
                400,
            )
            self.assertEqual(
                self.request("POST", "/api/meeting/stop", dict(run_id="new-meeting"), cookie)[0],
                202,
            )
            self.assertEqual(schedule.call_args.args, ("stop", ["--run-id", "new-meeting"]))

    def test_submission_crosses_pid_namespaces_and_retries_keep_original_deadline(self):
        from live_transcript.runtime import application

        config.CONFIG["runtime"]["supervisor"] = "app"
        deadline = time.time() + 60
        with patch.object(application, "ready", return_value=False):
            first = workspace.schedule("launch", ["fixture"], key="repeat", deadline=deadline)
            again = workspace.schedule("launch", ["fixture"], key="repeat", deadline=deadline + 30)
        self.assertEqual(first, again)
        queue = application.Queue()
        self.addCleanup(queue.close)
        row = queue.row("repeat")
        self.assertEqual(row["deadline"], deadline)
        self.assertEqual(float(row["env"]["LT_DEADLINE"]), deadline)
        with self.assertRaises(ValueError):
            workspace.schedule("launch", ["different"], key="repeat", deadline=deadline)

    def test_native_submission_timeout_has_durable_idempotence_receipt(self):
        import subprocess

        config.CONFIG["runtime"]["supervisor"] = "systemd"
        config.CONFIG["_file"] = str(self.root / "config.toml")
        with patch.object(
            workspace.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemd-run", 10)
        ) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                workspace.schedule("launch", ["fixture"], key="repeat", deadline=time.time() + 60)
            result = workspace.schedule(
                "launch", ["fixture"], key="repeat", deadline=time.time() + 90
            )
            self.assertEqual(result["status"], "submission_pending")
            self.assertEqual(run.call_count, 1)

    def test_view_only_workspace_rejects_mutations(self):
        cookie = self.login()
        self.manager.view_only = True
        self.assertEqual(self.request("POST", "/api/control", dict(enabled=True), cookie)[0], 403)
        self.manager.control.assert_not_called()

    def test_recorded_audio_supports_authenticated_byte_ranges(self):
        root = self.root / "recordings"
        root.mkdir()
        with wave.open(str(root / "fixture.wav"), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x01" * 16000)
        library.scan()
        ident = library.listing()["recordings"][0]["id"]
        cookie = self.login()
        code, headers, body = self.request(
            "GET", f"/api/recordings/{ident}/audio", cookie=cookie, extra={"Range": "bytes=0-3"}
        )
        self.assertEqual(code, 206)
        self.assertEqual(body, b"RIFF")
        self.assertEqual(headers["Content-Range"], "bytes 0-3/32044")
        self.assertEqual(
            self.request(
                "GET",
                f"/api/recordings/{ident}/audio",
                cookie=cookie,
                extra={"Range": "bytes=999999-"},
            )[0],
            416,
        )
        self.assertEqual(self.request("GET", f"/api/recordings/{ident}/audio")[0], 401)

    def test_manual_control_cannot_cross_desktop_generations(self):
        manager = workspace.Manager.__new__(workspace.Manager)
        manager.lock = threading.RLock()
        manager.view_only = False
        manager.target = {"display": ":99"}
        manager.target_generation = "4" * 32
        manager.manual_until = 0
        with self.assertRaises(ValueError):
            manager.control(True, "3" * 32)
        self.assertEqual(manager.manual_until, 0)
        manager.control(True, "4" * 32)
        self.assertGreater(manager.manual_until, time.time())

    def test_manual_control_is_not_inferred_from_another_console_lease(self):
        manager = workspace.Manager.__new__(workspace.Manager)
        manager.lock = threading.RLock()
        manager.snapshot = dict(control=True)
        manager.updated = time.time()
        manager.view_only = False
        manager.manual_until = time.time() - 1
        manager.password = "viewer"
        manager.viewer_control = True
        self.assertFalse(manager.status()["control"])
