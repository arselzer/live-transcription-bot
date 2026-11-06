#!/usr/bin/env python3
"""Idempotent PulseAudio/ALSA topology for the Live Transcript bot.

- Configured null sink: Zoom's playback lands here; its monitor is recorded/chunked.
- Browser mic: snd-dummy (pure software ALSA card) via module-alsa-source.
  It ALWAYS yields buffers (silence), which is exactly what is needed:
    * Chromium refuses PulseAudio *monitor* sources as mics (getUserMedia
      NotFoundError) — only real ALSA capture devices are openable;
    * a silent-but-FLOWING capture keeps WebRTC's audio thread alive.
      snd-aloop in some tested guest configurations opens without yielding data — that starves the paired output
      stream and silently kills the entire meeting downlink (decoded remote
      audio stays zero). Do NOT reintroduce aloop.
- TTS uses the browser WebAudio virtual microphone, independently of these sinks.
NOTE: build the topology BEFORE Chrome starts (Chrome caches devices at
launch; every join launches a fresh Chrome, so ordering is automatic).
"""

import re
import subprocess
import time
import os
import signal
from pathlib import Path
from live_transcript.core.config import get
from live_transcript.runtime.session import assert_idle
from live_transcript.worker import is_role_process


def main():
    _owner = os.environ.get("LT_PREP_OWNER", "")
    _parent_args = Path(f"/proc/{os.getppid()}/cmdline").read_bytes().split(b"\0")
    _leased_preparation = _owner == str(os.getppid()) and is_role_process(
        _parent_args, {"prepare", "session"}
    )
    if not _leased_preparation:
        assert_idle()
    _archives = subprocess.run(
        [
            "systemctl",
            "--user",
            "list-units",
            "--state=active,activating,deactivating",
            "--no-legend",
            "live-transcript-archive-*",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    if _archives.stdout.strip():
        raise RuntimeError("Archival capture is active; refusing to reset its audio source")

    def sh(*cmd):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout

    def ensure_pulse():
        """One socket-activated server; never race an ad-hoc daemon against systemd."""
        dropin = Path.home() / ".config/systemd/user/pulseaudio.service.d/live-transcript.conf"
        dropin.parent.mkdir(parents=True, exist_ok=True)
        config = (
            "[Unit]\nStartLimitIntervalSec=0\n[Service]\nExecStart=\n"
            "ExecStart=/usr/bin/pulseaudio --daemonize=no --log-target=journal --exit-idle-time=-1 --disallow-exit\n"
            "Restart=always\nRestartSec=2\n"
        )
        changed = not dropin.exists() or dropin.read_text() != config
        if changed:
            dropin.write_text(config)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=10)
        client = Path.home() / ".config/pulse/client.conf"
        client.parent.mkdir(parents=True, exist_ok=True)
        previous = client.read_text() if client.exists() else ""
        content = re.sub(r"^\s*autospawn\s*=.*$", "autospawn = no", previous, flags=re.M)
        if not re.search(r"^\s*autospawn\s*=", content, re.M):
            content += "\nautospawn = no\n"
        if content != previous:
            client.write_text(content)
        active = (
            subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", "pulseaudio.service"], timeout=5
            ).returncode
            == 0
        )
        ready = subprocess.run(["pactl", "info"], capture_output=True, timeout=5).returncode == 0
        if changed or not (active and ready):
            subprocess.run(
                ["systemctl", "--user", "stop", "pulseaudio.service", "pulseaudio.socket"],
                capture_output=True,
                timeout=15,
            )
            # Retire only this account's legacy ad-hoc server while the idle VM
            # lease is held. A stale socket must not trigger a competing autospawn.
            for value in sh("pgrep", "-u", str(os.getuid()), "-x", "pulseaudio").split():
                try:
                    os.kill(int(value), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 5
            while (
                sh("pgrep", "-u", str(os.getuid()), "-x", "pulseaudio").strip()
                and time.monotonic() < deadline
            ):
                time.sleep(0.1)
            subprocess.run(
                ["systemctl", "--user", "reset-failed", "pulseaudio.service", "pulseaudio.socket"],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["systemctl", "--user", "enable", "pulseaudio.service", "pulseaudio.socket"],
                capture_output=True,
                check=True,
                timeout=10,
            )
            subprocess.run(
                ["systemctl", "--user", "start", "pulseaudio.socket", "pulseaudio.service"],
                capture_output=True,
                check=True,
                timeout=15,
            )
        for _ in range(40):
            if subprocess.run(["pactl", "info"], capture_output=True, timeout=3).returncode == 0:
                return
            time.sleep(0.25)
        raise RuntimeError("Managed PulseAudio did not become ready")

    def _drop(module_id):
        """Detach one PulseAudio module by id."""
        subprocess.run(["pactl", "un" + "load-module", str(module_id)], capture_output=True)

    def _load(args):
        return subprocess.run(
            ["pactl", "load-module"] + args, capture_output=True, text=True
        ).stdout.strip()

    def _drop_modules(pred):
        for row in sh("pactl", "list", "short", "modules").splitlines():
            f = row.split("\t")
            if len(f) > 1 and pred(f[1]):
                _drop(f[0])

    # 1) kernel dummy card for the browser mic; remove the hostile aloop if present
    if "snd_dummy" not in sh("lsmod"):
        subprocess.run(["sudo", "-n", "modprobe", "snd-dummy"], capture_output=True)
    if "snd_aloop" in sh("lsmod"):
        _drop_modules(lambda n: n.startswith("module-alsa-"))
        subprocess.run(["sudo", "-n", "rmmod", "snd-aloop"], capture_output=True)

    # 2) after kernel setup, let exactly one managed server own the native socket.
    ensure_pulse()

    # 3) silence PulseAudio's auto-profile on the dummy card, else it holds
    #    the PCM exclusively and our manual module hits "Device busy"
    for row in sh("pactl", "list", "cards", "short").splitlines():
        card = row.split("\t")[1] if "\t" in row else ""
        if "dummy" in card.lower():
            subprocess.run(["pactl", "set-card-profile", card, "off"], capture_output=True)

    # 4) retire owned recording/probe sinks and manual ALSA modules
    name, mods = None, {}
    for line in sh("pactl", "list", "sinks").splitlines():
        m = re.match(r"\s*Name: <?([^>\s]+)>?", line)
        if m:
            name = m.group(1)
        m = re.match(r"\s*Owner Module: (\d+)", line)
        if m and name:
            mods[name] = m.group(1)
    for sink, mid in mods.items():
        if sink.startswith((get("audio", "sink"), "botmic", "probe")):
            _drop(mid)
    _drop_modules(lambda n: n in ("module-alsa-source", "module-alsa-sink"))

    # 5) fresh topology
    _load(
        [
            "module-null-sink",
            "sink_name=" + get("audio", "sink"),
            "sink_properties=device.description=TranscriptAudio",
        ]
    )
    srcs = sh("pactl", "list", "short", "sources")
    if "alsa_input.hw_0_0" not in srcs and "snd_dummy" in sh("lsmod"):
        _load(
            ["module-alsa-source", "device=hw:0,0", "source_properties=device.description=BotMic"]
        )
    srcs = sh("pactl", "list", "short", "sources")
    mic = next((f.split("\t")[1] for f in srcs.splitlines() if "alsa_input" in f), "")
    if mic:
        subprocess.run(["pactl", "set-default-source", mic], capture_output=True)
    # defaults LAST: alsa modules can steal the default slots when loaded
    subprocess.run(["pactl", "set-default-sink", get("audio", "sink")], capture_output=True)

    ok = (
        get("audio", "sink") in sh("pactl", "list", "short", "sinks")
        and mic
        and mic in sh("pactl", "list", "short", "sources")
    )
    print(
        ("PULSE-OK" if ok else "PULSE-PROBLEM"),
        f"| mic={mic or 'NONE'}",
        "| default-sink:",
        sh("pactl", "get-default-sink").strip(),
    )


if __name__ == "__main__":
    main()
