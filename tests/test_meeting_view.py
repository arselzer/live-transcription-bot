"""Read-only UI access must preserve revisions and isolate meeting journals."""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from live_transcript.runtime.meeting_view import read, validate_session
from live_transcript.transcription.store import CaptionStore


class MeetingViewTests(unittest.TestCase):
    def test_canonical_revisions_isolation_and_no_audio_or_extra_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = CaptionStore(path)
            self.addCleanup(store.close)
            store.checkpoint("session-a", dict(lines=[(1, "Speaker", "old words")]))
            store.checkpoint("session-b", dict(lines=[(1, "Other", "another meeting")]))
            store.checkpoint(
                "session-a",
                dict(
                    lines=[(1, "Speaker", "corrected words")],
                    pcm=b"private audio",
                    private_key="never serialize",
                    board=dict(bullets=["Draft note"]),
                ),
            )
            view = read(path, dict(id="session-a", provider="proton"))
            self.assertEqual(view["lines"], [dict(t=1, speaker="Speaker", text="corrected words")])
            self.assertNotIn("private", str(view))
            self.assertNotIn("another meeting", str(view))
            self.assertEqual(view["board"]["bullets"], ["Draft note"])
            self.assertEqual(read(path, dict(id="missing"))["status"], "waiting")
            self.assertIsNone(read(path, None)["session"])

    def test_limit_truncation_corrupt_journal_and_missing_file_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            self.assertEqual(read(path, dict(id="test"))["status"], "unavailable")
            self.assertFalse(path.exists())
            store = CaptionStore(path)
            store.checkpoint("test", dict(lines=[(n, "Speaker", str(n)) for n in range(20)]))
            store.close()
            result = read(path, dict(id="test"), limit=5)
            self.assertEqual([line["t"] for line in result["lines"]], list(range(15, 20)))
            self.assertTrue(result["truncated"])
            path.write_bytes(b"corrupt database")
            with self.assertRaises(sqlite3.DatabaseError):
                read(path, dict(id="test"))
            self.assertEqual(path.read_bytes(), b"corrupt database")

    def test_invalid_session_is_rejected(self):
        for value in ("session", {}, dict(id="../other"), dict(id="a\nb")):
            with self.assertRaises(ValueError):
                validate_session(value)

    def test_large_unicode_passages_fit_the_hub_response_budget(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = CaptionStore(path)
            store.checkpoint(
                "test", dict(lines=[(n, "Speaker", "\u2603" * 16000) for n in range(40)])
            )
            store.close()
            result = read(path, dict(id="test"))
            self.assertTrue(result["truncated"])
            self.assertLess(len(json.dumps(result).encode()), 3 * 1024 * 1024)
            self.assertEqual(result["lines"][-1]["t"], 39)


if __name__ == "__main__":
    unittest.main()
