"""Exercise the owned VM's real audio and caption camera without joining a room."""

import array
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import wave

from live_transcript.core.config import get
from live_transcript.core.files import atomic_json
from live_transcript.runtime.prepare import prepare_contents
from live_transcript.runtime.session import assert_idle
from live_transcript.worker import command


def main():
    os.umask(0o077)
    home = Path(get("vm", "home"))
    assert_idle()
    with (home / "logs/meeting-owner.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare_contents()
        root = home / "setup-check" / str(time.time_ns())
        root.mkdir(parents=True, mode=0o700)
        with (root / "render.log").open("wb") as log:
            renderer = subprocess.Popen(
                command("render"), stdout=log, stderr=log, start_new_session=True
            )
        recorder = None
        try:
            time.sleep(4)
            if renderer.poll() is not None:
                raise RuntimeError("Caption camera renderer exited; inspect " + str(root))
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "v4l2",
                    "-i",
                    "/dev/video0",
                    "-frames:v",
                    "3",
                    "-c:v",
                    "ffv1",
                    "-n",
                    str(root / "camera.mkv"),
                ],
                check=True,
                timeout=25,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-n",
                    str(root / "tone.wav"),
                ],
                check=True,
                timeout=10,
            )
            with (root / "audio.log").open("wb") as log:
                recorder = subprocess.Popen(
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
                        "4",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-n",
                        str(root / "capture.wav"),
                    ],
                    stdout=log,
                    stderr=log,
                )
            time.sleep(0.5)
            subprocess.run(
                ["paplay", "--device=" + get("audio", "sink"), str(root / "tone.wav")],
                check=True,
                timeout=10,
            )
            if recorder.wait(timeout=15):
                raise RuntimeError("Audio recorder failed; inspect " + str(root))
            with wave.open(str(root / "capture.wav")) as stream:
                samples = array.array("h", stream.readframes(stream.getnframes()))
            peak = max(map(abs, samples), default=0)
            if peak < 100:
                raise RuntimeError("Recorded audio contains no test signal")
            result = dict(
                ok=True,
                audio_peak=peak,
                camera_bytes=(root / "camera.mkv").stat().st_size,
                directory=str(root),
                meeting_joined=False,
            )
            atomic_json(root / "result.json", result)
            print(json.dumps(result), flush=True)
        finally:
            if recorder and recorder.poll() is None:
                recorder.terminate()
                try:
                    recorder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                    recorder.wait(timeout=5)
            try:
                os.killpg(renderer.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                renderer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(renderer.pid, signal.SIGKILL)
                renderer.wait(timeout=5)


if __name__ == "__main__":
    main()
