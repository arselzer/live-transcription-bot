#!/usr/bin/env python3
"""Independent archival capture. Never calls ASR, summaries, or caption transport.

Run through Archive.start() in a separate user systemd service. Short lossless
FLAC and Matroska segments remain usable after capture is interrupted. A restart
uses new filenames, never overwrites earlier evidence, and records the gap.
"""

from live_transcript.worker import command as worker_command

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from live_transcript.core.config import get
from live_transcript.core.files import atomic_json as atomic_json


def event(root, kind, **fields):
    with open(Path(root) / "events.jsonl", "a") as fh:
        fh.write(json.dumps(dict(utc=time.time(), kind=kind, **fields)) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def seal(root):
    """Hash finalized evidence so retrieval can verify the archive byte for byte."""
    hashes = {}
    for path in Path(root).iterdir():
        if (
            path.suffix not in (".flac", ".mkv", ".jpg", ".csv", ".jsonl", ".wav", ".txt")
            and path.name not in ("config.json", "observer-health.json", "ui-health.json")
        ) or not path.is_file():
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while data := fh.read(1024 * 1024):
                digest.update(data)
            os.fsync(fh.fileno())
        hashes[path.name] = dict(bytes=path.stat().st_size, sha256=digest.hexdigest())
    atomic_json(Path(root) / "integrity.json", hashes)


def verify_inventory(root, evidence_only=False):
    root = Path(root)
    inventory = json.loads((root / "integrity.json").read_text())
    checked = 0
    for name, info in inventory.items():
        if Path(name).name != name:
            raise ValueError("invalid archive filename")
        if evidence_only and (name.endswith(".mkv") or name.startswith("screen-")):
            continue
        digest = hashlib.sha256()
        with open(root / name, "rb") as fh:
            while data := fh.read(1024 * 1024):
                digest.update(data)
        if digest.hexdigest() != info["sha256"]:
            raise ValueError("Archive checksum mismatch: " + name)
        checked += 1
    if not checked:
        raise ValueError("Empty archive inventory")
    return checked


def commands(
    root,
    generation,
    display,
    audio_source=None,
    segment_seconds=10,
    audio_segment_seconds=5,
):
    root = Path(root)
    audio_source = audio_source or get("audio", "sink") + ".monitor"
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-n"]
    segment = [
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        "-segment_list_type",
        "csv",
    ]
    audio_segment = list(segment)
    audio_segment[audio_segment.index("-segment_time") + 1] = str(audio_segment_seconds)
    audio = (
        base
        + [
            "-f",
            "pulse",
            "-sample_rate",
            "48000",
            "-channels",
            "2",
            "-i",
            audio_source,
            "-c:a",
            "flac",
        ]
        + audio_segment
        + [
            "-segment_list",
            str(root / f"audio-{generation}.csv"),
            str(root / f"audio-{generation}-%06d.flac"),
        ]
    )
    screen = (
        base
        + [
            "-f",
            "x11grab",
            "-framerate",
            "5",
            "-video_size",
            "1280x800",
            "-i",
            display,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "25",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{segment_seconds})",
        ]
        + segment
        + [
            "-segment_list",
            str(root / f"screen-{generation}.csv"),
            str(root / f"screen-{generation}-%06d.mkv"),
        ]
    )
    snapshots = base + [
        "-f",
        "x11grab",
        "-framerate",
        "5",
        "-video_size",
        "1280x800",
        "-i",
        display,
        "-vf",
        "fps=1/10",
        "-q:v",
        "3",
        "-atomic_writing",
        "1",
        str(root / f"snapshot-{generation}-%06d.jpg"),
    ]
    return dict(audio=audio, screen=screen, snapshots=snapshots)


