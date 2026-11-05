"""Export settled live caption evidence independently of final recognition.

Read the local journal without changing it. Keep a local copy before touching
archive storage. A missing/reset session never erases an earlier export.
"""

import json
import os
from pathlib import Path
import sqlite3
import time

from live_transcript.capture.guard import atomic_json
from live_transcript.core.config import get
from live_transcript.transcription.quality import assess


def export(wav, db_path=None, local_root=None):
    wav = Path(wav)
    session = wav.stem
    database = Path(
        db_path or os.environ.get("CAPTION_DB") or Path(get("paths", "state")) / "captions.sqlite3"
    )
    if not database.is_file():
        return dict(status="unavailable", reason="journal_missing")
    # mode=ro prevents accidental creation/migration; SQLite reads a consistent
    # snapshot, including committed WAL records from the running service.
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    snapshot = None
    try:
        with connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT t,speaker,text FROM cards WHERE session=? ORDER BY t,speaker", (session,)
            ).fetchall()
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'"
            ).fetchone():
                checkpoint = connection.execute(
                    "SELECT state FROM snapshots WHERE session=?", (session,)
                ).fetchone()
                if checkpoint:
                    snapshot = json.loads(checkpoint[0])
    finally:
        connection.close()
    cards = [dict(t=t, speaker=speaker, text=text) for t, speaker, text in rows]
    # Both views are evidence. Older checkpoint code could omit corrected rows
    # from cards even though the complete lines were durably in the snapshot.
    lines = snapshot.get("lines", []) if isinstance(snapshot, dict) else []
    utterances = (
        [dict(t=t, speaker=speaker, text=text) for t, speaker, text in lines if text]
        if lines
        else cards
    )
    if not utterances:
        return dict(status="unavailable", reason="no_matching_session")
    payload = dict(
        version=2,
        session=session,
        exported_utc=time.time(),
        source="live_caption_journal",
        view="checkpoint_lines" if lines else "journal_cards",
        utterances=utterances,
        journal_cards=cards,
        decoder_snapshot=snapshot,
        # Review flags retain every word; they are not corrections.
        quality=assess(utterances, min_run_segments=8),
    )
    local = Path(local_root or Path(get("paths", "state")) / "live-evidence")
    local.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = local / (session + ".live.json")
    try:
        previous = json.loads(target.read_text())
        if len(previous.get("utterances", [])) > len(utterances):
            payload = previous  # do not replace complete evidence after a reset
    except (OSError, ValueError, TypeError):
        pass
    atomic_json(target, payload)
    atomic_json(wav.with_suffix(".live.json"), payload)
    return dict(
        status="saved",
        segments=len(payload["utterances"]),
        local=str(target),
        quality=payload["quality"]["status"],
    )
