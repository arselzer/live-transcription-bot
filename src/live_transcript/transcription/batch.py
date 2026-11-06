"""Bounded full-model transcription with independent chunk retries and quality checks."""

from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import wave

from live_transcript.core.config import get
from live_transcript.transcription.quality import require_acceptable, TranscriptQualityError


def glossary_text():
    selected = get("paths", "glossary")
    return Path(selected).read_text().strip()[:900] if selected else ""


def cache_signature(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    models = {}
    for key in ("model", "vad", "whisper_cli"):
        model = Path(get("paths", key))
        try:
            stat = model.stat()
            models[key] = dict(path=str(model), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        except FileNotFoundError:
            models[key] = dict(path=str(model), missing=True)
    prompt = glossary_text()
    return dict(
        audio_sha256=digest.hexdigest(),
        models=models,
        decoder_policy=2,
        glossary_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        chunk_seconds=get("postprocess", "chunk_seconds"),
        retry_chunk_seconds=get("postprocess", "retry_chunk_seconds"),
    )


TIME = r"(\d+):(\d\d):(\d\d)[.,](\d{1,3})"
SEGMENT = re.compile(r"\[" + TIME + r"\s*-->\s*" + TIME + r"\]\s*(.+)")


def seconds(parts):
    h, m, s, fraction = parts
    if int(m) >= 60 or int(s) >= 60:
        raise ValueError("invalid recognizer timestamp")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(fraction) / (10 ** len(fraction))


def parse_output(stdout, offset=0):
    rows = []
    for line in stdout.splitlines():
        match = SEGMENT.search(line)
        if re.match(r"^\s*\[\d", line) and match is None:
            raise ValueError("unparsed recognizer timestamp line")
        if match and match[9].strip():
            start, end = seconds(match.groups()[:4]), seconds(match.groups()[4:8])
            if end < start:
                raise ValueError("recognizer returned negative segment duration")
            rows.append(dict(t=offset + start, end=offset + end, text=match[9].strip()))
    return rows


def voiced_offset(source, from_s, to_s, floor=300):
    """First moment at or after from_s where audio clears a noise floor.
    Auto language detection on silence is arbitrary: callers skip dead air."""
    import array

    with wave.open(str(source)) as w:
        rate = w.getframerate()
        step = max(1, int(rate * 0.1))
        w.setpos(min(int(from_s * rate), w.getnframes()))
        t = w.tell() / rate
        limit = min(w.getnframes(), int(to_s * rate))
        while w.tell() < limit:
            buf = w.readframes(step)
            if not buf:
                return None
            a = array.array("h", buf[: len(buf) - (len(buf) % 2)])
            if a and (sum(x * x for x in a) / len(a)) ** 0.5 >= floor:
                return t
            t += len(buf) / 2 / rate
    return None


def voiced_pieces(source, from_s, to_s, floor=300, cap=8.0):
    """(start,end) spans of voiced audio in [from_s,to_s], up to cap seconds.
    Concatenated, these give language detection a window of actual speech.
    The cap keeps the decision anchored to the chunk's opening language."""
    import array

    pieces = []
    with wave.open(str(source)) as w:
        rate = w.getframerate()
        step = max(1, int(rate * 0.1))
        w.setpos(min(int(from_s * rate), w.getnframes()))
        limit = min(w.getnframes(), int(to_s * rate))
        while w.tell() < limit and sum(e - s for s, e in pieces) < cap:
            buf = w.readframes(step)
            if not buf:
                break
            a = array.array("h", buf[: len(buf) - (len(buf) % 2)])
            if not a or (sum(x * x for x in a) / len(a)) ** 0.5 < floor:
                continue
            start = max(from_s, w.tell() / rate - 0.3)
            span = len(buf) / 2 / rate
            while w.tell() < limit:
                nb = w.readframes(step)
                if not nb:
                    break
                na = array.array("h", nb[: len(nb) - (len(nb) % 2)])
                if not na or (sum(x * x for x in na) / len(na)) ** 0.5 < floor:
                    break
                span += len(nb) / 2 / rate
            pieces.append((start, min(to_s, start + span + 0.3)))
    return pieces


def detect_language(path, runner, env, ffmpeg, from_s, to_s, scratch):
    """Explicit language for a silence-led chunk, detected on voiced speech
    only. None means fall back to the recognizer's own auto-detection."""
    pieces = voiced_pieces(path, from_s, to_s)
    if not pieces:
        return None
    det = Path(scratch) / "detect.wav"
    args = [ffmpeg, "-nostdin", "-v", "error"]
    for s, e in pieces:
        args += ["-ss", str(round(s, 3)), "-t", str(round(e - s, 3)), "-i", str(path)]
    chain = "".join(f"[{i}:a]" for i in range(len(pieces)))
    args += [
        "-filter_complex",
        f"{chain}concat=n={len(pieces)}:v=0:a=1[a]",
        "-map",
        "[a]",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-y",
        str(det),
    ]
    try:
        runner(args, check=True, capture_output=True, text=True, timeout=120)
        result = runner(
            [
                get("paths", "whisper_cli"),
                "-m",
                get("paths", "model"),
                "-l",
                "auto",
                "-dl",
                str(det),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=get("postprocess", "timeout_seconds"),
            env=env,
        )
    except Exception:
        return None
    found = re.search(r"auto-detected language:\s*([a-z]+)", result.stderr or "", re.I)
    return found[1].lower() if found else None


def transcribe_wav(path, env=None, runner=None, progress=None):
    """Keep original audio untouched. No bad chunk is silently skipped."""
    runner = runner or subprocess.run
    with wave.open(str(path)) as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("postprocessing requires 16 kHz mono PCM16 WAV")
        duration = audio.getnframes() / audio.getframerate()
    signature = cache_signature(path)
    cache = Path(path).with_suffix(".transcription.json")
    try:
        saved = json.loads(cache.read_text())
        if isinstance(saved, dict) and saved.get("signature") == signature:
            rows = saved["utterances"]
            quality = require_acceptable(rows, duration)
            return rows, dict(
                duration_seconds=duration,
                chunks=saved.get("chunks", 0),
                retries=saved.get("retries", []),
                languages=saved.get("languages", []),
                quality=quality,
                signature=signature,
                cache_reused=True,
            )
    except (OSError, ValueError, KeyError, TypeError, TranscriptQualityError):
        pass
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg is required for bounded transcription")
    scratch = Path(get("paths", "state")) / "postprocess-scratch"
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    chunk = get("postprocess", "chunk_seconds")
    retry = get("postprocess", "retry_chunk_seconds")
    glossary = glossary_text()
    utterances = []
    languages = []
    retried = []
    count = 0
    with tempfile.TemporaryDirectory(prefix="batch-", dir=scratch) as directory:
        clip = Path(directory) / "audio.wav"

        def decode(at, length, repair=False):
            # Retain crossing segments: dropping by start alone loses words
            # spoken across the boundary. A small amount of repeated context
            # is preferable to silently losing speech.
            start = max(0, at - 2) if at > 0 else 0
            language = None
            onset = voiced_offset(path, start, at + length)
            if onset is not None and onset > start + 10:
                # Silence-led chunk: decide language on concatenated voiced
                # speech, not on the dead air that happens to open the clip.
                language = detect_language(path, runner, env, ffmpeg, onset, at + length, directory)
                start = max(0, onset - 2)
            runner(
                [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    str(start),
                    "-t",
                    str(length + (at - start)),
                    "-i",
                    str(path),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-y",
                    str(clip),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            # Detect each independent chunk: a German opening must not lock
            # later English speech to German (or the reverse).
            command = [
                get("paths", "whisper_cli"),
                "-m",
                get("paths", "model"),
                "-l",
                language or "auto",
            ]
            if glossary:
                command += ["--prompt", glossary]
            vad = get("paths", "vad")
            if Path(vad).is_file():
                command += ["--vad", "-vm", vad]
            if repair:
                command += ["-mc", "0", "-bs", "5"]
            result = runner(
                [*command, str(clip)],
                check=True,
                capture_output=True,
                text=True,
                timeout=get("postprocess", "timeout_seconds"),
                env=env,
            )
            parsed = parse_output(result.stdout, offset=start)
            local = [dict(r, t=max(at, r["t"])) for r in parsed if r["end"] > at]
            # Successful exit with changed/unparseable output is not silence.
            if not parsed and result.stdout.strip():
                raise ValueError("recognizer timestamps could not be parsed")
            detected = re.search(r"auto-detected language:\s*([a-z]+)", result.stderr or "", re.I)
            if detected:
                languages.append(detected[1].lower())
            elif language:
                languages.append(language)
            # Rows use meeting-relative timestamps, including later chunks.
            require_acceptable(local, at + length)
            return local

        for at in range(0, int(duration) + 1, chunk):
            if at >= duration:
                break
            length = min(chunk, duration - at)
            count += 1
            if progress:
                progress(
                    dict(
                        status="transcribing",
                        chunk=count,
                        audio_offset=at,
                        duration_seconds=duration,
                    )
                )
            try:
                rows = decode(at, length)
            except (TranscriptQualityError, subprocess.SubprocessError, ValueError) as exc:
                retried.append(dict(start=at, seconds=length, reason=type(exc).__name__))
                if length <= retry:
                    raise
                rows = []
                for small in range(0, int(length) + 1, retry):
                    if small >= length:
                        break
                    rows.extend(decode(at + small, min(retry, length - small), repair=True))
            utterances.extend(rows)
    quality = require_acceptable(utterances, duration)
    return utterances, dict(
        duration_seconds=duration,
        chunks=count,
        retries=retried,
        languages=sorted(set(languages)),
        quality=quality,
        signature=signature,
        cache_reused=False,
    )
