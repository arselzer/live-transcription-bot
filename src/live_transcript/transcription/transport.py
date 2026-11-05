#!/usr/bin/env python3
"""Live Transcript VM transport: durable retry IDs, persistent audio and captions.
Capture generations use separate directories; unsent audio survives restarts.
"""

import fcntl
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import wave

from live_transcript.core.config import get

HOST = os.environ.get("CAPTION_HOST", get("caption", "host"))
PORT = int(os.environ.get("CAPTION_PORT", str(get("caption", "port"))))
SESSION = sys.argv[1] if len(sys.argv) > 1 else "m"
NO_VIDEO = "--no-video" in sys.argv
HOME = Path(os.environ.get("SCRIBE_DIR", get("vm", "home")))
CAP_TSV = HOME / "captions.tsv"
SCOPED_SPK_LOG = HOME / "rec" / (SESSION + ".speakers.jsonl")
SPK_LOG = HOME / "rec/speakers.jsonl"
HB_FILE = "/tmp/caprender.hb"
CHILDREN = []
SOURCES = []
SOURCE_LOCK = threading.RLock()
STOP = threading.Event()
SESSION_START = time.time()
DEADLINE = time.monotonic() + 8 * 3600
CHUNK_SECONDS = 0.75
ROOT = Path("/tmp") / ("capchunks_" + SESSION)


def atomic_write(path, content, durable=False):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(content if isinstance(content, bytes) else content.encode())
            if durable:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        if durable:
            fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def txt(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def tcp(msg, timeout=5):
    with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
        sock.sendall(msg + b"\n")
        sock.shutdown(socket.SHUT_WR)
        return sock.makefile("rb").read()


def read_timeline():
    ev = []
    try:
        source = SCOPED_SPK_LOG if SCOPED_SPK_LOG.exists() else SPK_LOG
        for line in source.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
                name = next((w["name"] for w in row["who"] if w.get("role") == "active"), "")
                if name and ("recording" in name.lower() or "live transcript" in name.lower()):
                    name = ""
                if not ev or ev[-1][1] != name:
                    ev.append((float(row["t"]), name))
            except (ValueError, KeyError, TypeError):
                pass
    except OSError:
        pass
    try:
        voice = HOME / "rec" / (SESSION + ".voice.jsonl")
        if not voice.exists():
            voice = HOME / "rec/voice.jsonl"
        for line in voice.read_text().splitlines():
            event = json.loads(line)
            start, end = float(event["t"]), float(event["t"]) + float(event["duration"])
            ev = [(t, name) for t, name in ev if not start <= t < end]
            ev.extend([(start, event.get("speaker_label", event["name"])), (end, "")])
        ev.sort()
    except (OSError, ValueError, KeyError):
        pass
    return ev


def speaker_tracker():
    while not STOP.wait(0.5):
        try:
            ev = read_timeline()
            elapsed = time.time() - SESSION_START
            now = ev[-1][1] if ev and elapsed - ev[-1][0] < 5 else ""
            atomic_write(HOME / "nowspeaking.txt", now)
            totals = {}
            for i, (t, name) in enumerate(ev):
                end = ev[i + 1][0] if i + 1 < len(ev) else min(elapsed, t + 5)
                if name:
                    totals[name] = totals.get(name, 0) + max(0, end - t)
            atomic_write(HOME / "talktime.json", json.dumps(totals))
        except Exception as exc:
            print("[speaker]", exc, flush=True)


def host_poller():
    while not STOP.is_set():
        try:
            raw = tcp(f"GETGPU {SESSION}".encode())
            json.loads(raw)
            atomic_write(HOME / "gpu.json", raw.strip())
            todo = tcp(f"GETTODO {SESSION}".encode()).decode(errors="replace").strip()
            atomic_write(HOME / "todo.txt", todo)
        except Exception as exc:
            print("[health]", exc, flush=True)
        STOP.wait(3)


def caption_poller():
    """One subscription; reconnect supplies a complete snapshot, never a delta."""
    seen_backend_events = set()
    while not STOP.is_set():
        try:
            with socket.create_connection((HOST, PORT), timeout=8) as sock:
                sock.sendall(f"SUBCAP {SESSION}\n".encode())
                sock.settimeout(12)
                reader = sock.makefile("rb")
                while not STOP.is_set():
                    raw = reader.readline(1024 * 1024)
                    if not raw:
                        raise ConnectionError("caption subscription closed")
                    if STOP.is_set():
                        break
                    data = json.loads(raw)
                    lines = data.get("lines", [])[-12:]
                    atomic_write(CAP_TSV, "".join(f"{sp}\t{text}\n" for _, sp, text in lines))
                    data["received_at"] = time.time()
                    atomic_write(HOME / "captions.json", json.dumps(data, ensure_ascii=False))
                    archive = HOME / "rec" / (SESSION + ".archive")
                    if archive.is_dir() and not (archive / "finalized.json").exists():
                        for item in data.get("asr_events", []):
                            key = (item.get("utc"), item.get("engine"))
                            if key not in seen_backend_events:
                                with open(archive / "asr.jsonl", "a") as evidence:
                                    evidence.write(json.dumps(item) + "\n")
                                    evidence.flush()
                                    os.fsync(evidence.fileno())
                                seen_backend_events.add(key)
        except Exception as exc:
            print("[captions]", exc, flush=True)
            STOP.wait(1)


def _pdeath():
    import ctypes

    ctypes.CDLL(None).prctl(1, signal.SIGKILL)


def spawn(args, log):
    with open(log, "ab") as fh:
        proc = subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT, preexec_fn=_pdeath)
    CHILDREN.append(proc)
    return proc


def save_source(source):
    atomic_write(Path(source["dir"]) / "source.json", json.dumps(source), durable=True)


