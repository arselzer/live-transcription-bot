"""Read-only, session-scoped views of the durable caption journal.

The caller selects the session from the pinned VM, never an HTTP parameter.
Only text fields cross this boundary; audio buffers, file paths and credentials
in decoder checkpoints are deliberately not serialized.
"""

import math
from pathlib import Path
import re
import sqlite3
import time
import json
from live_transcript.core.options import PROVIDERS


def validate_session(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise ValueError("Meeting identity is unavailable")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value["id"]):
        raise ValueError("Meeting identity is unavailable")
    return dict(
        id=value["id"],
        provider=value.get("provider") if value.get("provider") in PROVIDERS else "",
    )


def read(database, session, limit=500):
    session = validate_session(session)
    result = dict(
        session=session,
        lines=[],
        board=None,
        status="waiting",
        updated_at=None,
        truncated=False,
        draft=False,
    )
    if session is None:
        return result
    database = Path(database)
    if not database.is_file():
        result["status"] = "unavailable"
        return result
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute("PRAGMA query_only=ON")
        # Bound work even if an external journal is unexpectedly large or corrupt.
        deadline = time.monotonic() + 1
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT updated, json_extract(state,'$.board'), "
            "json_extract(state,'$.cand') IS NOT NULL AND "
            "COALESCE(json_extract(state,'$.cand'),'') != '' FROM snapshots WHERE session=?",
            (session["id"],),
        ).fetchone()
        # Checkpoint lines are the canonical revised text, including corrections
        # that legacy card tables may not contain. SQL excludes PCM from Python.
        rows = connection.execute(
            "SELECT j.value FROM snapshots AS s, json_each(s.state,'$.lines') AS j "
            "WHERE s.session=? ORDER BY CAST(j.key AS INTEGER) DESC LIMIT ?",
            (session["id"], limit + 1),
        ).fetchall()
        if row:
            updated, board, draft = row
            if type(updated) not in (int, float) or not math.isfinite(updated) or updated < 0:
                raise ValueError("Invalid caption checkpoint timestamp")
            result.update(updated_at=updated, draft=bool(draft))
            if board:
                data = json.loads(board)
                if isinstance(data, dict) and isinstance(data.get("bullets"), list):
                    result["board"] = dict(
                        kind=data.get("kind")
                        if data.get("kind") in ("summary", "topic", "decision", "action")
                        else "summary",
                        bullets=[s[:500] for s in data["bullets"][:3] if isinstance(s, str)],
                    )
        lines = [json.loads(item[0]) for item in reversed(rows[:limit])]
        result["truncated"] = len(rows) > limit
        if not row:
            lines = list(
                reversed(
                    connection.execute(
                        "SELECT t,speaker,text FROM cards WHERE session=? ORDER BY t DESC,speaker DESC LIMIT ?",
                        (session["id"], limit + 1),
                    ).fetchall()
                )
            )
            result["truncated"] = len(lines) > limit
            lines = lines[-limit:]
        budget = 2 * 1024 * 1024  # stay below the hub's fixed response limit
        for item in reversed(lines):
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 3
                or type(item[0]) not in (int, float)
                or not math.isfinite(item[0])
                or item[0] < 0
                or not all(isinstance(s, str) for s in item[1:])
            ):
                raise ValueError("Invalid caption journal text")
            line = dict(t=item[0], speaker=item[1][:200], text=item[2][:16000])
            budget -= len(json.dumps(line).encode()) + 2
            if budget < 0:
                result["truncated"] = True
                break
            result["lines"].append(line)
        result["lines"].reverse()
        result["status"] = "available" if row or lines else "waiting"
        return result
    finally:
        connection.close()
