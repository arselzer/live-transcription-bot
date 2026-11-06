#!/usr/bin/env python3
"""A separate, temporary meeting participant exercising the reusable voice mic."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
from live_transcript.core.config import get
import signal
import time
import os
import subprocess
import tempfile
import wave
import array
import math
import re
from urllib.parse import urlsplit
from live_transcript.speech.browser import MIC_SCRIPT, play_wav
from live_transcript.providers.proton import prejoin, click_if_visible, joined, PEERS_JS
from live_transcript.providers.admission import inspect_zoom, save_failure, claim_test_join


def zoom_prejoin(pg, name, timeout=180):
    from live_transcript.providers.zoom import joined_controls, admission_refused

    deadline = time.monotonic() + timeout
    submitted = False

    def click(control):
        try:
            if control.is_visible():
                control.click(timeout=1200, no_wait_after=True)
                return True
        except Exception:
            pass
        return False

    while time.monotonic() < deadline:
        for frame in pg.frames:
            for label in ("Got it", "Not now"):
                click(frame.get_by_role("button", name=label, exact=True))
        reason = inspect_zoom(pg, joined=joined_controls(pg))
        if reason or admission_refused(pg):
            error = RuntimeError(
                "Zoom refused test participant admission: " + (reason or "host_denied")
            )
            error.admission_reason = reason or "host_denied"
            raise error
        field = pg.locator("#input-for-name")
        if field.is_visible() and not submitted:
            field.fill(name)
            join = pg.get_by_role("button", name="Join", exact=True)
            if join.is_enabled() and click(join):
                submitted = True
        if joined_controls(pg):
            for frame in pg.frames:
                audio = frame.get_by_role("button", name=re.compile(r"^audio$", re.I)).first
                if click(audio):
                    pg.wait_for_timeout(500)
                join_audio = frame.get_by_role(
                    "button", name=re.compile("Join.*(?:Computer|Audio)", re.I)
                ).first
                click(join_audio)
                mute = frame.get_by_role("button", name=re.compile(r"^mute(?:\s|$)", re.I)).first
                if mute.is_visible():
                    return
                unmute = frame.get_by_role(
                    "button", name=re.compile(r"^unmute(?:\s|$)", re.I)
                ).first
                if click(unmute):
                    pg.wait_for_timeout(300)
            more = pg.get_by_role("button", name="More audio controls", exact=True)
            if click(more):
                if "leave computer audio" in pg.inner_text("body").lower():
                    pg.keyboard.press("Escape")
                    return
                pg.keyboard.press("Escape")
        pg.wait_for_timeout(500)
    raise TimeoutError("Test participant did not join Zoom audio")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meeting")
    ap.add_argument("--directory", default=str(Path(get("vm", "home")) / "tts-test"))
    ap.add_argument("--name", default="Audio Test · TTS")
    ap.add_argument("--port", type=int, default=9230)
    ap.add_argument("--phrase-ids", default="")
    ap.add_argument("--pause", type=float, default=7)
    ap.add_argument("--start-at", type=float, default=0)
    a = ap.parse_args()
    directory = Path(a.directory)
    plan = json.loads((directory / "plan.json").read_text())
    if a.phrase_ids:
        ids = {int(x) for x in a.phrase_ids.split(",")}
        plan = [p for p in plan if p["id"] in ids]
    proton = urlsplit(a.meeting).hostname == "meet.proton.me"
    google = urlsplit(a.meeting).hostname == "meet.google.com"
    from live_transcript.providers import google_meet

    claim_test_join("tts-" + str(a.port))
    sink = None
    module = None
    if not proton:
        sink = "audio_test_" + str(os.getpid())
        module = subprocess.check_output(
            ["pactl", "load-module", "module-null-sink", "sink_name=" + sink], text=True
        ).strip()
    from playwright.sync_api import sync_playwright

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    with sync_playwright() as p, ExitStack() as browser_stack:
        profile = Path(get("vm", "home")) / ("chrome-tts-test-prof-" + str(a.port))
        environment = dict(os.environ, **({"PULSE_SINK": sink} if sink else {}))
        flags = [
            "--no-sandbox",
            "--autoplay-policy=no-user-gesture-required",
            *(["--mute-audio"] if proton else []),
            "--remote-debugging-port=" + str(a.port),
            "--remote-debugging-address=127.0.0.1",
        ]
        if google:
            from live_transcript.providers.browser import google_context

            context = browser_stack.enter_context(
                google_context(p, profile, env=environment, args=flags, port=a.port)
            )
        else:
            context = p.chromium.launch_persistent_context(
                str(profile),
                headless=False,
                ignore_default_args=["--mute-audio"],
                env=environment,
                args=flags,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
        try:
            for origin in (
                ["https://meet.proton.me"]
                if proton
                else ["https://meet.google.com"]
                if google
                else ["https://zoom.us", "https://app.zoom.us", "https://us06web.zoom.us"]
            ):
                context.grant_permissions(["camera", "microphone"], origin=origin)
            context.add_init_script(MIC_SCRIPT)
            context.add_init_script(PEERS_JS)
            pg = context.new_page()
            from live_transcript.providers.zoom import meeting_url, joined_controls

            pg.goto(
                a.meeting if proton or google else meeting_url(a.meeting),
                wait_until="domcontentloaded",
                timeout=60000,
            )
            if proton:
                prejoin(pg, a.name, timeout=180)
                click_if_visible(pg, "Turn off camera")
                click_if_visible(pg, "Unmute microphone")
            elif google:
                google_meet.prejoin(pg, a.name, timeout=180)
                google_meet.click_if_visible(pg, "Turn off camera")
                google_meet.click_if_visible(pg, "Turn on microphone")
            else:
                zoom_prejoin(pg, a.name)
            print("TTS TEST PARTICIPANT JOINED", flush=True)
            pg.wait_for_timeout(3000)
            while time.time() < a.start_at:
                pg.wait_for_timeout(100)
            for phrase in plan:
                while a.start_at and time.time() < a.start_at + phrase.get("start_offset", 0):
                    pg.wait_for_timeout(100)
                admitted = (
                    joined(pg)
                    if proton
                    else google_meet.joined(pg)
                    if google
                    else joined_controls(pg)
                )
                if not admitted:
                    raise RuntimeError("test participant disconnected")
                if not proton:
                    from live_transcript.speech.controller import VoiceController

                    # A persisted Zoom profile may reapply mute after admission.
                    controls = VoiceController.__new__(VoiceController)
                    controls.page, controls.provider = pg, "google" if google else "zoom"
                    unmute = controls.mic_button(True)
                    if unmute is not None and unmute.is_visible():
                        unmute.click(timeout=1500, no_wait_after=True)
                        pg.wait_for_timeout(400)
                    active = controls.mic_button(False)
                    if active is None or not active.is_visible():
                        raise RuntimeError("Test sender microphone is still muted")
                wav = (directory / phrase["file"]).read_bytes()
                result = play_wav(pg, wav, monitor=False)
                event = dict(phrase, **result)
                with open(directory / "spoken.jsonl", "a") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                print("SPOKE", json.dumps(event, ensure_ascii=False), flush=True)
                pg.wait_for_timeout(a.pause * 1000)
            print("TTS TEST COMPLETE", flush=True)
            print("Listening briefly for a bot voice response", flush=True)
            if sink:
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "reply.wav"
                    capture = subprocess.Popen(
                        [
                            "ffmpeg",
                            "-nostdin",
                            "-v",
                            "error",
                            "-f",
                            "pulse",
                            "-i",
                            sink + ".monitor",
                            "-t",
                            "15",
                            "-ar",
                            "16000",
                            "-ac",
                            "1",
                            str(path),
                        ]
                    )
                    for _ in range(150):
                        pg.wait_for_timeout(100)
                    capture.wait(timeout=10)
                    with wave.open(str(path)) as audio:
                        samples = array.array("h", audio.readframes(audio.getnframes()))
                    reply = dict(
                        max_rms=math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))
                        / 32768,
                        tracks=1,
                        source="isolated meeting downlink sink",
                    )
            else:
                reply = pg.evaluate("""async () => {
                const ctx=new AudioContext(); await ctx.resume();
                const meters=[];
                for(const pc of window.__liveTranscriptPeers||[]) for(const r of pc.getReceivers()) {
                    if(r.track?.kind!=='audio') continue;
                    const src=ctx.createMediaStreamSource(new MediaStream([r.track]));
                    const meter=ctx.createAnalyser();meter.fftSize=2048;
                    const gain=ctx.createGain();gain.gain.value=0;
                    src.connect(meter);meter.connect(gain);gain.connect(ctx.destination);
                    meters.push(meter);
                }
                let peak=0;const buf=new Float32Array(2048);
                for(let i=0;i<300;i++) {
                    for(const m of meters){m.getFloatTimeDomainData(buf);
                        peak=Math.max(peak,Math.sqrt(buf.reduce((s,v)=>s+v*v,0)/buf.length));}
                    await new Promise(r=>setTimeout(r,50));
                }
                await ctx.close();return {max_rms:peak,tracks:meters.length};
            }""")
            print("VOICE_REPLY", json.dumps(reply), flush=True)
        except Exception as exc:
            try:
                reason = getattr(exc, "admission_reason", None) or (
                    getattr(exc, "reason", "admission_error")
                    if proton or google
                    else inspect_zoom(pg, joined=joined_controls(pg)) or "admission_error"
                )
                save_failure(
                    pg,
                    directory,
                    reason,
                    a.name,
                    "proton" if proton else "google" if google else "zoom",
                )
            except Exception:
                pass
            raise
        finally:
            for page in context.pages:
                try:
                    if proton:
                        control = page.get_by_role("button", name="Leave Meeting", exact=True).first
                    else:
                        control = page.get_by_role("button", name="Leave", exact=True).first
                    if control.is_visible():
                        control.click(timeout=1500)
                        for label in ("Leave Meeting", "Leave meeting", "Leave"):
                            confirm = (
                                page.get_by_role("dialog")
                                .get_by_role("button", name=label, exact=True)
                                .first
                            )
                            if confirm.is_visible():
                                confirm.click(timeout=1500)
                                break
                except Exception:
                    pass
            if not google:
                context.close()
            if module:
                subprocess.run(["pactl", "unload-module", module], capture_output=True)


if __name__ == "__main__":
    main()
