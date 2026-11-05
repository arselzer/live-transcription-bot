#!/usr/bin/env python3
"""Idle-only recording shutdown fixture; no network meeting is opened."""

import json
import os
from pathlib import Path
from live_transcript.core.config import get
import signal
import subprocess
import time
import wave
from live_transcript.capture.guard import Archive, atomic_json


def main():
    from playwright.sync_api import sync_playwright

    out = Path(get("vm", "home")) / "rec" / ("stop-fixture-" + str(time.time_ns()) + ".wav")
    sink = "stop_fixture_" + str(os.getpid())
    module = subprocess.check_output(
        ["pactl", "load-module", "module-null-sink", "sink_name=" + sink], text=True
    ).strip()
    stop = False

    def signal_stop(*_):
        nonlocal stop
        stop = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, signal_stop)
    archive = Archive(out)
    audio = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False, args=["--no-sandbox", "--remote-debugging-port=9258"]
            )
            page = browser.new_page()
            page.goto("about:blank")
            archive.start(
                time.time(), 90, cdp="http://127.0.0.1:9258", audio_source=sink + ".monitor"
            )
            audio = subprocess.Popen(
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
            print("STOP FIXTURE READY " + str(out), flush=True)
            while not stop:
                page.wait_for_timeout(100)
            audio.terminate()
            audio.wait(timeout=10)
            archive.stop()
            with wave.open(str(out)) as wav:
                count = wav.getnframes()
                rate = wav.getframerate()
            assert 0 < count < 90 * rate, (count, rate)
            assert (archive.root / "finalized.json").exists()
            result = dict(wav=str(out), frames=count, archive_sealed=True, clean_wav_header=True)
            atomic_json(out.with_suffix(".stop-report.json"), result)
            print("STOP FIXTURE COMPLETE " + json.dumps(result), flush=True)
            browser.close()
    finally:
        if audio and audio.poll() is None:
            audio.terminate()
            audio.wait(timeout=10)
        if not (archive.root / "finalized.json").exists():
            archive.stop()
        subprocess.run(["pactl", "unload-module", module], capture_output=True)


if __name__ == "__main__":
    main()
