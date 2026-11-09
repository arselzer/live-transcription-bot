import copy
import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from live_transcript.core import config
from live_transcript.runtime import library


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        value = copy.deepcopy(config.DEFAULTS)
        value["paths"].update(
            root=str(self.root),
            state=str(self.root / "state"),
            recordings=str(self.recordings),
            safety_copies=str(self.root / "safety"),
        )
        self.patch = patch.object(config, "CONFIG", value)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with wave.open(str(self.recordings / "fixture-session.wav"), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x01" * 48000)
        (self.recordings / "fixture-session.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nA synthetic passage.\n"
        )
        archive = self.recordings / "fixture-session.archive"
        archive.mkdir()
        (archive / "config.json").write_text(json.dumps(dict(started=990, provider="proton")))
        (archive / "events.jsonl").write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    dict(kind="primary_capture_started", utc=1000),
                    dict(
                        kind="capture_interrupted", utc=1005, component="audio", private="ignored"
                    ),
                ]
            )
        )
        library.scan()
        self.ident = library.listing()["recordings"][0]["id"]

    def test_catalogue_contains_bounded_views_and_wav_relative_gap_times(self):
        value = library.detail(self.ident)
        self.assertEqual(value["duration"], 3)
        self.assertEqual(value["lines"][0]["t"], 1)
        self.assertEqual(value["events"][0]["t"], 5)
        self.assertNotIn("private", value["events"][0])
        self.assertNotIn(str(self.root), json.dumps(value))
        self.assertEqual(library.database().stat().st_mode & 0o777, 0o600)
        with library.artifact(self.ident, "audio") as (stream, details, mime):
            self.assertEqual(stream.read(4), b"RIFF")
            self.assertEqual(mime, "audio/wav")

    def test_symlink_replacement_and_path_escape_never_expose_outside_files(self):
        secret = self.root / "unrelated"
        secret.write_bytes(b"private fixture")
        wav = self.recordings / "fixture-session.wav"
        wav.unlink()
        wav.symlink_to(secret)
        with self.assertRaises(OSError):
            with library.artifact(self.ident, "audio"):
                self.fail("followed symlink")
        with self.assertRaises(ValueError):
            with library.evidence(self.recordings, "../unrelated"):
                self.fail("escaped root")
        library.scan()
        self.assertNotIn("audio", library.detail(self.ident)["artifacts"])

    def test_missing_archive_clock_is_not_guessed_and_search_is_literal(self):
        events = self.recordings / "fixture-session.archive/events.jsonl"
        events.write_text(json.dumps(dict(kind="audio_server_interrupted", utc=1005)))
        (self.recordings / "fixture-session.archive/health.json").write_text(
            '{"utc":"invalid","stopped":false}'
        )
        library.scan()
        self.assertIsNone(library.detail(self.ident)["events"][0]["t"])
        self.assertEqual(library.listing("_")["recordings"], [])
        self.assertEqual(library.listing("' OR 1=1 --")["recordings"], [])

    def test_changed_audio_is_flagged_instead_of_presenting_verified_alignment(self):
        (self.recordings / "fixture-session.transcript.json").write_text(
            json.dumps(
                dict(
                    audio=dict(bytes=1, mtime_ns=1),
                    lines=[dict(t=1, end=2, text="Old text", speaker="")],
                )
            )
        )
        library.scan()
        self.assertFalse(library.detail(self.ident)["audio_matches_transcript"])

    def test_final_structured_transcript_precedes_recognizer_cache(self):
        (self.recordings / "fixture-session.transcript.json").write_text(
            json.dumps(
                dict(
                    lines=[
                        dict(t=1, end=2, text="Final text", speaker="Participant 1"),
                        dict(t=-1, text="invalid"),
                    ]
                )
            )
        )
        library.scan()
        value = library.detail(self.ident)
        self.assertEqual(len(value["lines"]), 1)
        self.assertEqual(value["lines"][0]["speaker"], "Participant 1")
        self.assertEqual(value["transcript_source"], "final")

    def test_jitsi_recording_keeps_its_provider_label(self):
        (self.recordings / "fixture-session.archive/config.json").write_text('{"provider":"jitsi"}')
        library.scan()
        self.assertEqual(library.detail(self.ident)["provider"], "jitsi")
