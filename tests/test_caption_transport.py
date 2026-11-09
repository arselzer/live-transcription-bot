import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import live_transcript.transcription.transport as stream
from live_transcript.transcription.speakers import JSONLinesTail, LiveTimeline
from test_caption_pipeline import wav_bytes


class TimelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.speaker, self.voice = self.root / "speakers.jsonl", self.root / "voice.jsonl"

    def row(self, at, name):
        return json.dumps(dict(t=at, who=[dict(name=name, role="active")])) + "\n"

    def test_partial_rows_wait_and_completed_rows_are_not_reparsed(self):
        reader = JSONLinesTail()
        self.speaker.write_text('{"t":1')
        self.assertEqual(reader.read(self.speaker), (True, []))
        with self.speaker.open("a") as f:
            f.write('}\nnot json\n{"t":2}\n')
        self.assertEqual(reader.read(self.speaker), (False, [dict(t=1), dict(t=2)]))
        with patch(
            "live_transcript.transcription.speakers.json.loads",
            side_effect=AssertionError("reparsed"),
        ):
            self.assertEqual(reader.read(self.speaker), (False, []))

    def test_rotation_truncation_and_rewrite_reset_cached_names(self):
        reader = LiveTimeline()
        self.speaker.write_text(self.row(0, "Speaker A") + self.row(1, "Speaker A"))
        self.assertEqual(reader.read(self.speaker, self.voice), [(0, "Speaker A")])
        self.assertEqual(reader.observed_until, 1)
        self.speaker.write_text(self.row(0, "Speaker B"))
        self.assertEqual(reader.read(self.speaker, self.voice), [(0, "Speaker B")])
        replacement = self.root / "replacement"
        replacement.write_text(self.row(2, "Speaker C"))
        replacement.replace(self.speaker)
        self.assertEqual(reader.read(self.speaker, self.voice), [(2, "Speaker C")])
        self.speaker.write_text(self.row(2, "Speaker D") + self.row(3, "Speaker D"))
        self.assertEqual(reader.read(self.speaker, self.voice), [(2, "Speaker D")])
        self.speaker.unlink()
        self.assertEqual(reader.read(self.speaker, self.voice), [])

    def test_voice_changes_and_ambiguous_speakers_preserve_uncertainty(self):
        reader = LiveTimeline()
        self.speaker.write_text(self.row(0, "Speaker A") + self.row(4, "Speaker B"))
        self.assertEqual(
            reader.read(self.speaker, self.voice), [(0, "Speaker A"), (4, "Speaker B")]
        )
        self.voice.write_text(json.dumps(dict(t=1, duration=1, speaker_label="Test voice")) + "\n")
        self.assertEqual(
            reader.read(self.speaker, self.voice),
            [(0, "Speaker A"), (1, "Test voice"), (2, ""), (4, "Speaker B")],
        )
        with self.speaker.open("a") as f:
            f.write(
                json.dumps(
                    dict(
                        t=5,
                        who=[
                            dict(name="Speaker A", role="active"),
                            dict(name="Speaker B", role="active"),
                        ],
                    )
                )
                + "\n"
            )
        self.assertEqual(reader.read(self.speaker, self.voice)[-1], (5, ""))


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = dict(
            dir=str(self.root),
            stream="capture-id",
            next_seq=0,
            sent_samples=0,
            audio_t0=5,
            closed=False,
        )
        (self.root / "capchunk_00000.wav").write_bytes(wav_bytes(b"\x01\x08" * 12000))
        (self.root / "capchunk_00001.wav").write_bytes(b"")
        self.sources = patch.object(stream, "SOURCES", [self.source])
        self.timeline = patch.object(
            stream, "read_timeline", return_value=[(5, "Riley_name with spaces")]
        )
        self.sources.start()
        self.timeline.start()

    def tearDown(self):
        self.sources.stop()
        self.timeline.stop()
        self.temp.cleanup()

    def test_lost_ack_keeps_packet_and_reuses_identical_metadata(self):
        sent = []

        class Upload:
            def send(self, meta, wav):
                sent.append(dict(meta))
                if len(sent) == 1:
                    raise ConnectionError("ACK lost")

        uploader = Upload()
        with self.assertRaises(ConnectionError):
            stream.upload_ready(uploader)
        self.assertTrue((self.root / "capchunk_00000.wav").exists())
        self.assertEqual(self.source["next_seq"], 0)
        stream.upload_ready(uploader)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(sent[1]["timeline"][0][1], "Riley_name with spaces")
        self.assertFalse((self.root / "capchunk_00000.wav").exists())

    def test_success_persists_progress_before_removing_packet(self):
        class Upload:
            def send(self, *_):
                pass

        stream.upload_ready(Upload())
        saved = json.loads((self.root / "source.json").read_text())
        self.assertEqual(saved["next_seq"], 1)
        self.assertEqual(saved["sent_samples"], 12000)

    def test_open_packet_is_not_uploaded(self):
        (self.root / "capchunk_00001.wav").unlink()

        class Upload:
            def send(self, *_):
                raise AssertionError("open packet was uploaded")

        self.assertFalse(stream.upload_ready(Upload()))

    def test_closed_generation_flushes_last_packet(self):
        (self.root / "capchunk_00001.wav").unlink()
        self.source["closed"] = True
        sent = []

        class Upload:
            def send(self, meta, *_):
                sent.append(meta)

        self.assertTrue(stream.upload_ready(Upload()))
        self.assertEqual(len(sent), 1)

    def test_malformed_packet_is_retained(self):
        (self.root / "capchunk_00000.wav").write_bytes(b"invalid audio")
        self.assertFalse(stream.upload_ready(None))
        self.assertTrue((self.root / "capchunk_00000.wav").exists())
        self.assertEqual(self.source["next_seq"], 0)
        self.assertEqual(self.source["read_error"]["seq"], 0)

    def test_zero_byte_old_generation_does_not_block_new_audio_and_can_be_repaired(self):
        old = self.root / "old"
        old.mkdir()
        damaged = old / "capchunk_00000.wav"
        damaged.write_bytes(b"")
        previous = dict(self.source, dir=str(old), stream="old-capture", closed=True)
        sent = []

        class Upload:
            def send(self, meta, wav):
                sent.append((dict(meta), wav))

        with patch.object(stream, "SOURCES", [previous, self.source]):
            self.assertTrue(stream.upload_ready(Upload()))
            self.assertEqual(sent[0][0]["stream"], "capture-id")
            self.assertEqual(previous["next_seq"], 0)
            self.assertEqual(previous["sent_samples"], 0)
            self.assertEqual(damaged.read_bytes(), b"")
            persisted = json.loads((old / "source.json").read_text())
            self.assertEqual(persisted["read_error"]["error"], "EOFError")
            repaired = wav_bytes(b"\x01\x08" * 4000)
            damaged.write_bytes(repaired)
            self.assertTrue(stream.upload_ready(Upload()))
        self.assertEqual(sent[1][0]["stream"], "old-capture")
        self.assertEqual(sent[1][0]["seq"], 0)
        self.assertEqual(sent[1][0]["audio_t0"], 5)
        self.assertEqual(sent[1][1], repaired)
        self.assertNotIn("read_error", previous)
        self.assertFalse(damaged.exists())


if __name__ == "__main__":
    unittest.main()
