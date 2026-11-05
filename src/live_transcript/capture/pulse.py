#!/usr/bin/env python3
"""Idempotent PulseAudio/ALSA topology for the Live Transcript bot.

- `scribe` null sink: Zoom's playback lands here; its monitor is recorded/chunked.
- Browser microphone: Chromium REFUSES PulseAudio monitor sources as mics, so the
  mic must be a real ALSA capture device: snd-aloop loopback card +
  module-alsa-source (name: alsa_input.hw_0_0 or scribe_loop_mic).
  TTS/test-tone reach the mic by playing into the paired sink scribe_loop_tx
  (kernel loopback routes playback->capture).
- Defaults: sink=scribe, source=loop mic.
NOTE: load the loopback source BEFORE Chrome starts (Chrome caches devices at
launch; every join launches fresh Chrome, so ordering is automatic).
"""

import re
import subprocess


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


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


def main():
    from live_transcript.runtime.session import assert_idle

    assert_idle()
    # 1) kernel loopback card (real capture device for the browser mic)
    if "snd_aloop" not in sh("lsmod"):
        subprocess.run(["sudo", "-n", "modprobe", "snd-aloop"], capture_output=True)

    # 1b) silence PulseAudio's auto-detected profile on the loopback card, else it
    #     holds pcm0p/pcm0c exclusively and our manual modules hit "Device busy"
    for row in sh("pactl", "list", "cards", "short").splitlines():
        card = row.split("\t")[1] if "\t" in row else ""
        if "aloop" in card or "Loopback" in card:
            subprocess.run(["pactl", "set-card-profile", card, "off"], capture_output=True)

    # 2) retire stale scribe/botmic/alsa modules from previous designs
    name, mods = None, {}
    for line in sh("pactl", "list", "sinks").splitlines():
        m = re.match(r"\s*Name: <?([^>\s]+)>?", line)
        if m:
            name = m.group(1)
        m = re.match(r"\s*Owner Module: (\d+)", line)
        if m and name:
            mods[name] = m.group(1)
    for sink, mid in mods.items():
        if sink.startswith(("scribe", "botmic")):
            _drop(mid)
    _drop_modules(lambda n: n in ("module-alsa-source", "module-alsa-sink"))

    # 3) recording sink
    _load(["module-null-sink", "sink_name=scribe", "sink_properties=device.description=ScribeRec"])

    # 4) mic: real ALSA capture + its tone-in partner
    srcs, sinks = sh("pactl", "list", "short", "sources"), sh("pactl", "list", "short", "sinks")
    if "alsa_input.hw_0_0" not in srcs and "scribe_loop_mic" not in srcs:
        _load(
            ["module-alsa-source", "device=hw:0,0", "source_properties=device.description=LoopMic"]
        )
    if "scribe_loop_tx" not in sinks:
        _load(["module-alsa-sink", "device=hw:0,0", "sink_properties=device.description=LoopTx"])
    srcs = sh("pactl", "list", "short", "sources")
    mic = next(
        (
            f.split("\t")[1]
            for f in srcs.splitlines()
            if "alsa_input" in f or "scribe_loop_mic" in f
        ),
        "botmic.monitor",
    )
    subprocess.run(["pactl", "set-default-source", mic], capture_output=True)
    # defaults LAST: module-alsa-sink would otherwise steal the default-sink slot
    subprocess.run(["pactl", "set-default-sink", "scribe"], capture_output=True)

    ok = "scribe" in sh("pactl", "list", "short", "sinks") and any(
        k in sh("pactl", "list", "short", "sources") for k in ("alsa_input", "scribe_loop_mic")
    )
    print(
        ("PULSE-OK" if ok else "PULSE-PROBLEM"),
        f"| mic={mic}",
        "| default-sink:",
        sh("pactl", "get-default-sink").strip(),
    )


if __name__ == "__main__":
    main()
