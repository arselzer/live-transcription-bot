"""VM-only PulseAudio crash drill with synthetic WebRTC audio; no meeting joined.

Run under xvfb-run through the test guest bundle. The idle lease prevents this
explicitly disruptive drill from touching an existing meeting or desktop.
"""

import array
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import wave

from live_transcript.capture.audio import healthy, server_identity
from live_transcript.capture.guard import Archive
from live_transcript.core.config import get
from live_transcript.runtime.session import assert_idle
from live_transcript.speech.browser import MIC_SCRIPT
from live_transcript.speech.controller import VoiceController


def main():
    if subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], timeout=5).returncode:
        raise RuntimeError("This drill requires the isolated browser VM")
    assert_idle()
    home = Path(get("vm", "home"))
    with (home / "logs/meeting-owner.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        execute(home)


def execute(home):
    from playwright.sync_api import sync_playwright

    root = home / "audio-recovery-check" / str(time.time_ns())
    root.mkdir(parents=True, mode=0o700)
    (root / "rec").mkdir()

    def sample(name):
        path = root / (name + ".wav")
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "pulse",
                "-i",
                get("audio", "sink") + ".monitor",
                "-t",
                "2",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-n",
                str(path),
            ],
            check=True,
            timeout=10,
        )
        with wave.open(str(path)) as audio:
            pcm = array.array("h", audio.readframes(audio.getnframes()))
        return max(map(abs, pcm), default=0)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
            ignore_default_args=["--mute-audio"],
            env=dict(os.environ, PULSE_SINK=get("audio", "sink")),
            args=[
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        archive = None
        try:
            context = browser.new_context()
            context.add_init_script(MIC_SCRIPT)
            page = context.new_page()
            page.route(
                "https://example.invalid/audio",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="<h1>Audio recovery fixture</h1><audio autoplay></audio>",
                ),
            )
            page.goto("https://example.invalid/audio")
            page.evaluate("""async () => {
              window.ctx = new AudioContext();await ctx.setSinkId({type:'none'});await ctx.resume();
              window.mic = await navigator.mediaDevices.getUserMedia({audio:true});
              mic.getAudioTracks()[0].enabled=false;
              const dest=ctx.createMediaStreamDestination();const osc=ctx.createOscillator();const gain=ctx.createGain();
              gain.gain.value=.2;osc.frequency.value=550;osc.connect(gain).connect(dest);osc.start();
              window.a=new RTCPeerConnection();window.b=new RTCPeerConnection();
              a.onicecandidate=e=>e.candidate&&b.addIceCandidate(e.candidate);
              b.onicecandidate=e=>e.candidate&&a.addIceCandidate(e.candidate);
              b.ontrack=e=>{document.querySelector('audio').srcObject=e.streams[0];document.querySelector('audio').play();};
              dest.stream.getTracks().forEach(t=>a.addTrack(t,dest.stream));
              await a.setLocalDescription(await a.createOffer());await b.setRemoteDescription(a.localDescription);
              await b.setLocalDescription(await b.createAnswer());await a.setRemoteDescription(b.localDescription);
            }""")
            time.sleep(3)
            voice = VoiceController(
                page, "fixture", "Fixture recorder", directory=root, t0=time.time()
            )
            archive = Archive(root / "rec/fixture.wav")
            archive.start(time.time(), 100, provider="proton", cdp=None)
            before = sample("before")
            if before < 100:
                raise RuntimeError("Fixture downlink has no test signal")
            identity = server_identity()
            if not identity:
                raise RuntimeError("No unambiguous owned audio server")
            os.kill(identity["pid"], signal.SIGKILL)
            deadline = time.monotonic() + 15
            while not healthy():
                if time.monotonic() > deadline:
                    raise RuntimeError("Audio devices did not recover")
                time.sleep(0.25)
            voice.poll()
            time.sleep(5)
            after = sample("after")
            health = json.loads((archive.root / "health.json").read_text())
            events = [
                json.loads(line)
                for line in (archive.root / "events.jsonl").read_text().splitlines()
            ]
            mic = page.evaluate(
                "({live:mic.getAudioTracks()[0].readyState,enabled:mic.getAudioTracks()[0].enabled,clock:__liveTranscriptVoice.health()})"
            )
            result = dict(
                before_peak=before,
                after_peak=after,
                daemon_replaced=server_identity() != identity,
                mic=mic,
                audio_recording=health["audio"]["running"],
                screen_recording=health["screen"]["running"],
                events=[
                    e["kind"]
                    for e in events
                    if e["kind"].startswith(("audio_server_", "browser_audio_"))
                ],
                meeting_joined=False,
            )
            (root / "result.json").write_text(json.dumps(result))
            if (
                after < 100
                or mic["live"] != "live"
                or mic["enabled"]
                or not result["audio_recording"]
                or not result["screen_recording"]
            ):
                raise RuntimeError(
                    "Audio recovery or mute preservation failed; inspect " + str(root)
                )
            print(json.dumps(dict(ok=True, **result)), flush=True)
        finally:
            if archive:
                archive.stop()
            browser.close()


if __name__ == "__main__":
    main()
