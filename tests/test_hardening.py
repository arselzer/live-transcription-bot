import json
import os
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import wave

from live_transcript.storage.mirror import finalized, capacity_available, safety_copy
from live_transcript.core.options import zoom_invitation, validate_output, rejoin_options
from live_transcript.runtime.watchdog import decision, recovery_options
from live_transcript.capture.guard import Archive, Guard, atomic_json
from live_transcript.storage.recover import available_frames, segments
from live_transcript.runtime.state import begin, update
from live_transcript.transcription.speakers import load
from live_transcript.storage.pull import pull
import live_transcript.providers.proton as proton_meet


class HardeningTests(unittest.TestCase):
    def test_finished_google_refusal_is_visible_to_hermes_without_rejoining(self):
        from live_transcript.runtime import watchdog

        path = self.root / "meeting.json"
        path.write_text(
            json.dumps(dict(status="finished", result="google_join_refused", finished=time.time()))
        )
        with patch.object(watchdog, "MJSON", path), patch.object(watchdog, "probe") as probe:
            self.assertIn("admission-needs-operator", watchdog.tick({}))
        probe.assert_not_called()

    def test_safety_copy_preserves_disk_reserve_and_quota(self):
        gib = 1024**3
        self.assertTrue(capacity_available(0, 73 * gib, 24 * gib, 12 * gib))
        self.assertFalse(capacity_available(0, 12 * gib, 24 * gib, 12 * gib))
        self.assertFalse(capacity_available(24 * gib, 73 * gib, 24 * gib, 12 * gib))

    def test_safety_copy_rejects_same_filesystem_as_primary(self):
        primary = self.root / "primary"
        primary.mkdir()
        result = safety_copy("vm", "/session.archive", primary, self.root / "secondary")
        self.assertEqual(result["status"], "same_filesystem")

    def test_chat_text_cannot_end_meeting_but_ui_end_notice_can(self):
        frame = MagicMock()
        values = {
            "dialog": [],
            "heading": [],
            "body": ["This meeting has ended is the phrase we are testing"],
        }
        frame.get_by_role.side_effect = lambda role: MagicMock(
            all_text_contents=lambda: values[role]
        )
        page = MagicMock(frames=[frame])
        self.assertFalse(proton_meet.meeting_ended(page))
        values["dialog"] = ["This meeting has been ended by the host"]
        self.assertTrue(proton_meet.meeting_ended(page))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def meeting(self):
        return dict(started=100, deadline=1000, status="active")

    def test_zoom_password_digits_cannot_corrupt_meeting_id(self):
        url, ident = zoom_invitation("https://us06web.zoom.us/j/12345678901?pwd=abc987654")
        self.assertEqual(ident, "12345678901")
        self.assertEqual(url, "https://zoom.us/wc/12345678901/join?pwd=abc987654")

    def test_zoom_webclient_formats_and_numeric_ids(self):
        for value in (
            "123 456 78901",
            "https://app.zoom.us/wc/12345678901/join",
            "https://app.zoom.us/wc/join/12345678901",
        ):
            self.assertEqual(zoom_invitation(value)[1], "12345678901")

    def test_invalid_zoom_domains_and_outputs_rejected(self):
        for value in ("https://zoom.us.evil/j/12345678901", "http://zoom.us/j/12345678901", "123"):
            with self.assertRaises(ValueError):
                zoom_invitation(value)
        for name in ("recording name.wav", "bad;command.wav", "name.mp3"):
            with self.assertRaises(ValueError):
                validate_output(self.root / name)
        (self.root / "old.archive").mkdir()
        with self.assertRaises(FileExistsError):
            validate_output(self.root / "old.wav")

    def test_rejoin_removes_both_output_forms_and_dry_run(self):
        options, budget = rejoin_options(
            ["--out=/old.wav", "--dry", "--max-min=30", "--max-min", "25", "--passcode=secret"]
        )
        self.assertNotIn("--out", options)
        self.assertEqual(budget, 25)
        self.assertEqual(options["--passcode"], "secret")

    def test_watchdog_unknown_cannot_trigger_rejoin(self):
        state = {"dead_since": 1}
        self.assertEqual(decision(self.meeting(), None, state, 500), "unknown")
        self.assertNotIn("dead_since", state)

    def test_watchdog_recovers_only_after_both_audio_writers_stop(self):
        state = {}
        remote = dict(
            phase="running",
            archive="/session.archive",
            archive_healthy=False,
            monitor_progress_age=120,
        )
        self.assertEqual(decision(self.meeting(), remote, state, 500), "observe")
        self.assertEqual(decision(self.meeting(), remote, state, 591), "rejoin")
        remote["archive_healthy"] = True
        self.assertEqual(decision(self.meeting(), remote, state, 600), "observe")
        self.assertNotIn("capture_dead_since", state)

    def test_watchdog_waits_through_normal_startup(self):
        meeting = dict(self.meeting(), control_alive=True)
        self.assertEqual(decision(meeting, {"phase": "missing"}, {"dead_since": 1}, 500), "observe")

    def test_watchdog_never_rejoins_dry_or_explicitly_stopped_meeting(self):
        self.assertEqual(
            decision(dict(self.meeting(), dry_run=True), {"phase": "failed"}, {}, 500), "observe"
        )
        self.assertEqual(decision(self.meeting(), {"requested_stop": True}, {}, 500), "finish")

    def test_watchdog_requires_confirmed_failure_and_original_budget(self):
        state = {}
        self.assertEqual(
            decision(self.meeting(), {"phase": "failed", "exit": 1}, state, 500), "observe"
        )
        self.assertEqual(
            decision(self.meeting(), {"phase": "failed", "exit": 1}, state, 591), "rejoin"
        )
        self.assertEqual(
            decision(self.meeting(), {"phase": "failed", "exit": 1}, state, 1001), "exhausted"
        )
        self.assertEqual(
            decision(self.meeting(), {"phase": "failed", "exit": 4}, state, 600), "terminal"
        )

    def test_scoped_rejoin_password_and_last_duration_option(self):
        secret = self.root / "secret.json"
        secret.write_text(
            json.dumps(
                {"argv": ["--out=/old.wav", "--passcode=private", "--max-min=40", "--max-min=30"]}
            )
        )
        args, deadline = recovery_options(dict(started=100, args_file=str(secret)), 700)
        self.assertEqual(deadline, 1900)
        self.assertIn("private", args)
        self.assertEqual(args[args.index("--max-min") + 1], "20")

    def test_previous_owner_cannot_finish_new_meeting(self):
        path = self.root / "state.json"
        atomic_json(path, dict(pid=22, status="active"))
        self.assertFalse(update(path, 21, dict(status="finished")))
        self.assertEqual(json.loads(path.read_text())["status"], "active")

    def test_begin_rejects_superseded_recovery_and_scopes_secrets(self):
        path = self.root / "current_meeting.json"
        atomic_json(path, dict(pid=1, run_id="newer", status="active"))
        env = dict(LT_RUN_ID="test", LT_EXPECT_RUN_ID="older")
        with patch.dict(os.environ, env):
            with self.assertRaises(RuntimeError):
                begin("12345678901", [], self.root)
        with patch.dict(os.environ, {"LT_RUN_ID": "test"}, clear=True):
            begin("12345678901", ["--passcode=private"], self.root)
        value = json.loads(path.read_text())
        self.assertNotIn("private", value["args"])
        self.assertEqual(
            json.loads(Path(value["args_file"]).read_text())["argv"], ["--passcode=private"]
        )
        self.assertEqual(Path(value["args_file"]).stat().st_mode & 0o777, 0o600)

    def test_crashed_wav_header_is_bounded_by_actual_file_bytes(self):
        path = self.root / "crash.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x01\0" * 16000)
        data = bytearray(path.read_bytes())
        struct.pack_into("<I", data, 4, 0xFFFFFFFF)
        struct.pack_into("<I", data, 40, 0xFFFFFFFF)
        path.write_bytes(data)
        self.assertEqual(available_frames(path), 16000)

    def test_partial_csv_and_json_tail_do_not_discard_valid_audio(self):
        atomic_json(self.root / "config.json", dict(started=100, segment_seconds=5))
        (self.root / "events.jsonl").write_text(
            '{"utc":100,"kind":"capture_started","component":"audio","generation":"a"}\n{"unfinished":'
        )
        (self.root / "audio-a.csv").write_text("audio-a-000000.flac,0,5\naudio-a-000001.flac,")
        (self.root / "audio-a-000000.flac").touch()
        (self.root / "audio-a-000001.flac").touch()
        self.assertEqual(len(segments(self.root)), 2)

    def test_guard_exception_is_not_published_as_finalized(self):
        guard = Guard(self.root, dict(started=100, deadline=time.time() + 10))
        with (
            patch.object(guard, "tick", side_effect=RuntimeError("disk error")),
            patch("live_transcript.capture.guard.seal") as seal,
        ):
            with self.assertRaises(RuntimeError):
                guard.run()
        self.assertFalse(json.loads((self.root / "health.json").read_text())["stopped"])
        self.assertFalse(finalized(self.root))
        seal.assert_not_called()

    def test_guard_does_not_finalize_before_successful_seal(self):
        guard = Guard(self.root, dict(started=100, deadline=0))
        with patch("live_transcript.capture.guard.seal", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                guard.run()
        self.assertFalse(finalized(self.root))
        self.assertFalse(json.loads((self.root / "health.json").read_text())["stopped"])

    def test_stopped_health_alone_cannot_stop_mirroring(self):
        atomic_json(self.root / "health.json", dict(stopped=True))
        self.assertFalse(finalized(self.root))

    def test_existing_archive_cannot_be_overwritten(self):
        archive = Archive(self.root / "old.wav")
        archive.root.mkdir()
        atomic_json(archive.root / "config.json", dict(original=True))
        with self.assertRaises(FileExistsError):
            archive.start(100, 60)
        self.assertEqual(json.loads((archive.root / "config.json").read_text()), {"original": True})

    def test_mutable_wav_transfer_uses_checksum_and_retries_timeouts(self):
        with patch(
            "live_transcript.storage.pull.subprocess.run",
            side_effect=subprocess.TimeoutExpired("rsync", 1),
        ) as run:
            with self.assertRaises(RuntimeError):
                pull("vm", "/rec.wav", self.root / "rec.wav", required=True)
        self.assertEqual(run.call_count, 3)
        self.assertIn("--checksum", run.call_args.args[0])
        self.assertNotIn("--append-verify", run.call_args.args[0])

    def test_speaker_never_uses_future_or_stale_observations(self):
        path = self.root / "speakers.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(dict(t=t, who=[dict(role="active", name=n)]))
                for t, n in [(0, "Alice"), (1, "Bob")]
            )
        )
        speaker, _ = load(path, self.root / "voice.jsonl")
        self.assertEqual(speaker(0.8), "Alice")
        self.assertEqual(speaker(1), "Bob")
        self.assertIsNone(speaker(10))

    def test_observer_error_clears_label_and_own_voice_is_attributed(self):
        path = self.root / "speakers.jsonl"
        path.write_text(
            '{"t":0,"who":[{"role":"active","name":"Alice"}]}\n{"t":1,"who":[],"error":"DOM changed"}\n'
        )
        voice = self.root / "voice.jsonl"
        voice.write_text('{"t":2,"duration":1,"name":"Live Transcript"}\n')
        speaker, _ = load(path, voice)
        self.assertIsNone(speaker(1.5))
        self.assertEqual(speaker(2.5), "Live Transcript")

    def test_proton_optional_feature_failure_does_not_end_recording(self):
        def broken():
            raise RuntimeError("browser navigation race")

        self.assertEqual(proton_meet.optional_feature(broken, "stats", {}), {})

    def test_stt_outage_keeps_previous_transcript(self):
        path = self.root / "recording.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 80000)
        transcript = path.with_suffix(".txt")
        transcript.write_text("Previously verified transcript.")
        with (
            patch.object(sys, "argv", ["postprocess.py", str(path)]),
            patch("subprocess.run", side_effect=RuntimeError("GPU unavailable")),
            patch.dict(sys.modules, {"faster_whisper": None}),
        ):
            with self.assertRaises(SystemExit):
                runpy.run_module("live_transcript.runtime.postprocess", run_name="__main__")
        self.assertEqual(transcript.read_text(), "Previously verified transcript.")
        self.assertEqual(
            json.loads(path.with_suffix(".postprocess-status.json").read_text())["status"],
            "stt_failed",
        )

    def test_success_exit_with_unparsed_asr_output_cannot_erase_transcript(self):
        path = self.root / "recording.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 80000)
        transcript = path.with_suffix(".txt")
        transcript.write_text("Verified transcript.")
        result = MagicMock(stdout="Backend changed its output format", stderr="", returncode=0)
        with (
            patch.object(sys, "argv", ["postprocess.py", str(path)]),
            patch("subprocess.run", return_value=result),
        ):
            with self.assertRaises(SystemExit):
                runpy.run_module("live_transcript.runtime.postprocess", run_name="__main__")
        self.assertEqual(transcript.read_text(), "Verified transcript.")
        self.assertEqual(
            json.loads(path.with_suffix(".postprocess-status.json").read_text())["status"],
            "stt_failed",
        )

    def test_failed_primary_with_empty_fallback_reports_stt_failed_not_silence(self):
        import types

        path = self.root / "recording.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 80000)
        transcript = path.with_suffix(".txt")
        transcript.write_text("Verified transcript.")
        result = MagicMock(stdout="Backend changed its output format", stderr="", returncode=0)
        fw = types.ModuleType("faster_whisper")

        class _Model:
            def __init__(self, *a, **k):
                pass

            def transcribe(self, *a, **k):
                return iter([]), MagicMock(language="und")

        fw.WhisperModel = _Model
        with (
            patch.object(sys, "argv", ["postprocess.py", str(path)]),
            patch("subprocess.run", return_value=result),
            patch.dict(sys.modules, {"faster_whisper": fw}),
        ):
            with self.assertRaises(SystemExit):
                runpy.run_module("live_transcript.runtime.postprocess", run_name="__main__")
        status = json.loads(path.with_suffix(".postprocess-status.json").read_text())
        self.assertEqual(status["status"], "stt_failed")
        self.assertTrue(status.get("fallback_empty"))
        self.assertEqual(transcript.read_text(), "Verified transcript.")

    def test_successful_silence_reports_prior_retained_not_failure(self):
        path = self.root / "recording.wav"
        path.touch()
        transcript = path.with_suffix(".txt")
        transcript.write_text("Verified transcript.")
        from live_transcript.transcription import batch as batch_module

        with (
            patch.object(sys, "argv", ["postprocess.py", str(path)]),
            patch.object(
                batch_module,
                "transcribe_wav",
                return_value=([], {"chunks": 1, "retries": [], "languages": []}),
            ),
        ):
            with self.assertRaises(SystemExit):
                runpy.run_module("live_transcript.runtime.postprocess", run_name="__main__")
        self.assertEqual(
            json.loads(path.with_suffix(".postprocess-status.json").read_text())["status"],
            "stt_empty_prior_retained",
        )
        self.assertEqual(transcript.read_text(), "Verified transcript.")

    def test_silence_led_detection_window_uses_voiced_speech_only(self):
        import math, struct
        from live_transcript.transcription.batch import voiced_offset, voiced_pieces

        path = self.root / "silence-led.wav"
        burst = struct.pack(
            "<" + "h" * (2 * 16000),
            *[int(10000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(2 * 16000)],
        )
        with wave.open(str(path), "wb") as a:
            a.setnchannels(1)
            a.setsampwidth(2)
            a.setframerate(16000)
            a.writeframes(b"\0\0" * 16000 * 20)
            a.writeframes(burst)
            a.writeframes(b"\0\0" * 16000 * 20)
            a.writeframes(burst)
        self.assertAlmostEqual(voiced_offset(path, 0, 60), 20.0, delta=0.5)
        pieces = voiced_pieces(path, 0, 60)
        self.assertTrue(pieces and 19.5 <= pieces[0][0] <= 20.2)
        self.assertLessEqual(sum(e - s for s, e in pieces), 9.0)

    def test_looping_candidate_never_overwrites_outputs_or_starts_cpu_fallback(self):
        from live_transcript.transcription.quality import require_acceptable, TranscriptQualityError

        rows = [
            dict(t=i, text="This recognizer is repeating a hallucinated sentence.")
            for i in range(15)
        ]
        try:
            require_acceptable(rows)
        except TranscriptQualityError as exc:
            error = exc
        path = self.root / "meeting.wav"
        path.touch()
        for suffix in (".txt", ".md", ".srt"):
            path.with_suffix(suffix).write_text("Prior evidence")
        with (
            patch.object(sys, "argv", ["postprocess.py", str(path)]),
            patch("live_transcript.transcription.batch.transcribe_wav", side_effect=error),
            patch("subprocess.run") as run,
        ):
            with self.assertRaises(SystemExit):
                runpy.run_module("live_transcript.runtime.postprocess", run_name="__main__")
        run.assert_not_called()
        for suffix in (".txt", ".md", ".srt"):
            self.assertEqual(path.with_suffix(suffix).read_text(), "Prior evidence")
        rejected = list(self.root.glob("meeting.rejected-*.json"))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(json.loads(rejected[0].read_text())["utterances"], rows)
        self.assertEqual(
            json.loads(path.with_suffix(".postprocess-status.json").read_text())["status"],
            "quality_failed",
        )


if __name__ == "__main__":
    unittest.main()