class Archive:
    """Joiner-side control. Capture is owned by systemd, not its caller."""

    def __init__(self, wav):
        self.root = Path(wav).with_suffix(".archive")
        self.unit = "live-transcript-archive-" + Path(wav).stem

    def start(
        self,
        started,
        max_seconds,
        provider="proton",
        cdp="http://127.0.0.1:9222",
        audio_source=None,
    ):
        if (self.root / "config.json").exists():
            raise FileExistsError(
                "Refusing to reuse an existing recording archive: " + str(self.root)
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        audio_source = audio_source or get("audio", "sink") + ".monitor"
        config = dict(
            archive_version=2,
            display=os.environ.get("DISPLAY", ":99"),
            started=started,
            deadline=started + max_seconds + 30,
            provider=provider,
            cdp=cdp,
            segment_seconds=10,
            audio_segment_seconds=5,
            audio_source=audio_source,
        )
        atomic_json(self.root / "config.json", config)
        cmd = [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            "--unit=" + self.unit,
            "--property=RuntimeMaxSec=" + str(max_seconds + 150),
            "--property=Restart=on-failure",
            "--property=RestartSec=2s",
            "--property=TimeoutStopSec=75s",
            "--property=KillSignal=SIGINT",
            "--property=KillMode=mixed",
        ]
        for key in (
            "DISPLAY",
            "XAUTHORITY",
            "PULSE_SERVER",
            "XDG_RUNTIME_DIR",
            "LIVE_TRANSCRIPT_CONFIG",
        ):
            if key in os.environ:
                cmd.append(f"--setenv={key}={os.environ[key]}")
        cmd += worker_command("capture", "--root", str(self.root))
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=10)
        for _ in range(150):
            # FLAC/segment output can buffer its first block, especially silence.
            # An opened output plus the supervisor heartbeat confirms startup;
            # progress is monitored independently and failure is never silent.
            if any(self.root.glob("audio-*.flac")) and self.check():
                return
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("Archival audio did not start; inspect " + str(self.root))

    def check(self):
        try:
            state = json.loads((self.root / "health.json").read_text())
            audio = state.get("audio", {})
            return (
                time.time() - state["utc"] < 8
                and audio.get("running", False)
                and audio.get("progress_age", 0) < 20
            )
        except (OSError, ValueError, KeyError):
            return False

    def stop(self):
        if (self.root / "finalized.json").exists():
            try:
                if json.loads((self.root / "health.json").read_text()).get("stopped"):
                    return
            except (OSError, ValueError):
                pass
        # A durable stop marker also prevents a service restart after completion.
        atomic_json(self.root / "stop.json", dict(utc=time.time()))
        subprocess.run(
            ["systemctl", "--user", "stop", self.unit], timeout=90, check=True, capture_output=True
        )


