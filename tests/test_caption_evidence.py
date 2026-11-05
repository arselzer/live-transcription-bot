from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from live_transcript.storage.live_evidence import export


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "captions.sqlite3"
        self.wav = self.root / "session.wav"
        self.local = self.root / "local"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("CREATE TABLE cards(session TEXT,t REAL,speaker TEXT,text TEXT)")
            connection.executemany(
                "INSERT INTO cards VALUES(?,?,?,?)",
                [("session", i, "Alice", "Independent evidence " + str(i)) for i in range(50)]
                + [("other", 0, "Bob", "Do not associate another meeting")],
            )

    def save(self):
        return export(self.wav, self.db, self.local)

    def test_full_session_is_exported_not_only_the_camera_tail(self):
        result = self.save()
        self.assertEqual(result["segments"], 50)
        saved = json.loads(self.wav.with_suffix(".live.json").read_text())
        self.assertEqual(len(saved["utterances"]), 50)
        self.assertEqual(saved["utterances"][-1]["t"], 49)
        self.assertEqual(saved["quality"]["status"], "pass")
        self.assertEqual(saved, json.loads((self.local / "session.live.json").read_text()))

    def test_empty_or_shorter_journal_cannot_replace_complete_export(self):
        self.save()
        before = self.wav.with_suffix(".live.json").read_text()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DELETE FROM cards WHERE t>3")
        self.assertEqual(self.save()["segments"], 50)
        self.assertEqual(self.wav.with_suffix(".live.json").read_text(), before)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DELETE FROM cards")
        self.assertEqual(self.save()["status"], "unavailable")
        self.assertEqual(self.wav.with_suffix(".live.json").read_text(), before)

    def test_local_evidence_survives_archive_destination_failure(self):
        self.wav = self.root / "absent" / "session.wav"
        with self.assertRaises(OSError):
            self.save()
        saved = json.loads((self.local / "session.live.json").read_text())
        self.assertEqual(len(saved["utterances"]), 50)

    def test_missing_database_is_not_created(self):
        self.db.unlink()
        self.assertEqual(self.save()["reason"], "journal_missing")
        self.assertFalse(self.db.exists())

    def test_live_repetition_is_flagged_without_deleting_words(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DELETE FROM cards")
            connection.executemany(
                "INSERT INTO cards VALUES(?,?,?,?)",
                [
                    ("session", i, "Alice", "Und ich bin sehr glücklich dass ich hier bin")
                    for i in range(11)
                ],
            )
        self.save()
        saved = json.loads(self.wav.with_suffix(".live.json").read_text())
        self.assertEqual(len(saved["utterances"]), 11)
        self.assertEqual(saved["quality"]["status"], "needs_review")

    def test_snapshot_entries_missing_from_legacy_cards_are_preserved(self):
        lines = [(i, "Alice", f"Final speech {i}") for i in range(70)]
        state = dict(lines=lines, pick="de", revision=100, decode_error="")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("CREATE TABLE snapshots(session TEXT PRIMARY KEY,state TEXT)")
            connection.execute("INSERT INTO snapshots VALUES(?,?)", ("session", json.dumps(state)))
        self.assertEqual(self.save()["segments"], 70)
        saved = json.loads(self.wav.with_suffix(".live.json").read_text())
        self.assertEqual(saved["view"], "checkpoint_lines")
        self.assertEqual(len(saved["journal_cards"]), 50)
        self.assertEqual(saved["decoder_snapshot"]["revision"], 100)
        self.assertEqual(saved["utterances"][69]["text"], "Final speech 69")

    def test_final_state_survives_even_when_card_table_is_empty(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DELETE FROM cards")
            connection.execute("CREATE TABLE snapshots(session TEXT PRIMARY KEY,state TEXT)")
            connection.execute(
                "INSERT INTO snapshots VALUES(?,?)",
                ("session", json.dumps(dict(lines=[(1, "Alice", "Complete final speech.")]))),
            )
        self.assertEqual(self.save()["segments"], 1)
        saved = json.loads(self.wav.with_suffix(".live.json").read_text())
        self.assertEqual(saved["journal_cards"], [])
