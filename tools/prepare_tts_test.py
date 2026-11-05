#!/usr/bin/env python3
"""Build a known bilingual script using the bot's local speech synthesizer."""

import io
import json
from pathlib import Path
from live_transcript.core.config import get
import wave
from live_transcript.transcription.daemon import tts_wav

PHRASES = [
    ("en", "This is the automated English transcription test."),
    ("en", "Example will send the project report before Friday."),
    ("en", "Please keep the live captions readable and accurate."),
    ("de", "Dies ist der automatische deutsche Transkriptionstest."),
    ("de", "Example schickt den Projektbericht vor Freitag."),
    ("de", "Bitte stellt sicher, dass die Untertitel gut lesbar und genau sind."),
]


def main():
    root = Path(get("paths", "root")) / "benchmarks" / "tts-test"
    root.mkdir(parents=True, exist_ok=True)
    plan = []
    for number, (language, text) in enumerate(PHRASES, 1):
        wav = tts_wav(text, language)
        if not wav:
            raise RuntimeError("local synthesis failed")
        name = f"{number:02d}-{language}.wav"
        (root / name).write_bytes(wav)
        with wave.open(io.BytesIO(wav)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        plan.append(dict(id=number, language=language, text=text, file=name, duration=duration))
        print(name, round(duration, 2), "seconds", flush=True)
    (root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
