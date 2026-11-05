#!/usr/bin/env python3
"""Rebuild a timestamp-preserving STT WAV from the lossless audio archive.

Original lossless files and the original convenience WAV are always retained.
Gaps between capture generations become silence and are listed in recovery.json;
this keeps speaker/video timestamps aligned rather than hiding missing audio.
"""

import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import wave
import math


def json_rows(path):
    """A truncated final write must not discard the earlier durable evidence."""
    for line in Path(path).read_text().splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def available_frames(path):
    # ffmpeg may die before patching RIFF/data sizes. Never treat an advertised
    # 0xffffffff-byte data chunk as hours of actual recorded audio.
    with open(path, "rb") as fh, wave.open(fh, "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("Unsupported backup audio format")
        return min(source.getnframes(), max(0, (Path(path).stat().st_size - fh.tell()) // 2))


def segments(root):
    root = Path(root)
    config = json.loads((root / "config.json").read_text())
    generations = {}
    for item in json_rows(root / "events.jsonl"):
        if item.get("kind") == "capture_started" and item.get("component") == "audio":
            generations[item["generation"]] = item["utc"] - config["started"]
    result = []
    listed = set()
    for listing in root.glob("audio-*.csv"):
        generation = listing.stem.removeprefix("audio-")
        if generation not in generations:
            raise ValueError("Missing capture clock: " + generation)
        for row in csv.reader(listing.read_text().splitlines()):
            if len(row) != 3:
                continue  # crash-truncated CSV tail is recovered by file scan
            name, start, end = row
            try:
                start, end = float(start), float(end)
                if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end):
                    continue
            except ValueError:
                continue
            path = root / Path(name).name
            if not path.exists():
                raise FileNotFoundError(path)
            listed.add(path.name)
            result.append((max(0, generations[generation] + start), path, end - start))
    # SIGKILL may leave a decodable current FLAC before ffmpeg updates its CSV.
    # Include it, rather than silently discarding the newest captured speech.
    for path in root.glob("audio-*.flac"):
        if path.name in listed:
            continue
        generation, index = path.stem.removeprefix("audio-").rsplit("-", 1)
        if generation not in generations:
            raise ValueError("Missing capture clock: " + generation)
        at = generations[generation] + int(index) * config.get(
            "audio_segment_seconds", config.get("segment_seconds", 10)
        )
        result.append((max(0, at), path, None))
    return sorted(result)


def recover(root, output, replace=False):
    root, output = Path(root), Path(output)
    pieces = segments(root)
    if not pieces:
        raise ValueError("No finalized lossless audio segments found")
    if output.exists() and not replace:
        raise FileExistsError(output)
    tmp = output.with_name(output.stem + "." + uuid.uuid4().hex + ".tmp.wav")
    gaps, warnings, cursor, frames, backup_frames = [], [], 0, 0, 0
    # A separately captured monitor WAV can cover an archive encoder restart.
    # Use its recorded clock, never guess alignment from its filename or mtime.
    config = json.loads((root / "config.json").read_text())
    backups = []
    for item in json_rows(root / "events.jsonl"):
        try:
            if item.get("kind") != "primary_capture_started":
                continue
            name = item["file"]
            if Path(name).name != name:
                continue
            path = root / name
            if name == output.name:
                monitor = output.with_suffix(".monitor.wav")
                originals = sorted(output.parent.glob(output.stem + ".original-*.wav"))
                path = monitor if monitor.exists() else originals[0] if originals else output
            count = available_frames(path)
            at = max(0, round((item["utc"] - config["started"]) * 16000))
            backups.append((at, at + count, path))
        except (OSError, ValueError, KeyError, wave.Error, EOFError):
            continue
    backups.sort(reverse=True)

    def fill_gap(wav, begin, end):
        nonlocal backup_frames
        position, filled = begin, 0
        while position < end:
            match = next((b for b in backups if b[0] <= position < b[1]), None)
            if match:
                start, finish, path = match
                count = min(end - position, finish - position, 16000)
                with wave.open(str(path), "rb") as source:
                    source.setpos(position - start)
                    data = source.readframes(count)
                got = len(data) // 2
                wav.writeframesraw(data)
                position += got
                filled += got
                backup_frames += got
                if got < count:
                    wav.writeframesraw(b"\0\0" * (count - got))
                    position += count - got
            else:
                boundary = min([start for start, _, _ in backups if start > position] + [end])
                count = min(end - position, boundary - position, 16000)
                wav.writeframesraw(b"\0\0" * count)
                position += count
        if end - begin > 800:
            gaps.append(
                dict(
                    start=begin / 16000,
                    seconds=(end - begin) / 16000,
                    backup_seconds=filled / 16000,
                )
            )

    try:
        with wave.open(str(tmp), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            for start, path, duration in pieces:
                sample = round(start * 16000)
                gap = sample - cursor
                if gap > 0:
                    fill_gap(wav, cursor, sample)
                    cursor = sample
                decoded = subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(path),
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-f",
                        "s16le",
                        "-",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
                skip = max(0, cursor - sample) * 2
                data = decoded.stdout[skip:]
                wav.writeframesraw(data)
                cursor += len(data) // 2
                frames += len(data) // 2
                if decoded.returncode:
                    warnings.append("Interrupted or damaged segment: " + path.name)
            backup_end = max([end for _, end, _ in backups] + [cursor])
            if backup_end > cursor:
                fill_gap(wav, cursor, backup_end)
                cursor = backup_end
        if not frames + backup_frames:
            raise ValueError("Archive contained no audio frames")
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        if output.exists():
            backup = output.with_name(output.stem + f".original-{time.time_ns()}.wav")
            # Hardlink first: a crash before replacing the target retains both.
            os.link(output, backup)
        os.replace(tmp, output)
        report = dict(
            source=str(root),
            output=str(output),
            segments=len(pieces),
            duration_seconds=cursor / 16000,
            gaps=gaps,
            warnings=warnings,
        )
        from live_transcript.capture.guard import atomic_json

        atomic_json(output.with_suffix(".recovery.json"), report)
        return report
    finally:
        tmp.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive")
    ap.add_argument("output")
    ap.add_argument("--replace", action="store_true")
    a = ap.parse_args()
    print(json.dumps(recover(a.archive, a.output, a.replace), indent=2))


if __name__ == "__main__":
    main()
