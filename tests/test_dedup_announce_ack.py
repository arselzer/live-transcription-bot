import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class DedupTests(unittest.TestCase):
    def test_consecutive_repetition_bleed_collapses_once(self):
        from live_transcript.transcription.dedup import collapse

        utterances = [
            {"t": 1.0, "text": "Okay, also daher kommt es."},
            {"t": 1.0, "text": " Okay also daher kommt es"},
            {"t": 2.0, "text": "Okay, also daher kommt es!"},
            {"t": 9.0, "text": "Und die IMDB nutzt ihr auch."},
            {"t": 20.0, "text": "Okay, also daher kommt es."},
        ]
        kept, dropped = collapse(utterances)
        self.assertEqual([u["t"] for u in kept], [1.0, 9.0, 20.0])
        self.assertEqual(len(dropped), 2)

    def test_distinct_speech_survives(self):
        from live_transcript.transcription.dedup import collapse

        utterances = [
            {"t": 1.0, "text": "Wie machen wir das mit dem Demo?"},
            {"t": 4.0, "text": "Das Demo läuft über die Live-Dashboards."},
        ]
        kept, dropped = collapse(utterances)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])


class StatusJoinedTests(unittest.TestCase):
    def test_status_reports_joined_marker(self):
        from live_transcript.runtime import session

        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            token = "20261108-000000-abcdef"
            marker = home / "logs" / ("session-" + token + ".joined.json")
            with (
                patch.object(session, "HOME", home),
                patch.object(session.subprocess, "run") as run,
            ):
                run.return_value = type("R", (), {"stdout": "active\n"})()
                self.assertFalse(session.status(token)["joined"])
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps(dict(utc=time.time(), provider="proton")))
                self.assertTrue(session.status(token)["joined"])


class AnnounceTests(unittest.TestCase):
    def _meeting(self, path):
        path.write_text(
            json.dumps(
                dict(
                    status="active",
                    run_id="tok-1",
                    url="https://meet.proton.me/join/id-X#pwd-secretfragment",
                    started=time.time(),
                    deadline=time.time() + 3600,
                    pid=1,
                    control_start_ticks="0",
                )
            )
        )

    def test_recording_started_announces_exactly_once_without_secret(self):
        from live_transcript.runtime import watchdog

        with tempfile.TemporaryDirectory() as d:
            mjson = Path(d) / "meeting.json"
            self._meeting(mjson)
            remote = dict(
                phase="running",
                archive="/home/transcript/rec/tok-1.archive",
                joined=True,
            )
            state = {}
            with (
                patch.object(watchdog, "MJSON", mjson),
                patch.object(watchdog, "probe", return_value=remote),
                patch.object(watchdog, "pending_alive", return_value=False),
                patch.object(watchdog, "decision", return_value="wait"),
            ):
                issues = watchdog.tick(state)
                self.assertIn("recording-started", issues)
                self.assertNotIn("pwd-secretfragment", issues["recording-started"])
                self.assertIn("https://meet.proton.me/join/id-X", issues["recording-started"])
                self.assertIn("run tok-1", issues["recording-started"])
                issues = watchdog.tick(state)
                self.assertNotIn("recording-started", issues)

    def test_dry_run_does_not_consume_the_announcement(self):
        from live_transcript.runtime import watchdog

        with tempfile.TemporaryDirectory() as d:
            mjson = Path(d) / "meeting.json"
            self._meeting(mjson)
            remote = dict(
                phase="running",
                archive="/home/transcript/rec/tok-1.archive",
                joined=True,
            )
            state = {}
            with (
                patch.object(watchdog, "MJSON", mjson),
                patch.object(watchdog, "probe", return_value=remote),
                patch.object(watchdog, "pending_alive", return_value=False),
                patch.object(watchdog, "decision", return_value="wait"),
            ):
                self.assertNotIn("recording-started", watchdog.tick(state, True))
                self.assertIn("recording-started", watchdog.tick(state, False))


