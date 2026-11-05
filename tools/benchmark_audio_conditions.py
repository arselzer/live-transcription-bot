#!/usr/bin/env python3
"""Deterministic ASR stress matrix; no meeting join, playback or source changes.

Report word errors and the LIVE energy gate separately. Direct ASR may recognize
speech which the live pipeline would skip; reporting only ASR would hide that.
"""

import argparse
from array import array
import json
import math
from pathlib import Path
import random
import subprocess
import tempfile
import time
import wave
from live_transcript.transcription.audio_quality import measure
from tools.run_tts_e2e import errors


def transform(pcm, kind, seed=42):
    source = array("h")
    source.frombytes(pcm)
    rng = random.Random(seed)
    rms = math.sqrt(sum(v * v for v in source) / max(1, len(source)))
    if kind == "quiet_20db":
        out = [v * 0.1 for v in source]
    elif kind == "very_quiet_35db":
        out = [v * 0.0177828 for v in source]
    elif kind == "loud_clipped":
        out = [v * 8 for v in source]
    elif kind == "noise_10db":
        out = [v + rng.gauss(0, rms / (10**0.5)) for v in source]
    elif kind == "noise_0db":
        out = [v + rng.gauss(0, rms) for v in source]
    elif kind == "dropouts":
        out = [0 if (i // 1600) % 11 == 5 else v for i, v in enumerate(source)]
    else:
        out = source
    result = array("h", (max(-32768, min(32767, round(v))) for v in out))
    return result.tobytes()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--plan", required=True, help="JSON phrase plan with language, text and WAV file"
    )
    ap.add_argument("--output", required=True)
    ap.add_argument("--ids", default="1,4")
    ap.add_argument(
        "--conditions",
        default="clean,quiet_20db,very_quiet_35db,loud_clipped,noise_10db,noise_0db,dropouts",
    )
    a = ap.parse_args(argv)
    import live_transcript.transcription.daemon as captiond

    plan = Path(a.plan)
    ids = {int(i) for i in a.ids.split(",")}
    rows = []
    with tempfile.TemporaryDirectory(prefix="live-transcript-acoustics-") as tmp:
        tmp = Path(tmp)
        for phrase in json.loads(plan.read_text()):
            if phrase["id"] not in ids:
                continue
            normalized = tmp / "source.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(plan.parent / phrase["file"]),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(normalized),
                ],
                check=True,
                timeout=15,
            )
            with wave.open(str(normalized)) as wav:
                original = wav.readframes(wav.getnframes())
            for condition in a.conditions.split(","):
                pcm = transform(original, condition)
                candidate = tmp / "condition.wav"
                with wave.open(str(candidate), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(pcm)
                started = time.monotonic()
                result = captiond.stt_server(str(candidate), phrase["language"])
                actual = " ".join(text for _, text in result) if result is not None else ""
                # Feed the same approximately 0.75-second windows used live.
                live_skipped = all(
                    captiond._rms(pcm[i : i + 24000]) < captiond.SILENCE_RMS
                    for i in range(0, len(pcm), 24000)
                )
                row = dict(
                    id=phrase["id"],
                    language=phrase["language"],
                    condition=condition,
                    expected=phrase["text"],
                    actual=actual,
                    backend_unavailable=result is None,
                    live_gate_skips_entire_phrase=live_skipped,
                    quality=measure(pcm),
                    inference_seconds=round(time.monotonic() - started, 3),
                    **errors(phrase["text"], actual),
                )
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                Path(a.output).write_text(
                    json.dumps(
                        dict(
                            results=rows,
                            notes="Synthetic degradation, real ASR. Timing is inference only. No claim that noise reduction, diarization or overlap separation was applied.",
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
    return rows


if __name__ == "__main__":
    main()
