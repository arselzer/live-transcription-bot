"""Persistent PulseAudio devices for the dedicated browser VM account.

Topology changes require the idle VM lease. Once installed, systemd recreates
identical devices on every PulseAudio restart without resetting a live browser.
The microphone uses snd-dummy, whose flowing silence keeps WebRTC audio alive.
"""

from contextlib import nullcontext
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from live_transcript.core.config import get
from live_transcript.core.files import atomic_text
from live_transcript.runtime.session import assert_idle
from live_transcript.worker import is_role_process


def startup_script(sink, device, source):
    for value in (sink, source):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", value):
            raise ValueError("Invalid PulseAudio device name")
    if not re.fullmatch(r"hw:[0-9]{1,2},[0-9]{1,2}", device):
        raise ValueError("Microphone device must identify an ALSA hardware device")
    # No udev auto-profiles: they would compete with the explicit dummy source
    # for the same PCM. Do not include suspend-on-idle, which can starve WebRTC.
    content = (
        "# Managed browser VM audio devices\n.fail\n"
        "load-module module-native-protocol-unix\n"
        f"load-module module-null-sink sink_name={sink} rate=48000 channels=2 "
        "sink_properties=device.description=TranscriptAudio\n"
        f"load-module module-alsa-source device={device} source_name={source} "
        "rate=48000 channels=2 source_properties=device.description=BotMic\n"
        f"set-default-sink {sink}\nset-default-source {source}\n"
    )
    # Lab escape hatch (e.g. a scripted test speaker feeding an extra null-sink).
    # Unset by default, so production PulseAudio config stays byte-identical.
    extra = str(get("audio", "extra_startup_file") or "").strip()
    if extra and re.fullmatch(r"[A-Za-z0-9/._-]{1,255}", extra) and Path(extra).is_file():
        content += f".include {extra}\n"
    return content


def server_identity():
    """Identify this account's audio daemon without depending on a working socket."""
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            args = (path / "cmdline").read_bytes().split(b"\0")
            if args and Path(os.fsdecode(args[0])).name == "pulseaudio":
                ticks = (path / "stat").read_text().rsplit(")", 1)[1].split()[19]
                result.append(dict(pid=int(path.name), ticks=ticks))
        except (OSError, IndexError, ValueError):
            continue
    return result[0] if len(result) == 1 else None


def run(*args, check=True, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=timeout)


def healthy():
    try:
        sinks = run("pactl", "list", "short", "sinks").stdout.splitlines()
        sources = run("pactl", "list", "short", "sources").stdout.splitlines()
        return (
            get("audio", "sink") in [line.split()[1] for line in sinks if len(line.split()) > 1]
            and get("audio", "microphone_source")
            in [line.split()[1] for line in sources if len(line.split()) > 1]
            and run("pactl", "get-default-sink").stdout.strip() == get("audio", "sink")
            and run("pactl", "get-default-source").stdout.strip()
            == get("audio", "microphone_source")
        )
    except (OSError, subprocess.SubprocessError):
        return False


def configure():
    archives = run(
        "systemctl",
        "--user",
        "list-units",
        "--state=active,activating,deactivating",
        "--no-legend",
        "live-transcript-archive-*",
    ).stdout
    if archives.strip():
        raise RuntimeError("Archival capture is active; refusing to reset its audio source")
    script = Path.home() / ".config/pulse/live-transcript.pa"
    script.parent.mkdir(parents=True, exist_ok=True)
    content = startup_script(
        get("audio", "sink"), get("audio", "microphone_device"), get("audio", "microphone_source")
    )
    dropin = Path.home() / ".config/systemd/user/pulseaudio.service.d/live-transcript.conf"
    dropin.parent.mkdir(parents=True, exist_ok=True)
    # %h is expanded by systemd, avoiding shell quoting/user-path interpolation.
    unit = (
        "[Unit]\nStartLimitIntervalSec=0\n[Service]\nExecStart=\n"
        "ExecStart=/usr/bin/pulseaudio -n --file=%h/.config/pulse/live-transcript.pa "
        "--daemonize=no --log-target=journal --exit-idle-time=-1 --disallow-exit\n"
        "Restart=always\nRestartSec=2\n"
    )
    changed = (
        not script.exists()
        or script.read_text() != content
        or not dropin.exists()
        or dropin.read_text() != unit
    )
    if changed:
        atomic_text(script, content)
        atomic_text(dropin, unit)
        run("systemctl", "--user", "daemon-reload")
    client = script.parent / "client.conf"
    previous = client.read_text() if client.exists() else ""
    value = re.sub(r"^\s*autospawn\s*=.*$", "autospawn = no", previous, flags=re.M)
    if not re.search(r"^\s*autospawn\s*=", value, re.M):
        value += "\nautospawn = no\n"
    if value != previous:
        atomic_text(client, value)
    if changed or not healthy():
        run(
            "systemctl",
            "--user",
            "stop",
            "pulseaudio.service",
            "pulseaudio.socket",
            check=False,
            timeout=15,
        )
        # Retire only an owned legacy daemon that is still present after stop.
        identity = server_identity()
        if identity:
            if server_identity() == identity:
                try:
                    os.kill(identity["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 5
            while server_identity() == identity and time.monotonic() < deadline:
                time.sleep(0.1)
            if server_identity() == identity:
                raise RuntimeError("Legacy audio daemon did not stop")
        run("sudo", "-n", "modprobe", "snd-dummy")
        if "snd_aloop " in Path("/proc/modules").read_text():
            run("sudo", "-n", "rmmod", "snd-aloop")
        run(
            "systemctl",
            "--user",
            "reset-failed",
            "pulseaudio.service",
            "pulseaudio.socket",
            check=False,
        )
        run("systemctl", "--user", "enable", "pulseaudio.service", "pulseaudio.socket")
        run("systemctl", "--user", "start", "pulseaudio.socket", "pulseaudio.service", timeout=15)
    deadline = time.monotonic() + 10
    while not healthy():
        if time.monotonic() >= deadline:
            raise RuntimeError("Managed audio devices did not become ready")
        time.sleep(0.25)
    print("PULSE-OK | persistent devices | mic=" + get("audio", "microphone_source"), flush=True)


def main():
    if run("systemd-detect-virt", "--vm", "--quiet", check=False).returncode:
        raise RuntimeError("Audio preparation must run inside the browser VM")
    parent = Path(f"/proc/{os.getppid()}/cmdline").read_bytes().split(b"\0")
    leased = os.environ.get("LT_PREP_OWNER") == str(os.getppid()) and is_role_process(
        parent, {"prepare", "session", "check-media"}
    )
    if not leased:
        assert_idle()
    with (
        nullcontext()
        if leased
        else (Path(get("vm", "home")) / "logs/meeting-owner.lock").open("a") as lease
    ):
        if lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        configure()


if __name__ == "__main__":
    main()
