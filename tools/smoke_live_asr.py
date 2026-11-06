#!/usr/bin/env python3
"""Isolated real-time STREAM2 replay with real ASR: quiet input or backend outage.

No meeting, sound playback, production session reset, or physical GPU reset.
The outage is an HTTP proxy returning 503; original PCM and final state remain
in the diagnostic directory for auditing exactly what the replay submitted.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
from live_transcript.worker import command as worker_command
import threading
import time
import urllib.error
import urllib.request
import wave

from tools.benchmark_audio_conditions import transform
from tools.run_tts_e2e import errors
from live_transcript.core.config import get


def wav_bytes(pcm):
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return data.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("quiet", "fallback"), required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    plan = Path(a.plan)
    output = Path(a.output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    selected = [
        p
        for p in json.loads(plan.read_text())
        if p["id"] in ((1, 4) if a.mode == "quiet" else (1, 2, 3, 4, 5, 6))
    ]
    selected.sort(key=lambda p: {1: 0, 4: 1, 2: 2, 5: 3, 3: 4, 6: 5}[p["id"]])
    pcm = bytearray()
    timeline = []
    references = []
    for index, phrase in enumerate(selected):
        offset = index * 10 * 32000
        pcm.extend(b"\0" * max(0, offset - len(pcm)))
        path = output / "normalized.wav"
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
            timeout=15,
        )
        with wave.open(str(path)) as wav:
            source = wav.readframes(wav.getnframes())
        pcm.extend(transform(source, "very_quiet_35db") if a.mode == "quiet" else source)
        timeline.append((index * 10, "English" if phrase["language"] == "en" else "Deutsch"))
        references.append(phrase["text"])
    pcm.extend(b"\0" * 32000 * 8)
    (output / "submitted.wav").write_bytes(wav_bytes(pcm))
    outage = threading.Event()
    outage.set()

    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if outage.is_set():
                self.send_error(503, "injected speech backend failure")
                return
            request = urllib.request.Request(
                get("caption", "stt_url"), data, {"Content-Type": self.headers["Content-Type"]}
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    body = response.read()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(502)

        def log_message(self, *args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(
        os.environ,
        CAPTION_BIND="127.0.0.1",
        CAPTION_PORT=str(port),
        CAPTION_DB=str(output / "captions.sqlite3"),
        CAPTION_CAP_FILE=str(output / "captions.tsv"),
        COMMIT_ONCE="1",
    )
    if a.mode == "fallback":
        env["CAPTION_STT_URL"] = f"http://127.0.0.1:{proxy.server_port}/inference"
    session = "isolated-" + a.mode
    with (output / "daemon.log").open("wb") as log:
        proc = subprocess.Popen(
            worker_command("captions"),
            env=env,
            stdout=log,
            stderr=log,
        )
    observations = []

    def snapshot(command):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(f"{command} {session}\n".encode())
            return json.loads(sock.makefile("rb").readline(1024 * 1024))

    try:
        for _ in range(100):
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("isolated daemon did not start")
        sock.sendall(f"STREAM2 {session}\n".encode())
        reader = sock.makefile("rb")
        assert reader.readline() == b"OK\n"
        started = time.monotonic()
        with (output / "health.jsonl").open("w") as health:
            for seq, offset in enumerate(range(0, len(pcm), 24000)):
                if a.mode == "fallback" and time.monotonic() - started >= 25:
                    outage.clear()
                body = wav_bytes(pcm[offset : offset + 24000])
                meta = dict(
                    stream="known-replay",
                    seq=seq,
                    audio_t0=offset / 32000,
                    captured_at=time.time(),
                    size=len(body),
                    timeline=timeline,
                )
                sock.sendall(json.dumps(meta).encode() + b"\n" + body)
                assert reader.readline() == b"OK\n"
                if seq % 2 == 0:
                    value = dict(elapsed=time.monotonic() - started, caption=snapshot("GETGPU"))
                    observations.append(value)
                    health.write(json.dumps(value) + "\n")
                    health.flush()
                time.sleep(max(0, started + (seq + 1) * 0.75 - time.monotonic()))
        reader.close()
        sock.close()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = snapshot("GETGPU")
            if (
                state.get("queued_audio_s", 1) == 0
                and time.monotonic() - started > len(pcm) / 32000 + 6
            ):
                break
            time.sleep(1)
        time.sleep(4)
        final = snapshot("SUBCAP")
        actual = " ".join(row[2] for row in final["lines"])
        expected = " ".join(references)
        report = dict(
            mode=a.mode,
            expected=expected,
            actual=actual,
            final=final,
            engines=sorted(
                {r["caption"].get("asr", {}).get("engine", "unknown") for r in observations}
            ),
            max_queue_seconds=max(r["caption"].get("queued_audio_s", 0) for r in observations),
            notes="Isolated real-time wire replay. Backend failure injected through HTTP 503 proxy; no physical GPU reset or live meeting.",
            **errors(expected, actual),
        )
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if a.mode == "quiet":
            assert report["edits"] == 0, "quiet speech lost or misrecognized"
        else:
            assert any(engine.startswith("cpu") for engine in report["engines"])
            assert final.get("asr", {}).get("engine") == "gpu-large", "GPU recovery not observed"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proxy.shutdown()
        proxy.server_close()


if __name__ == "__main__":
    main()
