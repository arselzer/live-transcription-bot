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
            with patch.object(session, "HOME", home), patch.object(
                session.subprocess, "run"
            ) as run:
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
                archive="/home/scribe/rec/tok-1.archive",
                joined=True,
            )
            state = {}
            with patch.object(watchdog, "MJSON", mjson), patch.object(
                watchdog, "probe", return_value=remote
            ), patch.object(watchdog, "pending_alive", return_value=False), patch.object(
                watchdog, "decision", return_value="wait"
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
                archive="/home/scribe/rec/tok-1.archive",
                joined=True,
            )
            state = {}
            with patch.object(watchdog, "MJSON", mjson), patch.object(
                watchdog, "probe", return_value=remote
            ), patch.object(watchdog, "pending_alive", return_value=False), patch.object(
                watchdog, "decision", return_value="wait"
            ):
                self.assertNotIn("recording-started", watchdog.tick(state, True))
                self.assertIn("recording-started", watchdog.tick(state, False))


class AckAfterPrintTests(unittest.TestCase):
    def test_main_acks_notifications_after_print(self):
        from live_transcript.runtime import watchdog
        from live_transcript.integrations import notifications

        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "watchdog.json"
            report = dict(
                version=1,
                dry_run=False,
                issues={},
                messages=["Live Transcript [x]: y"],
                notifications=[dict(id="a" * 64, message="Live Transcript [x]: y")],
                pending_count=1,
                delivery="unacknowledged",
            )
            with patch.object(watchdog, "STATE", state_path), patch.object(
                watchdog, "ensure_backend", return_value={}
            ), patch.object(watchdog, "tick", return_value={}), patch.object(
                watchdog, "recovery_issues", return_value={}
            ), patch.object(
                watchdog, "deliver", return_value=report
            ), patch(
                "live_transcript.runtime.operator.console_notifications", return_value={}
            ), patch.object(
                notifications, "acknowledge"
            ) as ack:
                watchdog.main(["--json", "--ack-after-print"])
                ack.assert_called_once_with(["a" * 64])

    def test_no_flag_leaves_ledger_untouched(self):
        from live_transcript.runtime import watchdog
        from live_transcript.integrations import notifications

        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "watchdog.json"
            report = dict(
                version=1,
                dry_run=False,
                issues={},
                messages=[],
                notifications=[dict(id="b" * 64, message="m")],
                pending_count=1,
                delivery="unacknowledged",
            )
            with patch.object(watchdog, "STATE", state_path), patch.object(
                watchdog, "ensure_backend", return_value={}
            ), patch.object(watchdog, "tick", return_value={}), patch.object(
                watchdog, "recovery_issues", return_value={}
            ), patch.object(
                watchdog, "deliver", return_value=report
            ), patch(
                "live_transcript.runtime.operator.console_notifications", return_value={}
            ), patch.object(
                notifications, "acknowledge"
            ) as ack:
                watchdog.main(["--json"])
                ack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
