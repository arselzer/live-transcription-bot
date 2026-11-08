import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

from live_transcript.storage.mirror import archive_from_log, mirror
from live_transcript.storage.pull import verify_archive
from live_transcript.capture.guard import Archive, Guard, atomic_json, commands, event, seal
from live_transcript.storage.recover import recover, segments


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def config(self):
        config = dict(started=100, deadline=200, provider="proton", segment_seconds=10)
        atomic_json(self.root / "config.json", config)
        return config

    def test_master_audio_is_lossless_full_rate_and_short_segments(self):
        cmd = commands(self.root, "unique", ":99")["audio"]
        self.assertIn("flac", cmd)
        self.assertEqual(cmd[cmd.index("-sample_rate") + 1], "48000")
        self.assertEqual(cmd[cmd.index("-channels") + 1], "2")
        self.assertIn("5", cmd)
        self.assertNotIn("8099", " ".join(cmd))

    def test_new_generations_cannot_overwrite_older_evidence(self):
        a = commands(self.root, "a", ":99")
        b = commands(self.root, "b", ":99")
        for component in a:
            self.assertNotEqual(a[component][-1], b[component][-1])
            self.assertIn("-n", a[component])

    def test_full_desktop_capture_does_not_crop_taller_consoles(self):
        options = commands(self.root, "unique", ":99")
        for component in ("screen", "snapshots"):
            self.assertNotIn("-video_size", options[component])
        self.assertIn("pad=ceil(iw/2)*2:ceil(ih/2)*2", options["screen"])

    def test_admission_state_is_part_of_the_verified_inventory(self):
        atomic_json(self.root / "admission.json", dict(stage="needs_operator"))
        seal(self.root)
        self.assertEqual(verify_archive(self.root), 1)
        atomic_json(self.root / "admission.json", dict(stage="admitted"))
        with self.assertRaises(ValueError):
            verify_archive(self.root)

    def test_dead_component_restarts_without_stopping_other_captures(self):
        guard = Guard(self.root, self.config())
        for name in ("audio", "screen", "snapshots", "observer"):
            proc = MagicMock()
            proc.poll.return_value = 1 if name == "screen" else None
            proc.returncode = 1 if name == "screen" else None
            guard.slots[name] = dict(
                proc=proc,
                generation="old",
                started=100,
                progress_at=10000000000,
                size=0,
                restart_at=0,
            )

        def launch(name):
            proc = MagicMock()
            proc.poll.return_value = None
            guard.slots[name] = dict(
                proc=proc, generation="new", started=100, progress_at=10000000000, size=0
            )

        with patch.object(guard, "launch", side_effect=launch) as restart:
            guard.tick()
        restart.assert_called_once_with("screen")
        self.assertEqual(guard.slots["audio"]["generation"], "old")

    def test_sealed_archive_detects_transfer_corruption(self):
        (self.root / "audio-a-000000.flac").write_bytes(b"audio evidence")
        seal(self.root)
        self.assertEqual(verify_archive(self.root), 1)
        (self.root / "audio-a-000000.flac").write_bytes(b"corrupted")
        with self.assertRaises(ValueError):
            verify_archive(self.root)

    def test_session_log_discovers_archive_before_convenience_wav(self):
        log = self.root / "join.log"
        log.write_text("ARCHIVE to /home/transcript/rec/session.archive\n")
        self.assertEqual(archive_from_log(log), ("/home/transcript/rec/session.archive", False))

    def test_recovery_includes_unlisted_interrupted_tail(self):
        self.config()
        event(self.root, "capture_started", component="audio", generation="a")
        events = dict(utc=100, kind="capture_started", component="audio", generation="a")
        (self.root / "events.jsonl").write_text(json.dumps(events) + "\n")
        (self.root / "audio-a.csv").write_text("audio-a-000000.flac,0,10\n")
        (self.root / "audio-a-000000.flac").touch()
        (self.root / "audio-a-000001.flac").touch()
        self.assertEqual([x[0] for x in segments(self.root)], [0, 10])

    def test_mirror_copies_mutable_headers_without_append_or_delete(self):
        with patch("live_transcript.storage.mirror.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            mirror("vm", "/archive", self.root / "copy")
        cmd = run.call_args.args[0]
        self.assertIn("--checksum", cmd)
        self.assertFalse(any("--append" in arg or "--delete" in arg for arg in cmd))

    def test_archive_uses_independent_restarting_service(self):
        archive = Archive(self.root / "session.wav")
        archive.root.mkdir()
        (archive.root / "audio-a-000000.flac").write_bytes(b"x" * 2048)
        import time

        atomic_json(archive.root / "health.json", dict(utc=time.time(), audio=dict(running=True)))
        with patch("live_transcript.capture.guard.subprocess.run") as run:
            archive.start(100, 60)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "systemd-run")
        self.assertIn("--property=Restart=on-failure", cmd)
        self.assertIn("--property=KillMode=mixed", cmd)

    def test_recovered_audio_preserves_clock_gaps_and_original(self):
        self.config()
        (self.root / "events.jsonl").write_text(
            "\n".join(
                json.dumps(dict(utc=at, kind="capture_started", component="audio", generation=gen))
                for gen, at in [("a", 100), ("b", 102)]
            )
            + "\n"
        )
        for gen in ("a", "b"):
            audio = self.root / f"audio-{gen}-000000.flac"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:a",
                    "flac",
                    str(audio),
                ],
                check=True,
            )
            (self.root / f"audio-{gen}.csv").write_text(f"{audio.name},0,1\n")
        output = self.root / "session.wav"
        output.write_bytes(b"original recording")
        report = recover(self.root, output, replace=True)
        self.assertAlmostEqual(report["duration_seconds"], 3, places=2)
        self.assertAlmostEqual(report["gaps"][0]["seconds"], 1, places=2)
        self.assertEqual(
            next(self.root.glob("session.original-*.wav")).read_bytes(), b"original recording"
        )
        with wave.open(str(output)) as wav:
            self.assertEqual(wav.getframerate(), 16000)
            wav.setpos(16000)
            self.assertEqual(wav.readframes(16000), b"\0\0" * 16000)

    def test_separate_monitor_wav_fills_archive_restart_gap(self):
        self.config()
        output = self.root / "session.wav"
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\xd2\x04" * (3 * 16000))
        events = [dict(utc=100, kind="primary_capture_started", file=output.name)]
        for generation, at in [("a", 100), ("b", 102)]:
            events.append(
                dict(utc=at, kind="capture_started", component="audio", generation=generation)
            )
            name = f"audio-{generation}-000000.flac"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:a",
                    "flac",
                    str(self.root / name),
                ],
                check=True,
            )
            (self.root / f"audio-{generation}.csv").write_text(f"{name},0,1\n")
        (self.root / "events.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n")
        report = recover(self.root, output, replace=True)
        self.assertAlmostEqual(report["gaps"][0]["backup_seconds"], 1, places=2)
        with wave.open(str(output)) as wav:
            wav.setpos(16000)
            self.assertEqual(wav.readframes(16000), b"\xd2\x04" * 16000)


if __name__ == "__main__":
    unittest.main()
