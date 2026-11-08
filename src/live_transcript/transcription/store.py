"""Bounded local journal: acknowledge audio only after a durable SQLite commit.

SQLite stays on local disk (never NFS). Large finished recordings live on the
data volume. Checkpoints atomically replace session state and retire ingested
packets; per-stream receipts survive packet retirement and daemon restarts.
"""

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time


@contextmanager
def journal_owner(path):
    """One decoder owner across native/container deployments of the same state."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + ".owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise RuntimeError("Caption journal lock belongs to another user")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another caption service owns this journal") from exc
        os.fchmod(descriptor, 0o600)
        yield
    finally:
        # Keep the inode in place: unlinking a lock lets a new process acquire a
        # different inode while an existing decoder still owns the old one.
        os.close(descriptor)


def encode_state(st):
    out = {k: v for k, v in st.items() if k not in ("busy", "busy_since", "generation")}
    for key in ("pcm", "utterance_pcm", "preroll"):
        out[key] = base64.b64encode(bytes(st.get(key, b""))).decode("ascii")
    out.pop("reconcile_from", None)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def decode_state(raw):
    st = json.loads(raw)
    for key in ("pcm", "utterance_pcm", "preroll"):
        st[key] = bytearray(base64.b64decode(st.get(key, "")))
    st.update(busy=False, generation=0)
    return st


class CaptionStore:
    def __init__(self, path, max_pending_bytes=512 * 1024 * 1024):
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA wal_autocheckpoint=256")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS receipts (
                session TEXT, stream TEXT, seq INTEGER, digest TEXT,
                PRIMARY KEY (session, stream));
            CREATE TABLE IF NOT EXISTS packets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, meta TEXT, pcm BLOB);
            CREATE INDEX IF NOT EXISTS packet_session ON packets(session, id);
            CREATE TABLE IF NOT EXISTS snapshots (
                session TEXT PRIMARY KEY, updated REAL, state TEXT);
            CREATE TABLE IF NOT EXISTS cards (
                session TEXT, t REAL, speaker TEXT, text TEXT,
                PRIMARY KEY (session, t, speaker));
            CREATE TABLE IF NOT EXISTS packet_receipts (
                session TEXT, stream TEXT, seq INTEGER, pcm_digest TEXT, meta_digest TEXT,
                PRIMARY KEY (session, stream, seq));
        """)
        self.max_pending_bytes = max_pending_bytes
        # A single decode/correction may add more rows than the camera window.
        # Start from zero after a restart so legacy incomplete tables self-heal
        # on the next checkpoint without trusting a cached row count.
        self._line_counts = {}

    def accept(self, session, meta, pcm):
        if type(meta.get("seq")) is not int:
            raise ValueError("packet sequence must be an integer")
        stream, seq = str(meta["stream"]), int(meta["seq"])
        if not session or len(session) > 200 or not stream or len(stream) > 200 or seq < 0:
            raise ValueError("invalid packet identity")
        digest = hashlib.sha256(pcm).hexdigest()
        metadata = json.dumps(
            meta, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        meta_digest = hashlib.sha256(metadata.encode()).hexdigest()
        with self.lock, self.db:
            prev = self.db.execute(
                "SELECT seq,digest FROM receipts WHERE session=? AND stream=?", (session, stream)
            ).fetchone()
            if prev and seq <= prev[0]:
                receipt = self.db.execute(
                    "SELECT pcm_digest,meta_digest FROM packet_receipts "
                    "WHERE session=? AND stream=? AND seq=?",
                    (session, stream, seq),
                ).fetchone()
                if receipt:
                    if receipt != (digest, meta_digest):
                        raise ValueError("packet identity reused with different audio or metadata")
                    return False
                if seq < prev[0]:
                    raise ValueError(
                        "legacy replay cannot be verified; retain sender audio for recovery"
                    )
                if seq == prev[0] and digest != prev[1]:
                    raise ValueError("packet identity reused with different audio")
                return False
            expected = prev[0] + 1 if prev else 0
            if seq != expected:
                raise ValueError(f"out of order packet: expected {expected}, received {seq}")
            size = self.db.execute("SELECT COALESCE(SUM(LENGTH(pcm)),0) FROM packets").fetchone()[0]
            if size + len(pcm) > self.max_pending_bytes:
                raise BufferError("journal full: retain audio on sender and retry")
            self.db.execute(
                "INSERT INTO packets(session,meta,pcm) VALUES(?,?,?)",
                (session, json.dumps(meta, ensure_ascii=False), pcm),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)", (session, stream, seq, digest)
            )
            self.db.execute(
                "INSERT INTO packet_receipts VALUES(?,?,?,?,?)",
                (session, stream, seq, digest, meta_digest),
            )
        return True

    def pending(self, session, after=0, limit=16):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,meta,pcm FROM packets WHERE session=? AND id>? ORDER BY id LIMIT ?",
                (session, after, limit),
            ).fetchall()
        return [(i, json.loads(m), bytes(p)) for i, m, p in rows]

    def sessions(self):
        with self.lock:
            return [
                r[0]
                for r in self.db.execute(
                    "SELECT session FROM packets UNION SELECT session FROM snapshots"
                )
            ]

    def restore(self, session):
        with self.lock:
            row = self.db.execute(
                "SELECT state FROM snapshots WHERE session=?", (session,)
            ).fetchone()
        return decode_state(row[0]) if row else None

    def checkpoint(self, session, state):
        raw = encode_state(state)
        with self.lock:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO snapshots VALUES(?,?,?)", (session, time.time(), raw)
                )
                lines = state.get("lines", [])
                start = max(0, min(self._line_counts.get(session, 0), len(lines)) - 12)
                if "reconcile_from" in state:
                    self.db.execute(
                        "DELETE FROM cards WHERE session=? AND t>=?",
                        (session, state["reconcile_from"]),
                    )
                    # Reconciliation can replace a long suffix and shift its first
                    # timestamp backwards. Sync the entire canonical view here.
                    start = 0
                self.db.executemany(
                    "INSERT OR REPLACE INTO cards VALUES(?,?,?,?)",
                    [(session, t, speaker, text) for t, speaker, text in lines[start:] if text],
                )
                self.db.execute(
                    "DELETE FROM packets WHERE session=? AND id<=?",
                    (session, state.get("ingest_id", 0)),
                )
            self._line_counts[session] = len(lines)
            state.pop("reconcile_from", None)

    def backlog_seconds(self, session, after=0):
        with self.lock:
            size = self.db.execute(
                "SELECT COALESCE(SUM(LENGTH(pcm)),0) FROM packets WHERE session=? AND id>?",
                (session, after),
            ).fetchone()[0]
        return size / 32000

    def reset(self, session):
        with self.lock:
            with self.db:
                for table in ("receipts", "packets", "snapshots", "cards", "packet_receipts"):
                    self.db.execute(f"DELETE FROM {table} WHERE session=?", (session,))
            self._line_counts.pop(session, None)

    def transcript(self, session):
        with self.lock:
            return self.db.execute(
                "SELECT t,speaker,text FROM cards WHERE session=? ORDER BY t", (session,)
            ).fetchall()

    def close(self):
        with self.lock:
            self.db.close()
