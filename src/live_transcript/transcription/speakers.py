"""Conservative speaker labels from recorded observations, never future samples."""

import bisect
import json
import math
from pathlib import Path
import threading


class JSONLinesTail:
    """Incremental complete JSON rows, with replacement/truncation detection."""

    def __init__(self):
        self.key = None
        self.offset = 0
        self.partial = b""
        self.anchor = b""
        self.modified = None

    def read(self, path):
        reset = False
        try:
            with Path(path).open("rb") as source:
                import os

                stat = os.fstat(source.fileno())
                key = (str(Path(path)), stat.st_dev, stat.st_ino)
                if key != self.key or stat.st_size < self.offset:
                    reset = True
                elif self.offset:
                    source.seek(self.offset - len(self.anchor))
                    reset = source.read(len(self.anchor)) != self.anchor
                    reset |= stat.st_size == self.offset and stat.st_mtime_ns != self.modified
                if reset:
                    self.offset, self.partial, self.anchor = 0, b"", b""
                self.key = key
                self.modified = stat.st_mtime_ns
                source.seek(self.offset)
                data = source.read()
                self.offset += len(data)
                self.anchor = (self.anchor + data)[-128:]
                lines = (self.partial + data).split(b"\n")
                self.partial = lines.pop()
                result = []
                for line in lines:
                    try:
                        value = json.loads(line.decode("utf-8", errors="replace"))
                        if isinstance(value, dict):
                            result.append(value)
                    except ValueError:
                        pass
                return reset, result
        except OSError:
            reset = self.key is not None
            self.key, self.offset, self.partial, self.anchor = None, 0, b"", b""
            return reset, []


class LiveTimeline:
    """Cache speaker transitions and overlay checked bot speech intervals."""

    def __init__(self):
        self.lock = threading.Lock()
        self.speakers, self.voice = JSONLinesTail(), JSONLinesTail()
        self.events, self.voices, self.cached = [], [], []
        self.observed_until = 0

    def read(self, speaker_path, voice_path):
        with self.lock:
            reset, speakers = self.speakers.read(speaker_path)
            voice_reset, voices = self.voice.read(voice_path)
            if reset:
                self.events, self.observed_until = [], 0
            if voice_reset:
                self.voices = []
            for row in speakers:
                try:
                    at = float(row["t"])
                    if not math.isfinite(at) or at < 0:
                        continue
                    people = row.get("who", [])
                    if not isinstance(people, list):
                        continue
                    names = {
                        p["name"]
                        for p in people
                        if isinstance(p, dict)
                        and p.get("role") == "active"
                        and isinstance(p.get("name"), str)
                    }
                    name = next(iter(names)) if len(names) == 1 and not row.get("error") else ""
                    if "recording" in name.lower() or "live transcript" in name.lower():
                        name = ""
                    if at < self.observed_until:
                        continue  # an out-of-order sample cannot relabel earlier speech
                    self.observed_until = at
                    if not self.events or self.events[-1][1] != name:
                        self.events.append((at, name))
                except (KeyError, TypeError, ValueError):
                    continue
            for row in voices:
                try:
                    start, duration = float(row["t"]), float(row["duration"])
                    name = row.get("speaker_label") or row.get("name")
                    if (
                        not all(math.isfinite(t) for t in (start, duration))
                        or start < 0
                        or duration <= 0
                        or not isinstance(name, str)
                    ):
                        continue
                    self.voices.append((start, start + duration, name))
                except (KeyError, TypeError, ValueError):
                    continue
            if reset or voice_reset or speakers or voices:
                result = list(self.events)
                for start, end, name in self.voices:
                    result = [(t, n) for t, n in result if not start <= t < end]
                    result.extend([(start, name), (end, "")])
                self.cached = sorted(result)
            return list(self.cached)


def rows(path):
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result.append(value)
        except ValueError:
            pass
    return result


def load(speaker_file, voice_file):
    observations = []
    for row in rows(speaker_file):
        try:
            at = float(row["t"])
        except (KeyError, ValueError, TypeError):
            continue
        if not math.isfinite(at) or at < 0:
            continue
        people = row.get("who", [])
        if not isinstance(people, list):
            people = []
        names = {
            person["name"]
            for person in people
            if isinstance(person, dict)
            and person.get("role") == "active"
            and isinstance(person.get("name"), str)
            and person["name"]
        }
        name = next(iter(names)) if len(names) == 1 and not row.get("error") else None
        observations.append((at, name))
    observations.sort(key=lambda row: row[0])
    times = [t for t, _ in observations]
    voices = []
    for voice in rows(voice_file):
        try:
            start = float(voice["t"])
            duration = float(voice["duration"])
        except (KeyError, ValueError, TypeError):
            continue
        if math.isfinite(start) and math.isfinite(duration) and start >= 0 and duration > 0:
            voices.append(dict(voice, t=start, duration=duration))

    def speaker_at(at):
        for voice in voices:
            if voice["t"] <= at < voice["t"] + voice["duration"]:
                return voice.get("speaker_label") or voice.get("name")
        index = bisect.bisect_right(times, at) - 1
        if index >= 0 and at - times[index] <= 2:
            return observations[index][1]
        return None

    return speaker_at, len(observations) + len(voices)