class AckAfterPrintTests(unittest.TestCase):
    def run_watchdog(self, arguments, path, *, broken_output=False):
        import io
        from contextlib import redirect_stdout
        from live_transcript.runtime import watchdog

        output = io.StringIO()
        with (
            patch.object(watchdog, "STATE", path),
            patch.object(watchdog, "ensure_backend", return_value={}),
            patch.object(watchdog, "tick", return_value={"fixture": "A test incident"}),
            patch.object(watchdog, "recovery_issues", return_value={}),
            patch("live_transcript.runtime.operator.console_notifications", return_value={}),
            redirect_stdout(output),
        ):
            if broken_output:
                with patch("builtins.print", side_effect=BrokenPipeError):
                    with self.assertRaises(BrokenPipeError):
                        watchdog.main(arguments)
            else:
                watchdog.main(arguments)
        return json.loads(output.getvalue()) if output.getvalue() else None

    def test_main_acks_notifications_after_print(self):
        from live_transcript.integrations.notifications import load_state

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchdog.json"
            report = self.run_watchdog(["--json", "--ack-after-print"], path)
            state = load_state(path)
            self.assertEqual(state["notifications"], {})
            identifier = report["notifications"][0]["id"]
            self.assertEqual(len(identifier), 32)
            self.assertIn(identifier, state["notification_acks"])

    def test_no_flag_leaves_ledger_untouched(self):
        from live_transcript.integrations.notifications import load_state

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchdog.json"
            report = self.run_watchdog(["--json"], path)
            state = load_state(path)
            self.assertIn(report["notifications"][0]["id"], state["notifications"])
            self.assertFalse(state.get("notification_acks"))

    def test_failed_print_preserves_pending_notifications_even_with_ack_flag(self):
        from live_transcript.integrations.notifications import load_state

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchdog.json"
            self.run_watchdog(["--json", "--ack-after-print"], path, broken_output=True)
            state = load_state(path)
            self.assertEqual(len(state["notifications"]), 1)
            self.assertFalse(state.get("notification_acks"))


class EdgeDeliveryTests(unittest.TestCase):
    def setUp(self):
        from live_transcript.runtime import watchdog

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = patch.object(watchdog, "STATE", Path(temporary.name) / "watchdog.json")
        state.start()
        self.addCleanup(state.stop)

    def test_steady_condition_announces_once_not_every_cooldown(self):
        from live_transcript.runtime.watchdog import deliver

        state = {}
        issues = {"speech-backend-primary": "held by systemd conditions"}
        first = deliver(state, issues, False, ["speech-backend-primary"])
        self.assertEqual(len(first["messages"]), 1)
        # Simulate the cooldown having fully expired while nothing changed.
        state["issues"]["speech-backend-primary"]["at"] -= 10_000
        again = deliver(state, issues, False, ["speech-backend-primary"])
        self.assertEqual(again["messages"], [])
        # Text change is a new edge and announces again.
        changed = deliver(
            state, {"speech-backend-primary": "start rejected"}, False, ["speech-backend-primary"]
        )
        self.assertEqual(len(changed["messages"]), 1)
        # Cleared condition resets the cursor; recurrence re-announces.
        cleared = deliver(state, {}, False, ["speech-backend-primary"])
        self.assertEqual(cleared["messages"], [])
        self.assertEqual(state["edge"], {})
        recurrence = deliver(
            state, {"speech-backend-primary": "start rejected"}, False, ["speech-backend-primary"]
        )
        self.assertEqual(len(recurrence["messages"]), 1)

    def test_incident_keys_still_repeat_after_cooldown(self):
        from live_transcript.runtime import watchdog

        state = {}
        issues = {"vm-unreachable": "virsh domain not running"}
        first = watchdog.deliver(state, issues, False)
        self.assertEqual(len(first["messages"]), 1)
        state["issues"]["vm-unreachable"]["at"] -= watchdog.COOLDOWN + 1
        again = watchdog.deliver(state, issues, False)
        self.assertEqual(len(again["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