class Guard:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.slots = {}
        self.stopped = False
        self.last_sync = 0

    def launch(self, name):
        previous = self.slots.get(name, {})
        attempts = (
            previous.get("attempts", 0) + 1 if time.time() - previous.get("started", 0) < 60 else 0
        )
        generation = str(time.time_ns())
        options = commands(
            self.root,
            generation,
            os.environ.get("DISPLAY", ":99"),
            audio_source=self.config.get("audio_source", get("audio", "sink") + ".monitor"),
            audio_segment_seconds=self.config.get("audio_segment_seconds", 5),
            segment_seconds=self.config.get("segment_seconds", 10),
        )
        if name == "observer":
            argv = worker_command("observe", "--root", str(self.root))
        else:
            argv = options[name]
        log = open(self.root / f"{name}-{generation}.log", "ab")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        now = time.time()
        self.slots[name] = dict(
            proc=proc,
            generation=generation,
            started=now,
            progress_at=now,
            size=0,
            attempts=attempts,
        )
        event(self.root, "capture_started", component=name, generation=generation, pid=proc.pid)

    def tick(self):
        import shutil

        health = dict(
            utc=time.time(),
            started=self.config["started"],
            free_bytes=shutil.disk_usage(self.root).free,
        )
        for name in ("audio", "screen", "snapshots", "observer"):
            if (
                name == "observer"
                and self.config.get("provider") == "google"
                and not self.config.get("cdp")
            ):
                health[name] = dict(running=False, state="screen_evidence_only")
                continue
            slot = self.slots.get(name)
            if not slot:
                self.launch(name)
                slot = self.slots[name]
            proc = slot["proc"]
            if proc.poll() is not None:
                if "restart_at" not in slot:
                    slot["restart_at"] = time.time() + min(15, 2 ** min(slot.get("attempts", 0), 4))
                    event(self.root, "capture_interrupted", component=name, exit=proc.returncode)
                if time.time() < slot["restart_at"]:
                    health[name] = dict(running=False, restarting=True)
                    continue
                self.launch(name)
                slot = self.slots[name]
                proc = slot["proc"]
            pattern = {
                "audio": "audio-*.flac",
                "screen": "screen-*.mkv",
                "snapshots": "snapshot-*.jpg",
                "observer": "speakers.jsonl",
            }[name]
            files = list(self.root.glob(pattern))
            # Only the newest generation contributes progress after a restart.
            active = (
                [p for p in files if slot["generation"] in p.name] if name != "observer" else files
            )
            newest = max(active, key=lambda p: p.name) if active else None
            size = newest.stat().st_size if newest else 0
            signature = (newest.name if newest else "", size)
            if signature != slot.get("last_signature"):
                slot.update(size=size, progress_at=time.time(), last_signature=signature)
            stale = time.time() - slot["progress_at"]
            limit = 20 if name == "audio" else 40
            if stale > limit:
                event(self.root, "capture_stalled", component=name, seconds=round(stale, 2))
                self.terminate(proc)
                self.launch(name)
                slot = self.slots[name]
            health[name] = dict(
                running=slot["proc"].poll() is None, progress_age=round(stale, 2), bytes=size
            )
        atomic_json(self.root / "health.json", health)
        # Force recently closed segments and evidence to stable storage. This
        # does not make a failing disk or interrupted incoming media lossless.
        if time.time() - self.last_sync >= 10:
            for path in self.root.iterdir():
                if path.is_file() and path.suffix in (".flac", ".mkv", ".jpg", ".csv"):
                    if time.time() - path.stat().st_mtime < 25:
                        with open(path, "rb") as fh:
                            os.fsync(fh.fileno())
            self.last_sync = time.time()

    @staticmethod
    def terminate(proc, already_signalled=False):
        if proc.poll() is None:
            try:
                if not already_signalled:
                    os.killpg(proc.pid, signal.SIGINT)
                proc.wait(timeout=8)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()

    def run(self):
        # Only a clean, completely sealed stop is final. A service exception
        # must not convince the host mirror that recording is finished.
        (self.root / "finalized.json").unlink(missing_ok=True)
        event(self.root, "guard_started", pid=os.getpid())
        clean = False
        try:
            while (
                not self.stopped
                and time.time() < self.config["deadline"]
                and not (self.root / "stop.json").exists()
            ):
                self.tick()
                time.sleep(0.5)
            clean = True
        finally:
            # Signal every child first; one slow X11 capture must not delay the
            # audio recorder's flush or the speaker collector's shutdown.
            for slot in self.slots.values():
                try:
                    os.killpg(slot["proc"].pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            for slot in reversed(list(self.slots.values())):
                self.terminate(slot["proc"], already_signalled=True)
            event(self.root, "guard_stopped", deliberate=clean)
            if clean:
                atomic_json(
                    self.root / "health.json", dict(utc=time.time(), state="sealing", stopped=False)
                )
                seal(self.root)
                digest = hashlib.sha256((self.root / "integrity.json").read_bytes()).hexdigest()
                atomic_json(
                    self.root / "finalized.json", dict(utc=time.time(), inventory_sha256=digest)
                )
                atomic_json(
                    self.root / "health.json",
                    dict(utc=time.time(), state="finalized", stopped=True),
                )
            else:
                atomic_json(
                    self.root / "health.json",
                    dict(utc=time.time(), state="restarting", stopped=False),
                )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True)
    a = ap.parse_args()
    os.umask(0o077)
    root = Path(a.root)
    with open(root / "guard.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        guard = Guard(root, json.loads((root / "config.json").read_text()))
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(guard, "stopped", True))
        guard.run()


if __name__ == "__main__":
    main()
