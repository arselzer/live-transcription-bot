#!/usr/bin/env python3
"""Run unmodified upstream LocalAgreement processor with our whisper.cpp server.

The custom ASR bridge requests word-sized timestamped segments. This compares
streaming policy on the same AMD-compatible engine, not a SimulStreaming port.
Install numpy/librosa/soundfile in a separate research environment.
"""

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import wave
import numpy as np
from live_transcript.core.config import get
from tools.run_tts_e2e import errors


class ServerBridge:
    sep = " "

    def __init__(self, language):
        self.language = language
        self.calls = []

    def transcribe(self, audio, init_prompt=""):
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        boundary = "lt-research-boundary"
        parts = []
        fields = dict(
            language=self.language,
            response_format="verbose_json",
            translate="false",
            prompt=init_prompt,
            max_len="1",
            split_on_word="true",
            detect_language="false",
        )
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
            + data.getvalue()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        started = time.monotonic()
        request = urllib.request.Request(
            get("caption", "stt_url"),
            b"".join(parts),
            {"Content-Type": "multipart/form-data; boundary=" + boundary},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
        self.calls.append(
            dict(audio_seconds=len(audio) / 16000, inference_seconds=time.monotonic() - started)
        )
        return [
            s
            for s in result.get("segments", [])
            if s.get("no_speech_prob", 0) <= 0.9 and s.get("text", "").strip()
        ]

    def ts_words(self, result):
        return [(s["start"], s["end"], s["text"].strip()) for s in result]

    def segments_end_ts(self, result):
        return [s["end"] for s in result]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    source = Path(a.upstream)
    spec = importlib.util.spec_from_file_location("upstream_whisper_online", source)
    upstream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upstream)
    plan = Path(a.plan)
    phrases = json.loads(plan.read_text())
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for language in ("en", "de"):
            pcm = []
            references = []
            for phrase in (p for p in phrases if p["language"] == language):
                path = Path(tmp) / "input.wav"
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
                        str(path),
                    ],
                    check=True,
                )
                with wave.open(str(path)) as wav:
                    pcm.append(
                        np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(
                            "float32"
                        )
                        / 32768
                    )
                pcm.append(np.zeros(32000, dtype="float32"))
                references.append(phrase["text"])
            pcm = np.concatenate(pcm)
            bridge = ServerBridge(language)
            processor = upstream.OnlineASRProcessor(bridge, buffer_trimming=("segment", 15))
            outputs = []
            started = time.monotonic()
            events = []
            for offset in range(0, len(pcm), 24000):
                processor.insert_audio_chunk(pcm[offset : offset + 24000])
                result = processor.process_iter()
                if result[2]:
                    outputs.append(result[2])
                    events.append(
                        dict(
                            at_input_seconds=min((offset + 24000) / 16000, len(pcm) / 16000),
                            text=result[2],
                        )
                    )
            result = processor.finish()
            if result[2]:
                outputs.append(result[2])
            expected = " ".join(references)
            actual = " ".join(outputs)
            row = dict(
                language=language,
                expected=expected,
                actual=actual,
                events=events,
                calls=bridge.calls,
                wall_compute_seconds=time.monotonic() - started,
                **errors(expected, actual),
            )
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            Path(a.output).write_text(
                json.dumps(
                    dict(
                        upstream_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                        upstream_url="https://github.com/ufal/whisper_streaming/blob/main/whisper_online.py",
                        results=results,
                        caveats="Unmodified upstream LocalAgreement processor with custom whisper.cpp HTTP bridge; word-sized server segments approximate word timestamps. Compute-only replay, no claim of real-room latency or SimulStreaming performance.",
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
