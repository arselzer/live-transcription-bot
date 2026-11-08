"""Local recording catalogue and bounded, read-only access to retained evidence.

HTTP selects opaque catalogue IDs and named artifacts, never filesystem paths.
Indexing runs outside the request server so unavailable archive storage cannot
block live status/control. The catalogue contains private transcript text.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import wave

from live_transcript.core.config import get

ARTIFACTS = {
    "audio": (".wav", "audio/wav"),
    "video": ("-burned.mp4", "video/mp4"),
    "screen": (".mkv", "video/x-matroska"),
    "text": (".txt", "text/plain; charset=utf-8"),
    "subtitles": (".srt", "text/plain; charset=utf-8"),
    "notes": (".md", "text/plain; charset=utf-8"),
}
ID = re.compile(r"[a-f0-9]{32}")


def roots():
    return [Path(get("paths", key)) for key in ("recordings", "safety_copies")]


def database():
    return Path(get("paths", "state")) / "library.sqlite3"


@contextmanager
def evidence(root, relative):
    """Resolve each component against an open directory, rejecting symlink escapes."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in (".", "..") for p in parts):
        raise ValueError("Invalid evidence name")
    descriptors = []
    try:
        descriptor = os.open(Path(root).resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            descriptors.append(descriptor)
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            os.close(descriptor)
            raise ValueError("Evidence must be a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            yield stream, details
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read(root, relative, limit=4 * 1024**2):
    with evidence(root, relative) as (stream, details):
        if details.st_size > limit:
            raise ValueError("Evidence metadata exceeds its limit")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Evidence metadata grew beyond its limit")
        return data


def optional(root, relative, *, json_value=True, limit=4 * 1024**2):
    try:
        value = read(root, relative, limit).decode("utf-8")
        return json.loads(value) if json_value else value
    except (OSError, ValueError, UnicodeError):
        return None


def lines_from_srt(value):
    result = []
    for part in re.split(r"\r?\n\s*\r?\n", value or "")[:10000]:
        match = re.search(
            r"(\d+):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{3})[^\n]*\n(.+)",
            part,
            re.S,
        )
        if not match:
            continue
        g = match.groups()

        def stamp(at):
            return int(g[at]) * 3600 + int(g[at + 1]) * 60 + int(g[at + 2]) + int(g[at + 3]) / 1000

        result.append(dict(t=stamp(0), end=stamp(4), speaker="", text=g[8].strip()[:4000]))
    return result


def valid_lines(rows):
    result = []
    for row in rows[:10000] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        start, end, text = row.get("t"), row.get("end", row.get("t")), row.get("text")
        if (
            type(start) not in (int, float)
            or type(end) not in (int, float)
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end < start
            or not isinstance(text, str)
        ):
            continue
        speaker = row.get("speaker")
        result.append(
            dict(
                t=start,
                end=end,
                text=text[:4000],
                speaker=speaker[:120] if isinstance(speaker, str) else "",
            )
        )
    return result


def inspect_recording(root, stem):
    archive = stem + ".archive/"
    config = optional(root, archive + "config.json") or {}
    if not isinstance(config, dict):
        config = {}
    artifacts = {}
    modified = 0
    duration = None
    audio_fingerprint = None
    for key, (suffix, _) in ARTIFACTS.items():
        try:
            with evidence(root, stem + suffix) as (stream, details):
                artifacts[key] = dict(bytes=details.st_size, filename=stem + suffix)
                modified = max(modified, details.st_mtime)
                if key == "audio":
                    audio_fingerprint = dict(bytes=details.st_size, mtime_ns=details.st_mtime_ns)
                    try:
                        with wave.open(stream) as audio:
                            duration = audio.getnframes() / audio.getframerate()
                    except (wave.Error, EOFError, ZeroDivisionError):
                        artifacts.pop(key, None)
        except (OSError, ValueError):
            pass
    transcript = optional(root, stem + ".transcript.json")
    lines = valid_lines(transcript.get("lines")) if isinstance(transcript, dict) else []
    verified = (
        transcript.get("audio") == audio_fingerprint
        if isinstance(transcript, dict) and isinstance(transcript.get("audio"), dict)
        else None
    )
    source = "final"
    if not lines:
        lines = valid_lines(lines_from_srt(optional(root, stem + ".srt", json_value=False)))
    if not lines:
        saved = optional(root, stem + ".transcription.json")
        lines = valid_lines(saved.get("utterances")) if isinstance(saved, dict) else []
        source = "recognizer output"  # may precede the final quality/speaker pass
    if not lines:
        text = optional(root, stem + ".txt", json_value=False)
        for line in (text or "").splitlines()[:10000]:
            match = re.match(r"\[(\d+):(\d\d)\]\s*(.+)", line)
            if match:
                at = int(match[1]) * 60 + int(match[2])
                lines.append(dict(t=at, end=at, speaker="", text=match[3][:4000]))
        source = "saved text"
    notes = optional(root, stem + ".md", json_value=False, limit=200000)
    # Only allowlisted event fields enter the view. Raw events can contain paths.
    events = optional(root, archive + "events.jsonl", json_value=False) or ""
    raw = []
    origin = None
    for line in events.splitlines()[:50000]:
        try:
            item = json.loads(line)
            utc = item.get("utc")
            if type(utc) not in (int, float) or not math.isfinite(utc):
                continue
            if item.get("kind") == "primary_capture_started":
                origin = utc
            if item.get("kind") in (
                "capture_interrupted",
                "capture_stalled",
                "audio_server_interrupted",
                "audio_server_recovered",
                "browser_audio_output_reset",
            ):
                raw.append(
                    dict(
                        utc=utc,
                        kind=item["kind"],
                        component=item.get("component")
                        if item.get("component") in ("audio", "screen", "snapshots", "observer")
                        else "audio",
                    )
                )
        except (ValueError, AttributeError):
            continue
    events = [
        dict(item, t=max(0, item["utc"] - origin) if origin is not None else None, estimated=True)
        for item in raw[-100:]
    ]
    processing = optional(root, stem + ".postprocess-status.json")
    status = processing.get("status") if isinstance(processing, dict) else None
    try:
        from live_transcript.runtime.application import cancellation_file

        cancel = json.loads(cancellation_file(root / (stem + ".wav")).read_text())
        previous = processing.get("utc", 0) if isinstance(processing, dict) else 0
        if type(previous) not in (int, float):
            previous = 0
        if (
            type(cancel.get("utc")) in (int, float)
            and cancel["utc"] > previous
            and audio_fingerprint
            and cancel["utc"] >= audio_fingerprint["mtime_ns"] / 1e9
        ):
            status = "cancelled"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    capture = optional(root, archive + "health.json")
    if (
        isinstance(capture, dict)
        and type(capture.get("utc")) in (int, float)
        and not capture.get("stopped")
        and 0 <= time.time() - capture["utc"] < 90
    ):
        status = "recording"
    if not status:
        status = "available" if artifacts else "evidence only"
    quality = optional(root, stem + ".transcript-quality.json")
    return dict(
        title=stem,
        updated_at=modified or config.get("started", 0),
        provider=config.get("provider")
        if config.get("provider") in ("zoom", "proton", "google")
        else "",
        duration=duration,
        status=str(status)[:80],
        artifacts=artifacts,
        lines=lines,
        transcript_source=source,
        audio_matches_transcript=verified,
        notes=notes,
        events=events,
        quality_passed=quality.get("ok")
        if isinstance(quality, dict) and type(quality.get("ok")) is bool
        else None,
    )


def scan():
    """Incremental worker with bounded per-record memory and an external timeout."""
    path = database()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path, timeout=2)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS recordings(id TEXT PRIMARY KEY, stem TEXT, root INTEGER, updated REAL, metadata TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS catalogue_state(key TEXT PRIMARY KEY, value TEXT)"
        )
        deadline = time.monotonic() + 18
        candidates, roots_complete = {}, True
        for index, root in enumerate(roots()):
            try:
                with os.scandir(root) as entries:
                    for n, entry in enumerate(entries):
                        if n >= 10000 or time.monotonic() > deadline:
                            roots_complete = False
                            break
                        if entry.is_symlink():
                            continue
                        if entry.name.endswith(".archive") and entry.is_dir(follow_symlinks=False):
                            stem = entry.name[:-8]
                        elif entry.name.endswith(".wav") and entry.is_file(follow_symlinks=False):
                            stem = entry.name[:-4]
                        else:
                            continue
                        if not stem or len(stem) > 200 or any(ord(c) < 32 for c in stem):
                            continue
                        ident = hashlib.sha256(stem.encode()).hexdigest()[:32]
                        modified = entry.stat(follow_symlinks=False).st_mtime
                        previous = candidates.get(ident)
                        if previous is None:
                            candidates[ident] = (stem, index, modified)
                        elif previous[1] == index:
                            candidates[ident] = (stem, index, max(previous[2], modified))
            except OSError:
                if index == 0 or root.exists():
                    roots_complete = False
        ordered = sorted(candidates, key=lambda ident: (candidates[ident][2], ident), reverse=True)
        saved = connection.execute(
            "SELECT value FROM catalogue_state WHERE key='cursor'"
        ).fetchone()
        cursor = int(saved[0]) % max(1, len(ordered)) if saved else 0
        # Recent meetings are always refreshed; a cursor prevents older meetings
        # from starving in a large library. Never retain all transcript JSON in RAM.
        batch = list(dict.fromkeys(ordered[:30] + ordered[cursor : cursor + 300]))
        indexed = 0
        source_roots = roots()
        for ident in batch:
            if time.monotonic() > deadline:
                break
            stem, index, _ = candidates[ident]
            try:
                value = inspect_recording(source_roots[index], stem)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            updated = value["updated_at"]
            if type(updated) not in (int, float) or not math.isfinite(updated):
                updated = 0
                value["updated_at"] = 0
            with connection:
                connection.execute(
                    "INSERT OR REPLACE INTO recordings VALUES(?,?,?,?,?)",
                    (ident, stem, index, updated, json.dumps(value)),
                )
            indexed += 1
        with connection:
            old = {row[0] for row in connection.execute("SELECT id FROM recordings")}
            if roots_complete:
                connection.executemany(
                    "DELETE FROM recordings WHERE id=?",
                    [(ident,) for ident in old if ident not in candidates],
                )
            complete = roots_complete and set(candidates).issubset(old)
            connection.execute(
                "INSERT OR REPLACE INTO catalogue_state VALUES('scan',?)",
                (json.dumps(dict(checked_at=time.time(), complete=complete)),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO catalogue_state VALUES('cursor',?)",
                (str((cursor + max(1, indexed - 30)) % max(1, len(ordered))),),
            )
        return dict(indexed=indexed, complete=complete)
    finally:
        connection.close()


@contextmanager
def catalogue():
    connection = sqlite3.connect(database().resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def listing(query="", offset=0):
    if (
        not isinstance(query, str)
        or len(query) > 100
        or type(offset) is not int
        or not 0 <= offset <= 10000
    ):
        raise ValueError("Invalid recording search")
    if not database().is_file():
        return dict(recordings=[], scan=None, next_offset=None)
    with catalogue() as connection:
        term = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = connection.execute(
            "SELECT id,metadata FROM recordings WHERE stem LIKE ? ESCAPE '\\' ORDER BY updated DESC,id LIMIT 51 OFFSET ?",
            (term, offset),
        ).fetchall()
        state = connection.execute("SELECT value FROM catalogue_state WHERE key='scan'").fetchone()
        items = []
        for ident, value in rows[:50]:
            data = json.loads(value)
            items.append(
                dict(
                    id=ident,
                    **{
                        key: data[key]
                        for key in (
                            "title",
                            "updated_at",
                            "provider",
                            "duration",
                            "status",
                            "artifacts",
                        )
                    },
                )
            )
        return dict(
            recordings=items,
            scan=json.loads(state[0]) if state else None,
            next_offset=offset + 50 if len(rows) > 50 else None,
        )


def detail(ident):
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise ValueError("Invalid recording identifier")
    with catalogue() as connection:
        row = connection.execute("SELECT metadata FROM recordings WHERE id=?", (ident,)).fetchone()
        if not row:
            raise FileNotFoundError("Recording is not in the catalogue")
        return dict(json.loads(row[0]), id=ident)


@contextmanager
def artifact(ident, kind):
    if not isinstance(ident, str) or not ID.fullmatch(ident) or kind not in ARTIFACTS:
        raise ValueError("Invalid recording artifact")
    with catalogue() as connection:
        row = connection.execute(
            "SELECT stem,root,metadata FROM recordings WHERE id=?", (ident,)
        ).fetchone()
    if not row or kind not in json.loads(row[2])["artifacts"]:
        raise FileNotFoundError("Recording artifact is unavailable")
    stem, index, _ = row
    with evidence(roots()[index], stem + ARTIFACTS[kind][0]) as result:
        yield result[0], result[1], ARTIFACTS[kind][1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true")
    args = parser.parse_args()
    print(json.dumps(scan() if args.scan else listing()))


if __name__ == "__main__":
    main()
