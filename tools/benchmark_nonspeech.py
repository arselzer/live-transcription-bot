#!/usr/bin/env python3
"""Check quiet-gate changes against nonspeech through the real speech backend."""

from array import array
import json
import math
from pathlib import Path
from live_transcript.core.config import get
import random
import tempfile
import wave
import live_transcript.transcription.daemon as captiond
from live_transcript.transcription.audio_quality import measure


def main():
    Path(get("paths", "logs")).mkdir(parents=True, exist_ok=True)
    Path(get("paths", "logs")).mkdir(parents=True, exist_ok=True)
    rows = []
    rng = random.Random(42)
    samples = 16000 * 5
    with tempfile.TemporaryDirectory() as tmp:
        for kind, level in [
            ("silence", 0),
            ("white_noise", 40),
            ("white_noise", 100),
            ("white_noise", 400),
            ("white_noise", 2000),
            ("tone", 100),
            ("tone", 1000),
            ("hum", 300),
        ]:
            values = []
            for i in range(samples):
                v = (
                    rng.gauss(0, level)
                    if kind == "white_noise"
                    else math.sin(2 * math.pi * (120 if kind == "hum" else 1000) * i / 16000)
                    * level
                    * math.sqrt(2)
                )
                values.append(max(-32768, min(32767, round(v))))
            pcm = array("h", values).tobytes()
            path = Path(tmp) / "input.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(pcm)
            for language in ("en", "de"):
                result = captiond.stt_server(str(path), language)
                row = dict(
                    kind=kind,
                    level=level,
                    language=language,
                    text=" ".join(t for _, t in result) if result is not None else None,
                    quality=measure(pcm),
                )
                rows.append(row)
                print(json.dumps(row), flush=True)
                (Path(get("paths", "logs")) / "nonspeech.json").write_text(
                    json.dumps(rows, indent=2)
                )


if __name__ == "__main__":
    main()
