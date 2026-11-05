import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
import wave

import live_transcript.transcription.batch as batch
from live_transcript.notes.summary import public_metadata


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "meeting.wav"
        with wave.open(str(self.path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000 * 90)

    def test_parser_retains_real_end_times_and_chunk_offset(self):
        rows = batch.parse_output("[00:00:01.20 --> 00:00:02.50] Actual speech.", 300)
        self.assertEqual(rows, [dict(t=301.2, end=302.5, text="Actual speech.")])

    def test_invalid_end_timestamp_cannot_produce_bad_subtitles(self):
        with self.assertRaises(ValueError):
            batch.parse_output("[00:00:02.00 --> 00:00:01.00] Invalid")

    def test_bad_long_chunk_is_retried_independently_without_skipping_audio(self):
        commands = []
        number = 0
        clip_start = 0

        def runner(command, **kwargs):
            nonlocal number, clip_start
            commands.append(command)
            if "-ss" in command:
                clip_start = float(command[command.index("-ss") + 1])
            if command[0] == batch.get("paths", "whisper_cli"):
                number += 1
                if number == 1:
                    text = "\n".join(
                        f"[00:00:{i:02d}.00 --> 00:00:{i + 1:02d}.00] "
                        + (
                            "This is a repeated hallucinated long sentence."
                            if i % 2
                            else "Yes yes yes."
                        )
                        for i in range(30)
                    )
                else:
                    second = 0 if clip_start == 0 else 2
                    text = f"[00:00:{second:02d}.00 --> 00:00:{second + 2:02d}.00] Recovered independent section {number}."
                return MagicMock(stdout=text, stderr="auto-detected language: de", returncode=0)
            return MagicMock(stdout="", stderr="", returncode=0)

        rows, info = batch.transcribe_wav(self.path, runner=runner)
        self.assertEqual([r["t"] for r in rows], [0, 30, 60])
        self.assertEqual(len(info["retries"]), 1)
        repair = [c for c in commands if "-mc" in c]
        self.assertEqual(len(repair), 3)
        self.assertTrue(all("-bs" in c for c in repair))
        self.assertTrue(all(c[c.index("-l") + 1] == "auto" for c in repair))

    def test_later_chunks_and_crossing_speech_are_retained(self):
        with wave.open(str(self.path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000 * 610)

        def runner(command, **kwargs):
            if command[0] == batch.get("paths", "whisper_cli"):
                return MagicMock(
                    stdout="[00:00:01.00 --> 00:00:03.00] Speech crossing the boundary.",
                    stderr="",
                    returncode=0,
                )
            return MagicMock(stdout="", stderr="", returncode=0)

        rows, info = batch.transcribe_wav(self.path, runner=runner)
        self.assertEqual([r["t"] for r in rows], [1, 300, 600])
        self.assertEqual([r["end"] for r in rows], [3, 301, 601])
        self.assertEqual(info["retries"], [])

    def test_validated_matching_cache_avoids_duplicate_full_meeting_inference(self):
        rows = [dict(t=0, end=1, text="Previously decoded speech.")]
        signature = batch.cache_signature(self.path)
        self.path.with_suffix(".transcription.json").write_text(
            json.dumps(dict(signature=signature, utterances=rows, languages=["de"]))
        )
        runner = MagicMock(side_effect=AssertionError("must not run inference"))
        result, info = batch.transcribe_wav(self.path, runner=runner)
        self.assertEqual(result, rows)
        self.assertTrue(info["cache_reused"])
        runner.assert_not_called()
        with self.path.open("r+b") as stream:
            stream.seek(-2, 2)
            stream.write(b"xx")
        self.assertNotEqual(batch.cache_signature(self.path), signature)

    def test_unparseable_timestamp_format_is_not_accepted_as_silence(self):
        def runner(command, **kw):
            return MagicMock(stdout="[00:00:broken] text", stderr="", returncode=0)

        with self.assertRaises(ValueError):
            batch.transcribe_wav(self.path, runner=runner)

    def test_plain_changed_format_is_not_accepted_as_silence(self):
        def runner(command, **kw):
            return MagicMock(stdout="Unrecognized backend output", stderr="", returncode=0)

        with self.assertRaises(ValueError):
            batch.transcribe_wav(self.path, runner=runner)

    def test_notes_metadata_excludes_join_passwords_but_keeps_original_private_input(self):
        original = dict(
            meeting="https://meet.proton.me/join/id-ROOM#pwd-secret", title="Discussion"
        )
        self.assertEqual(
            public_metadata(original)["meeting"], "https://meet.proton.me/join/id-ROOM"
        )
        self.assertIn("pwd-secret", original["meeting"])
