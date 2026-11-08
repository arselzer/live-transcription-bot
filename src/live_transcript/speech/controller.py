"""Explicit speech requests for the meeting bot; silent unless a request arrives."""

import io
import json
import os
from pathlib import Path
from live_transcript.core.config import get
import socket
import time
import wave
import re
import uuid
from live_transcript.speech.browser import play_wav


class VoiceController:
    def __init__(self, page, session, name, directory=None, t0=None, provider="proton"):
        self.page, self.session, self.name = page, session, name
        from live_transcript.capture.audio import server_identity

        self.audio_identity = server_identity()
        self.audio_recovery_pending = False
        self.audio_probe_at = 0
        self.provider = provider
        self.root = Path(directory or get("vm", "home"))
        self.inbox = self.root / "voice_requests"
        self.inbox.mkdir(exist_ok=True)
        self.done = self.inbox / "done"
        self.done.mkdir(exist_ok=True)
        from live_transcript.capture.guard import atomic_json

        for interrupted in self.inbox.glob("*.inflight"):
            ident = interrupted.with_suffix(".json").name
            atomic_json(
                self.done / ident,
                dict(
                    ok=False,
                    error="speech interrupted; it may have played, explicit retry required",
                ),
            )
            interrupted.replace(self.done / (ident + ".interrupted"))
        self.t0 = float(Path("/tmp/cap_session_start").read_text()) if t0 is None else t0
        ready = self.root / "voice_ready.json"
        tmp = ready.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(session=session, pid=os.getpid())))
        tmp.replace(ready)

    def mic_button(self, unmute):
        if self.provider == "google":
            from live_transcript.providers.google_meet import button

            return button(self.page, "Turn on microphone" if unmute else "Turn off microphone")
        if self.provider == "proton":
            label = "Unmute microphone" if unmute else "Mute microphone"
            return self.page.get_by_role("button", name=label, exact=True).first
        pattern = re.compile(r"^" + ("unmute" if unmute else "mute") + r"(?:\s|$)", re.I)
        for frame in self.page.frames:
            control = frame.get_by_role("button", name=pattern).first
            if control.is_visible():
                return control
        return None

    def enqueue(self, text, language):
        from live_transcript.capture.guard import atomic_json

        ident = str(time.time_ns()) + "-" + uuid.uuid4().hex
        atomic_json(
            self.inbox / (ident + ".json"),
            dict(session=self.session, text=text, language=language, queued_at=time.time()),
        )
        return ident

    def poll(self):
        from live_transcript.runtime.console import operator_control_active

        if operator_control_active():
            from live_transcript.capture.audio import server_identity

            self.audio_identity = server_identity()
            self.audio_recovery_pending = False
            return
        if time.monotonic() - self.audio_probe_at >= 3:
            from live_transcript.capture.audio import server_identity
            import subprocess

            identity = server_identity()
            if self.audio_identity and identity != self.audio_identity:
                self.audio_recovery_pending = True
            self.audio_identity = identity
            self.audio_probe_at = time.monotonic()
            if identity and self.audio_recovery_pending:
                try:
                    ready = (
                        subprocess.run(["pactl", "info"], capture_output=True, timeout=1).returncode
                        == 0
                    )
                except (OSError, subprocess.SubprocessError):
                    ready = False
                if ready:
                    from live_transcript.speech.browser import recover_output

                    count = recover_output(self.page)
                    archive = self.root / "rec" / (self.session + ".archive")
                    if archive.is_dir():
                        from live_transcript.capture.guard import event

                        event(archive, "browser_audio_output_reset", outputs=count)
                    self.audio_recovery_pending = False
        requests = sorted(self.inbox.glob("*.json"))
        if not requests:
            return
        path = requests[0]
        ident = path.name
        claim = path.with_suffix(".inflight")
        if (self.done / ident).exists():
            path.unlink()
            return  # a repeated request ID cannot speak twice
        result = {}
        try:
            request = json.loads(path.read_text())
            if request.get("session") != self.session:
                raise ValueError("speech request belongs to a different meeting")
            if time.time() - request.get("queued_at", time.time()) > 120:
                raise ValueError("speech request expired")
            language = request.get("language", "en")
            text = str(request.get("text", "")).strip()
            if (
                language not in ("de", "en")
                or not text
                or len(text) > 500
                or any(ord(c) < 32 for c in text)
            ):
                raise ValueError("speech requires de/en and 1–500 characters")
            with socket.create_connection(
                (get("caption", "host"), get("caption", "port")), timeout=45
            ) as sock:
                sock.sendall(f"SAY {language} {text}\n".encode())
                sock.shutdown(socket.SHUT_WR)
                wav = sock.makefile("rb").read(8 * 1024 * 1024 + 1)
                if len(wav) > 8 * 1024 * 1024:
                    raise ValueError("speech audio response too large")
            with wave.open(io.BytesIO(wav)) as audio:
                duration = audio.getnframes() / audio.getframerate()
            if not 0 < duration <= 60:
                raise ValueError("speech audio duration exceeds limit")
            path.replace(claim)  # durable at-most-once playback boundary
            fd = os.open(self.inbox, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            muted = self.mic_button(True)
            restore_mute = muted is not None and muted.is_visible()
            if restore_mute:
                muted.click(timeout=1500, no_wait_after=True)
            try:
                label = (
                    self.name
                    if re.search(r"live transcript|recording", self.name, re.I)
                    else f"Live Transcript ({self.name})"
                )
                event = dict(
                    t=max(0, time.time() - self.t0),
                    duration=duration,
                    name=self.name,
                    speaker_label=label,
                    source="bot-tts",
                    text=text,
                )
                paths = [
                    self.root / "rec/voice.jsonl",
                    self.root / "rec" / (self.session + ".voice.jsonl"),
                ]
                archive = self.root / "rec" / (self.session + ".archive")
                if archive.exists():
                    paths.append(archive / "voice.jsonl")
                for event_path in paths:
                    with open(event_path, "a") as fh:
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                # Monitor to the recording sink as well as transmitting. The virtual
                # mic receives only the explicit speech buffer, so there is no echo loop.
                result = play_wav(self.page, wav, monitor=True)
                result["ok"] = True
            finally:
                if restore_mute:
                    control = self.mic_button(False)
                    if control is None:
                        raise RuntimeError("cannot restore microphone mute")
                    control.click(timeout=1500, no_wait_after=True)
        except Exception as exc:
            result = dict(ok=False, error=str(exc))
        from live_transcript.capture.guard import atomic_json

        atomic_json(self.done / ident, result)
        path.unlink(missing_ok=True)
        claim.unlink(missing_ok=True)
