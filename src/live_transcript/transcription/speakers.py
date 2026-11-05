"""Conservative speaker labels from recorded observations, never future samples."""

import bisect
import json
import math
from pathlib import Path


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