def start_capture():
    stream = uuid.uuid4().hex
    directory = ROOT / f"{time.time_ns()}_{stream}"
    directory.mkdir(parents=True)
    source = {
        "dir": str(directory),
        "stream": stream,
        "next_seq": 0,
        "sent_samples": 0,
        "audio_t0": max(0, time.time() - SESSION_START),
        "closed": False,
    }
    save_source(source)
    with SOURCE_LOCK:
        SOURCES.append(source)
    proc = spawn(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "warning",
            "-f",
            "pulse",
            "-i",
            os.environ.get("CAPTION_AUDIO_SOURCE", "scribe.monitor"),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "segment",
            "-segment_time",
            str(CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
            str(directory / "capchunk_%05d.wav"),
        ],
        "/tmp/capcapture.log",
    )
    return proc, source


def start_renderer():
    return spawn([sys.executable, str(HOME / "caption_render.py")], "/tmp/caprender.log")


def supervisor():
    capture, source = start_capture()
    renderer = None if NO_VIDEO else start_renderer()
    while not STOP.wait(1):
        try:
            if capture.poll() is not None:
                with SOURCE_LOCK:
                    source["closed"] = True
                    save_source(source)
                print("[capture] restarting; previous generation retained for upload", flush=True)
                if STOP.wait(2):
                    break
                capture, source = start_capture()
            if renderer is not None and renderer.poll() is not None:
                renderer = start_renderer()
        except Exception as exc:
            print("[supervisor]", exc, flush=True)
    if capture.poll() is None:
        capture.terminate()
        try:
            capture.wait(timeout=5)
        except subprocess.TimeoutExpired:
            capture.kill()
            capture.wait()
    with SOURCE_LOCK:
        source["closed"] = True
        save_source(source)


class Uploader:
    def __init__(self):
        self.sock = self.reader = None

    def close(self):
        if self.reader:
            self.reader.close()
        if self.sock:
            self.sock.close()
        self.reader = self.sock = None

    def send(self, meta, wav):
        if self.sock is None:
            self.sock = socket.create_connection((HOST, PORT), timeout=12)
            self.sock.sendall(f"STREAM2 {SESSION}\n".encode())
            self.reader = self.sock.makefile("rb")
            if self.reader.readline().strip() != b"OK":
                raise ConnectionError("audio handshake rejected")
        header = dict(meta, size=len(wav))
        self.sock.sendall(json.dumps(header, ensure_ascii=False).encode() + b"\n" + wav)
        if self.reader.readline().strip() != b"OK":
            raise ConnectionError("audio not committed by daemon")


def upload_ready(uploader):
    sent = False
    with SOURCE_LOCK:
        sources = list(SOURCES)
    for source in sources:
        directory = Path(source["dir"])
        while True:
            seq = source["next_seq"]
            current = directory / f"capchunk_{seq:05d}.wav"
            following = directory / f"capchunk_{seq + 1:05d}.wav"
            if not current.exists() or not (following.exists() or source["closed"]):
                break
            data = current.read_bytes()
            with wave.open(io.BytesIO(data)) as wav:
                pcm = wav.readframes(wav.getnframes())
            if not pcm:
                raise ValueError(f"empty capture retained for inspection: {current}")
            # Metadata is saved BEFORE send. A lost ACK/restart reuses the same ID.
            sidecar = current.with_suffix(".json")
            if sidecar.exists():
                meta = json.loads(sidecar.read_text())
            else:
                meta = {
                    "stream": source["stream"],
                    "seq": seq,
                    "audio_t0": source["audio_t0"] + source["sent_samples"] / 16000,
                    "captured_at": SESSION_START
                    + source["audio_t0"]
                    + (source["sent_samples"] + len(pcm) // 2) / 16000,
                    "timeline": [list(event) for event in read_timeline()[-200:]],
                }
                atomic_write(sidecar, json.dumps(meta, ensure_ascii=False), durable=True)
            uploader.send(meta, data)
            with SOURCE_LOCK:
                source["next_seq"] += 1
                source["sent_samples"] += len(pcm) // 2
                save_source(source)
            current.unlink()
            sidecar.unlink(missing_ok=True)
            sent = True
    return sent


def main():
    global SESSION_START
    HOME.mkdir(parents=True, exist_ok=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = open(os.environ.get("CAPTION_LOCK", "/tmp/caption_stream.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another caption streamer is already running")
    Path(os.environ.get("CAPTION_PID_FILE", "/tmp/caption_stream.pid")).write_text(str(os.getpid()))
    SESSION_START = float(txt("/tmp/cap_session_start") or time.time())
    for path in sorted(ROOT.glob("*/source.json")):
        source = json.loads(path.read_text())
        source["closed"] = True
        SOURCES.append(source)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: STOP.set())
    for task in (caption_poller, host_poller, speaker_tracker):
        threading.Thread(target=task, daemon=True).start()
    owner = threading.Thread(target=supervisor)
    owner.start()
    uploader = Uploader()
    print(f"[stream] v2 session={SESSION} video={not NO_VIDEO}", flush=True)
    try:
        while not STOP.is_set() and time.monotonic() < DEADLINE:
            try:
                if not upload_ready(uploader):
                    STOP.wait(0.1)
            except Exception as exc:
                print("[upload] retained for retry:", exc, flush=True)
                uploader.close()
                STOP.wait(1)
    finally:
        STOP.set()
        owner.join(timeout=8)
        # Flush the final closed packet when the backend is reachable.
        try:
            upload_ready(uploader)
        except Exception as exc:
            print("[upload] final packet retained:", exc, flush=True)
        uploader.close()
        for proc in CHILDREN:
            if proc.poll() is None:
                proc.terminate()
        for proc in CHILDREN:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    main()
