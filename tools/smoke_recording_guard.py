#!/usr/bin/env python3
"""VM-only isolated archive failure drill. Never joins or plays into a meeting.

Uses a private PulseAudio sink and a locally fulfilled fake participant page.
Kills a recorder and the archive supervisor, then checks recovery, speaker
evidence, snapshots, video and lossless audio. Run under xvfb-run.
"""

import json
import math
import os
from pathlib import Path
from live_transcript.core.config import get
import signal
import struct
import subprocess
import sys
import tempfile
import time
import wave
import argparse
import html

from live_transcript.capture.guard import Archive, event


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise TimeoutError("failure-drill condition did not arrive")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("proton", "zoom"), default="proton")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    name = "archive-fixture-" + str(time.time_ns())
    out = Path(get("vm", "home")) / "rec" / (name + ".wav")
    archive = Archive(out)
    sink = "lt_archive_test_" + str(os.getpid())
    module = subprocess.check_output(
        ["pactl", "load-module", "module-null-sink", "sink_name=" + sink], text=True
    ).strip()
    player = None
    caption = None
    primary = None
    ready = False
    try:
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
            tone = Path(tmp) / "tone.wav"
            with wave.open(str(tone), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                one = b"".join(
                    struct.pack("<h", int(7000 * math.sin(2 * math.pi * 440 * i / 48000)))
                    for i in range(48000)
                )
                wav.writeframes(one * 120)
            port = 9248 if args.provider == "proton" else 9249
            browser = p.chromium.launch(
                headless=False,
                ignore_default_args=["--mute-audio"],
                env=dict(os.environ, PULSE_SINK=sink),
                args=[
                    "--no-sandbox",
                    "--remote-debugging-port=" + str(port),
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            page = browser.new_page(viewport=dict(width=1280, height=800))
            url = (
                "https://meet.proton.me/archive-fixture"
                if args.provider == "proton"
                else "https://app.zoom.us/wc/12345678901/join"
            )
            body = """<body style="background:#192d3f;color:white;font:32px sans-serif">
                <h1>Isolated recording failure drill</h1>
                <div class="participant-tile-body"><div class="speaker-border"></div>
                <div class="participant-tile-name"><span>Fixture Alice</span></div></div>
                <script>setInterval(()=>{document.querySelector('span').textContent=
                document.querySelector('span').textContent.includes('Alice')?'Fixture Bob':'Fixture Alice'},2000)</script>"""
            if args.provider == "zoom":
                child = """<div class="speaker-active-container"><span class="name-label">Fixture Alice</span></div>
                <button aria-label="Participants">Participants</button><button aria-label="Leave Meeting">Leave</button>
                <button aria-label="Unmute my microphone (Alt+A)" onclick="this.setAttribute('aria-label',
                  this.getAttribute('aria-label').startsWith('Unmute')?'Mute my microphone (Alt+A)':'Unmute my microphone (Alt+A)')">Microphone</button>
                <script>setInterval(()=>{document.querySelector('span').textContent=
                document.querySelector('span').textContent.includes('Alice')?'Fixture Bob':'Fixture Alice'},2000)</script>"""
                body = (
                    '<h1>Isolated Zoom iframe fixture</h1><iframe style="width:100%;height:400px" srcdoc="'
                    + html.escape(child, quote=True)
                    + '"></iframe>'
                )
                from live_transcript.speech.browser import MIC_SCRIPT

                page.context.add_init_script(MIC_SCRIPT)
                page.context.grant_permissions(["microphone"], origin="https://app.zoom.us")
            page.route(url, lambda route: route.fulfill(content_type="text/html", body=body))
            page.goto(url)
            voice_verified = False
            if args.provider == "zoom":
                page.evaluate("""async () => {
                  const stream=await navigator.mediaDevices.getUserMedia({audio:true});
                  const ctx=new AudioContext();await ctx.resume();
                  const analyser=ctx.createAnalyser();ctx.createMediaStreamSource(stream).connect(analyser);
                  window.fixtureMicMax=0;setInterval(()=>{const a=new Float32Array(analyser.fftSize);
                    analyser.getFloatTimeDomainData(a);window.fixtureMicMax=Math.max(window.fixtureMicMax,
                    Math.sqrt(a.reduce((n,x)=>n+x*x,0)/a.length));},20);
                }""")
                from live_transcript.speech.controller import VoiceController

                home = Path(tmp) / "voice"
                home.mkdir()
                (home / "rec").mkdir()
                voice = VoiceController(
                    page, name, "Voice fixture", directory=home, t0=time.time(), provider="zoom"
                )
                ident = voice.enqueue("This is the isolated Zoom voice test.", "en")
                voice.poll()
                response = json.loads((voice.done / (ident + ".json")).read_text())
                assert response["ok"], response
                voice_verified = page.evaluate("window.fixtureMicMax") > 0.001
                assert voice_verified, "virtual microphone carried no speech"
                assert (
                    page.frames[-1]
                    .get_by_role("button", name="Unmute my microphone (Alt+A)", exact=True)
                    .is_visible()
                )
                print(
                    "PASS Zoom iframe voice: virtual microphone carried TTS and mute restored",
                    flush=True,
                )
                from live_transcript.runtime.entry import joined_controls

                assert joined_controls(page), "visible in-call controls were not recognized"
                page.frames[-1].evaluate(
                    'document.querySelector("button[aria-label=\\"Leave Meeting\\"]").remove()'
                )
                assert not joined_controls(page), (
                    "pre-join controls incorrectly confirmed admission"
                )
                print(
                    "PASS Zoom admission requires visible leave and participant controls",
                    flush=True,
                )
            player = subprocess.Popen(["paplay", "--device=" + sink, str(tone)])
            archive.start(
                time.time(),
                90,
                provider=args.provider,
                cdp="http://127.0.0.1:" + str(port),
                audio_source=sink + ".monitor",
            )
            ready = True
            primary = subprocess.Popen(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "pulse",
                    "-i",
                    sink + ".monitor",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-n",
                    str(out),
                ]
            )
            event(archive.root, "primary_capture_started", file=out.name)
            fixture_home = Path(tmp) / "caption"
            fixture_home.mkdir()
            env = dict(
                os.environ,
                SCRIBE_DIR=str(fixture_home),
                CAPTION_HOST="127.0.0.1",
                CAPTION_PORT="1",
                CAPTION_AUDIO_SOURCE=sink + ".monitor",
                CAPTION_LOCK=str(Path(tmp) / "caption.lock"),
                CAPTION_PID_FILE=str(Path(tmp) / "caption.pid"),
            )
            with open(archive.root / "caption-failure-drill.log", "ab") as log:
                caption = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(get("paths", "root")) / "caption_stream.py"),
                        name,
                        "--no-video",
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            wait_for(lambda: (archive.root / "speakers.jsonl").exists())
            time.sleep(5)
            assert caption.poll() is None, "caption fixture failed to start"
            os.kill(caption.pid, signal.SIGKILL)
            caption.wait()
            time.sleep(6)
            assert any(p.stat().st_size for p in archive.root.glob("audio-*.flac"))
            print(
                "PASS killed caption streamer with unreachable ASR host: archive capture continues",
                flush=True,
            )
            events = lambda: [
                json.loads(x) for x in (archive.root / "events.jsonl").read_text().splitlines()
            ]
            audio_events = [
                x
                for x in events()
                if x.get("component") == "audio" and x["kind"] == "capture_started"
            ]
            os.kill(audio_events[-1]["pid"], signal.SIGKILL)
            wait_for(
                lambda: (
                    len(
                        [
                            x
                            for x in events()
                            if x.get("component") == "audio" and x["kind"] == "capture_started"
                        ]
                    )
                    >= 2
                )
            )
            print(
                "PASS killed audio recorder: independent supervisor restarted capture", flush=True
            )
            guard_pid = int(
                subprocess.check_output(
                    ["systemctl", "--user", "show", archive.unit, "-p", "MainPID", "--value"],
                    text=True,
                )
            )
            os.kill(guard_pid, signal.SIGKILL)
            wait_for(
                lambda: (
                    int(
                        subprocess.check_output(
                            [
                                "systemctl",
                                "--user",
                                "show",
                                archive.unit,
                                "-p",
                                "MainPID",
                                "--value",
                            ],
                            text=True,
                        )
                    )
                    not in (0, guard_pid)
                )
            )
            wait_for(lambda: len([x for x in events() if x["kind"] == "guard_started"]) >= 2)
            print("PASS killed archive supervisor: systemd restarted it", flush=True)
            time.sleep(12)
            primary.send_signal(signal.SIGINT)
            primary.wait(timeout=10)
            archive.stop()
            ready = False
            browser.close()
            rows = [
                json.loads(x) for x in (archive.root / "speakers.jsonl").read_text().splitlines()
            ]
            names = {person["name"] for row in rows for person in row["who"]}
            assert {"Fixture Alice", "Fixture Bob"} <= names, names
            media = {}
            for extension in ("flac", "mkv", "jpg"):
                files = list(archive.root.glob("*." + extension))
                assert files, extension
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_name",
                        "-of",
                        "json",
                        str(files[-1]),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                media[extension] = dict(
                    files=len(files), streams=json.loads(probe.stdout)["streams"]
                )
            report = dict(
                archive=str(archive.root),
                audio_restart=True,
                guard_restart=True,
                provider=args.provider,
                zoom_voice_verified=voice_verified,
                caption_failure_survived=True,
                speaker_rows=len(rows),
                speakers=sorted(names),
                media=media,
                notes="Isolated private sink and local fixture; no meeting joined or audio injected.",
            )
            (archive.root / "drill-report.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2), flush=True)
    finally:
        if ready:
            archive.stop()
        if player and player.poll() is None:
            player.terminate()
            player.wait()
        for proc in (caption, primary):
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        subprocess.run(["pactl", "unload-module", module], check=False)


if __name__ == "__main__":
    main()
