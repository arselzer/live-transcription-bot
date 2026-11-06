#!/usr/bin/env python3
"""Exercise restart/retry/push on an isolated daemon and fake STT endpoint."""

import http.server
import io
import json
import os
from pathlib import Path
import socket
import subprocess
from live_transcript.worker import command as worker_command
import tempfile
import threading
import time
import wave


class Backend(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        payload = json.dumps(
            {
                "segments": [
                    {
                        "start": 0.25,
                        "text": "The test transcript survives a daemon restart.",
                        "avg_logprob": -0.1,
                        "no_speech_prob": 0.01,
                    }
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix="caption-smoke-") as d:
        backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        port = free_port()
        env = dict(
            os.environ,
            CAPTION_BIND="127.0.0.1",
            CAPTION_PORT=str(port),
            CAPTION_DB=d + "/journal.sqlite3",
            CAPTION_CAP_FILE=d + "/captions.tsv",
            CAPTION_STT_URL=f"http://127.0.0.1:{backend.server_port}/inference",
            COMMIT_ONCE="1",
        )
        log = open(d + "/daemon.log", "wb")
        proc = None

        def launch():
            p = subprocess.Popen(
                worker_command("captions"),
                env=env,
                stdout=log,
                stderr=log,
            )
            for _ in range(100):
                try:
                    s = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                    s.sendall(b"GETGPU\n")
                    s.shutdown(socket.SHUT_WR)
                    s.recv(4096)
                    s.close()
                    return p
                except OSError:
                    if p.poll() is not None:
                        raise RuntimeError(Path(d + "/daemon.log").read_text())
                    time.sleep(0.05)
            raise TimeoutError("isolated daemon did not start")

        def subscribe():
            s = socket.create_connection(("127.0.0.1", port), timeout=12)
            s.sendall(b"SUBCAP isolated-smoke\n")
            return s, s.makefile("rb")

        body = io.BytesIO()
        with wave.open(body, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x01\x08" * 12000)
        data = body.getvalue()

        def send(seq):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                s.sendall(b"STREAM2 isolated-smoke\n")
                f = s.makefile("rb")
                assert f.readline() == b"OK\n"
                meta = dict(
                    stream="same-capture",
                    seq=seq,
                    audio_t0=seq * 0.75,
                    size=len(data),
                    timeline=[(0, "Test participant")],
                )
                s.sendall(json.dumps(meta).encode() + b"\n" + data)
                assert f.readline() == b"OK\n"
                s.shutdown(socket.SHUT_WR)
                f.close()

        try:
            proc = launch()
            s, reader = subscribe()
            json.loads(reader.readline())
            started = time.monotonic()
            for seq in range(4):
                send(seq)
            while True:
                first = json.loads(reader.readline())
                if any(row[2] for row in first["lines"]):
                    break
            elapsed = time.monotonic() - started
            reader.close()
            s.close()
            proc.terminate()
            proc.wait(timeout=5)
            proc = launch()
            s, reader = subscribe()
            restored = json.loads(reader.readline())
            assert restored["lines"] == first["lines"], (first, restored)
            reader.close()
            s.close()
            send(3)  # simulate the ACK being lost just before the restart
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                s.sendall(b"GETGPU isolated-smoke\n")
                s.shutdown(socket.SHUT_WR)
                stats = json.loads(s.makefile("rb").read())
            assert stats["duplicates"] == 1, stats
            print(
                json.dumps(
                    dict(
                        restart_restored=True,
                        lost_ack_deduplicated=True,
                        first_caption_seconds=round(elapsed, 3),
                        caption=first["lines"],
                    ),
                    indent=2,
                )
            )
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            backend.shutdown()
            backend.server_close()
            log.close()


if __name__ == "__main__":
    main()
