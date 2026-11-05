import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
import fcntl
import wave
import live_transcript.transcription.daemon as captiond
from unittest.mock import MagicMock, patch

import live_transcript.storage.mirror as backup
import live_transcript.integrations.calendar_dispatch as dispatch
import live_transcript.integrations.calendar as calendar
from live_transcript.providers.zoom import joined_controls


class ReadinessTests(unittest.TestCase):
    def test_high_no_speech_score_does_not_erase_confident_voiced_clause(self):
        self.assertTrue(self.filtered_segment(b"\x01\x08" * 16000, -0.04, 0.68))
        self.assertFalse(self.filtered_segment(b"\0\0" * 16000, -0.04, 0.68))
        self.assertFalse(self.filtered_segment(b"\x01\x08" * 16000, -0.45, 0.68))
        self.assertFalse(self.filtered_segment(b"\x01\x08" * 16000, -0.96, 0.1))

    def filtered_segment(self, pcm, confidence, no_speech):
        path = self.root / "confidence.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        body = json.dumps(
            dict(
                segments=[
                    dict(
                        start=0,
                        end=1,
                        text="Bitte stellt sicher, dass die Untertitel gut lesbar sind.",
                        avg_logprob=confidence,
                        no_speech_prob=no_speech,
                    )
                ]
            )
        ).encode()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = body
        with patch.object(captiond.urllib.request, "urlopen", return_value=response):
            return captiond.stt_server(str(path), "de")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def event(self):
        return dict(
            uid="event",
            summary="Fixture meeting",
            start=dt.datetime(2030, 1, 15, 11, tzinfo=calendar.ZoneInfo("Europe/Vienna")),
            end=dt.datetime(2030, 1, 15, 12, tzinfo=calendar.ZoneInfo("Europe/Vienna")),
            url="https://meet.proton.me/join/id-TEST#pwd-test",
            passcode=None,
        )

    def test_folded_links_timezone_end_and_cancelled_event(self):
        data = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:yes\nDTSTART;TZID=Europe/Vienna:20300115T110000\n"
            "DTEND;TZID=Europe/Vienna:20300115T120000\n"
            "DESCRIPTION:https://meet.proton.me/join/id-TEST#pwd-lo\n ng\n"
            "END:VEVENT\nBEGIN:VEVENT\nUID:no\nSTATUS:CANCELLED\nDTSTART:20300115T100000Z\n"
            "DESCRIPTION:https://zoom.us/j/12345678901\nEND:VEVENT\nEND:VCALENDAR"
        )
        events = calendar.events(data)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["url"], "https://meet.proton.me/join/id-TEST#pwd-long")
        self.assertEqual(events[0]["start"].astimezone(dt.timezone.utc).hour, 10)
        self.assertEqual(dispatch.duration(events[0]), 180)

    def test_zoom_link_in_location_is_recognized(self):
        events = calendar.events(
            "BEGIN:VEVENT\nUID:one\nDTSTART:20300115T100000Z\n"
            "LOCATION:https://example.zoom.us/j/12345678901?pwd=fixture\nEND:VEVENT"
        )
        self.assertIn("pwd=fixture", events[0]["url"])

    def test_calendar_keeps_busy_control_lost_meeting(self):
        path = self.root / "current.json"
        path.write_text(json.dumps(dict(status="control_lost", started=time.time())))
        with patch.object(calendar, "MJSON", str(path)):
            self.assertTrue(calendar.meeting_active())

    def test_exact_timer_is_created_two_minutes_before_event_in_utc(self):
        event = self.event()

        def call(cmd, **kwargs):
            return MagicMock(returncode=1 if cmd[0] == "systemctl" else 0, stderr="", stdout="")

        with (
            patch.object(dispatch, "ROOT", self.root),
            patch.object(dispatch.subprocess, "run", side_effect=call) as run,
        ):
            result = dispatch.schedule(event, event["start"].timestamp() - 3600)
        self.assertEqual(result["status"], "scheduled")
        launch = next(x.args[0] for x in run.call_args_list if x.args[0][0] == "systemd-run")
        self.assertIn("--on-calendar=2030-01-15 09:58:00 UTC", launch)
        self.assertNotIn(event["url"], launch)

    def test_active_timer_or_dispatch_service_cannot_be_queued_twice(self):
        event = self.event()
        ident = dispatch.key(event)
        (self.root / (ident + ".json")).write_text(
            json.dumps(dict(status="waiting_for_idle", unit="fixture"))
        )
        with (
            patch.object(dispatch, "ROOT", self.root),
            patch.object(dispatch.subprocess, "run", return_value=MagicMock(returncode=0)) as run,
        ):
            result = dispatch.schedule(event, event["start"].timestamp() - 3600)
        self.assertEqual(result["status"], "waiting_for_idle")
        self.assertFalse(any(x.args[0][0] == "systemd-run" for x in run.call_args_list))

    def test_reconciliation_cannot_overwrite_running_dispatch_claim(self):
        event = self.event()
        path = self.root / (dispatch.key(event) + ".json")
        path.write_text(json.dumps(dict(status="dispatching", unit="fixture")))
        with open(str(path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch.object(dispatch, "ROOT", self.root),
                patch.object(dispatch.subprocess, "run") as run,
            ):
                result = dispatch.schedule(event, event["start"].timestamp() - 120)
        self.assertEqual(result["status"], "dispatching")
        run.assert_not_called()

    def test_cancelled_calendar_event_stops_only_its_timer(self):
        path = self.root / "event.json"
        path.write_text(json.dumps(dict(status="scheduled", unit="fixture")))
        with (
            patch.object(dispatch, "ROOT", self.root),
            patch.object(dispatch.subprocess, "run") as run,
        ):
            dispatch.reconcile([], time.time())
        self.assertEqual(json.loads(path.read_text())["status"], "cancelled")
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "stop", "fixture.timer"])

    def test_missing_event_at_dispatch_cannot_join(self):
        event = self.event()
        path = self.root / (dispatch.key(event) + ".json")
        path.write_text(json.dumps(dict(status="scheduled", start=time.time())))
        with (
            patch.object(calendar, "fetch_ics", return_value=""),
            patch.object(calendar, "events", return_value=[]),
            patch.object(dispatch.subprocess, "Popen") as launch,
        ):
            dispatch.dispatch(path)
        launch.assert_not_called()
        self.assertEqual(json.loads(path.read_text())["status"], "cancelled")

    def test_calendar_dry_run_neither_schedules_nor_changes_state(self):
        with (
            patch.object(calendar, "fetch_ics", return_value=""),
            patch.object(calendar, "events", return_value=[]),
            patch.object(dispatch, "reconcile") as reconcile,
            patch.object(calendar, "save_state") as save,
        ):
            calendar.cycle(dry_run=True)
        reconcile.assert_not_called()
        save.assert_not_called()

    def test_zoom_admission_requires_both_leave_and_participant_controls(self):
        frame = MagicMock()
        page = MagicMock(frames=[frame])
        frame.evaluate.return_value = ["Mute", "Participants in this meeting"]
        self.assertFalse(joined_controls(page))
        frame.evaluate.return_value = ["Leave Meeting", "Participants (2)"]
        self.assertTrue(joined_controls(page))
        frame.evaluate.side_effect = RuntimeError("frame detached")
        self.assertFalse(joined_controls(page))

    def test_truncated_feed_cannot_cancel_scheduled_events(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"BEGIN:VCALENDAR\ntruncated"
        with (
            patch.object(
                calendar.Path, "read_text", return_value="https://private.invalid/calendar"
            ),
            patch.object(calendar.urllib.request, "urlopen", return_value=response),
        ):
            with self.assertRaises(ValueError):
                calendar.fetch_ics()

    def test_local_copy_lane_never_calls_nas_mirror(self):
        with (
            patch.object(
                backup,
                "archive_from_log",
                return_value=("/home/transcript/rec/fixture.archive", True),
            ),
            patch.object(backup, "safety_copy", return_value={"status": "ok"}) as local,
            patch.object(backup, "finalized", return_value=True),
            patch.object(backup, "mirror") as nas,
        ):
            backup.run(
                "vm",
                "log",
                "/opt/live-transcription-bot/recordings",
                max_seconds=1,
                secondary=self.root,
                lane="local",
            )
        local.assert_called_once()
        nas.assert_not_called()

    def test_empty_dry_run_does_not_leave_backup_services_running(self):
        log = self.root / "join.log"
        log.write_text("DRY: inspected prejoin without admission")
        with patch.object(backup, "safety_copy") as local, patch.object(backup, "mirror") as nas:
            backup.run("vm", log, self.root, max_seconds=1, lane="local")
        local.assert_not_called()
        nas.assert_not_called()

    def test_local_backup_does_not_stat_known_hard_nfs_destination(self):
        primary = "/opt/live-transcription-bot/recordings/unreachable.archive"
        original_stat = Path.stat

        def checked_stat(path, *args, **kwargs):
            if str(path).startswith("/opt/live-transcription-bot/recordings"):
                raise AssertionError("Local safety worker touched hard NFS mount")
            return original_stat(path, *args, **kwargs)

        original_get = backup.get

        def settings(section, key):
            if (section, key) == ("paths", "recordings"):
                return "/opt/live-transcription-bot/recordings"
            if (section, key) == ("paths", "archive_mount"):
                return "/example/archive-mount"
            return original_get(section, key)

        with (
            patch.object(backup, "get", side_effect=settings),
            patch.object(Path, "stat", checked_stat),
            patch.object(backup, "mirror", return_value=(0, "")),
            patch.object(backup.subprocess, "run", return_value=MagicMock(returncode=23)),
            patch.object(backup, "finalized", return_value=False),
        ):
            result = backup.safety_copy("vm", "/remote/fixture.archive", primary, self.root)
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
