"""Owned Jitsi lab processes inside the browser VM; no global audio changes."""

import argparse
import base64
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.parse import quote, urlsplit

from live_transcript.core.files import atomic_json
from live_transcript.worker import command


def read(path):
    path = Path(path)
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Oversized lab manifest")
    return json.loads(path.read_text())


def stop_process(proc, seconds=20):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=seconds)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


@contextmanager
def idle_lease(home):
    """Use the real VM's meeting lease, even with a separate lab data root."""
    logs = Path(home) / "logs"
    logs.mkdir(exist_ok=True)
    with (logs / "meeting-owner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        units = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=active,activating,deactivating",
                "--no-legend",
                "live-transcript-meeting-*",
                "live-transcript-archive-*",
                "live-transcript-desktop-*",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
        from live_transcript.runtime.console import displays

        if displays():
            raise RuntimeError("An existing desktop still occupies the test VM")
        if units.stdout.strip():
            raise RuntimeError("A meeting, desktop or archive still owns the VM")
        yield


def sender(root, speaker):
    from playwright.sync_api import sync_playwright
    from live_transcript.providers import jitsi
    from live_transcript.speech.browser import MIC_SCRIPT
    from live_transcript.speech.controller import VoiceController

    plan = read(root / "job.json")
    items = [p for p in plan["phrases"] if p["speaker"] == speaker]
    index = plan["speakers"].index(speaker)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(root / ("sender-profile-" + str(index))),
            headless=False,
            ignore_https_errors=True,
            locale="en-US",
            args=[
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--mute-audio",
            ],
            viewport={"width": 1000, "height": 700},
        )
        try:
            context.add_init_script(MIC_SCRIPT)
            parsed = urlsplit(plan["url"])
            context.grant_permissions(
                ["microphone", "camera"], origin=f"{parsed.scheme}://{parsed.netloc}"
            )
            page = context.new_page()
            page.goto(
                jitsi.navigation_url(plan["url"])
                + "&userInfo.displayName="
                + quote(speaker, safe=""),
                wait_until="domcontentloaded",
                timeout=60000,
            )
            jitsi.prejoin(page, speaker, timeout=90)
            page.wait_for_function(
                "name => window.APP?.store?.getState?.()?.['features/base/participants']?.local?.name===name",
                arg=speaker,
                timeout=5000,
            )
            if index == 0:
                snapshot = page.evaluate("""() => ({config:window.config||null,
                    version:window.JitsiMeetJS?.version||null})""")
                atomic_json(root / "server-info.json", snapshot)
            voice = VoiceController.__new__(VoiceController)
            voice.page, voice.provider = page, "jitsi"
            button = voice.mic_button(True)
            if button.is_visible():
                button.click(timeout=1500)
            page.wait_for_function(
                "() => window.__liveTranscriptVoice?.tracks.some(t=>t.readyState==='live' && t.enabled)",
                timeout=10000,
            )
            atomic_json(root / f"sender-{index}.ready.json", dict(utc=time.time(), speaker=speaker))
            deadline = time.monotonic() + 120
            while not (root / "start.json").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("The receiver never armed playback")
                page.wait_for_timeout(100)
            start = read(root / "start.json")["utc"]
            with (root / f"sender-{index}.jsonl").open("a", buffering=1) as events:
                for item in items:
                    encoded = base64.b64encode(
                        (root / "inputs" / item["file"]).read_bytes()
                    ).decode()
                    while time.time() < start + item["offset"]:
                        if not jitsi.joined(page):
                            raise RuntimeError("Scripted speaker lost admission")
                        page.wait_for_timeout(100)
                    result = page.evaluate(
                        """async encoded => {
                      const api=window.__liveTranscriptVoice;
                      if(!api?.tracks.some(t=>t.readyState==='live' && t.enabled))throw Error('No active scripted microphone');
                      return await api.play(encoded,false);
                    }""",
                        encoded,
                    )
                    events.write(
                        json.dumps(dict(item, started=result["started"], ended=result["ended"]))
                        + "\n"
                    )
                    events.flush()
                    os.fsync(events.fileno())
            atomic_json(root / f"sender-{index}.done.json", dict(ok=True, utc=time.time()))
        finally:
            context.close()


def execute(root):
    plan = read(root / "job.json")
    from live_transcript.providers.jitsi import validate_url

    validate_url(plan["url"])
    children = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    recording = root / "rec" / (plan["session"] + ".wav")
    archive = recording.with_suffix(".archive")
    result = dict(ok=False, started=time.time())

    def launch(arguments, name):
        with (root / (name + ".log")).open("ab") as log:
            process = subprocess.Popen(
                ["xvfb-run", "-a", "-s", "-screen 0 1280x800x24", *arguments],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        children.append(process)
        return process

    def wait_for(check, timeout, message):
        end = time.monotonic() + timeout
        while not check():
            if stopping:
                raise InterruptedError("Lab stop requested")
            if time.monotonic() >= end:
                raise TimeoutError(message)
            if children and children[0].poll() is not None:
                raise RuntimeError("Receiver exited; inspect receiver.log")
            time.sleep(0.25)

    with idle_lease(plan["owner_home"]):
        try:
            launch(
                command(
                    "join",
                    plan["url"],
                    "--name",
                    "Lab Recorder",
                    "--out",
                    str(recording),
                    "--max-min",
                    str(min(30, math.ceil((plan["duration"] + 180) / 60))),
                ),
                "receiver",
            )
            wait_for(
                lambda: (
                    archive.joinpath("health.json").exists()
                    and recording.exists()
                    and recording.stat().st_size > 44
                ),
                110,
                "Receiver did not begin capture",
            )
            for index, name in enumerate(plan["speakers"]):
                launch(
                    command("lab-guest", "sender", str(root), "--speaker", name), f"speaker-{index}"
                )
            wait_for(
                lambda: all(
                    (root / f"sender-{i}.ready.json").exists() for i in range(len(plan["speakers"]))
                ),
                110,
                "A scripted speaker did not join",
            )
            atomic_json(root / "start.json", dict(utc=time.time() + 3))
            wait_for(
                lambda: all(p.poll() is not None for p in children[1:]),
                plan["duration"] + 25,
                "Scripted playback did not complete",
            )
            if any(p.returncode for p in children[1:]):
                raise RuntimeError("A scripted speaker failed; inspect speaker logs")
            wait_for(
                lambda: time.time() >= read(root / "start.json")["utc"] + plan["duration"] + 10,
                20,
                "Caption tail did not complete",
            )
            result["ok"] = True
        except Exception as exc:
            result.update(error=type(exc).__name__, detail=str(exc)[:500])
        finally:
            # Senders are our children; never pkill browsers or alter default devices.
            for proc in reversed(children):
                stop_process(proc, seconds=95 if proc is children[0] else 15)
            if (
                archive.joinpath("config.json").exists()
                and not archive.joinpath("finalized.json").exists()
            ):
                from live_transcript.capture.guard import Archive

                try:
                    Archive(recording).stop()
                except Exception as exc:
                    result.update(ok=False, cleanup_error=type(exc).__name__)
            result.update(
                finished=time.time(), finalized=archive.joinpath("finalized.json").exists()
            )
            atomic_json(root / "result.json", result)
    return 0 if result["ok"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "sender", "cleanup"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--speaker")
    args = parser.parse_args()
    os.umask(0o077)
    if subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], timeout=5).returncode:
        raise RuntimeError("Lab browsers must stay inside the configured VM")
    for key in tuple(os.environ):
        if key.startswith(("CAPTION_", "LT_", "LIVE_TRANSCRIPT_")) and key not in (
            "LIVE_TRANSCRIPT_CONFIG",
            "LIVE_TRANSCRIPT_BUNDLE",
        ):
            os.environ.pop(key)
    root = args.root.absolute()
    if root.resolve() != root or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise ValueError("Lab data must be an owned directory without symlinks")
    if args.action == "cleanup":
        plan = read(root / "job.json")
        if plan["session"] != "lab-" + root.name:
            raise ValueError("Lab cleanup identity changed")
        unit = "live-transcript-lab-" + root.name
        stopped = subprocess.run(
            ["systemctl", "--user", "stop", unit], capture_output=True, timeout=125
        )
        if stopped.returncode not in (0, 5):
            raise RuntimeError("Could not stop owned lab unit")
        recording = root / "rec" / (plan["session"] + ".wav")
        archive = recording.with_suffix(".archive")
        if (archive / "config.json").exists() and not (archive / "finalized.json").exists():
            from live_transcript.capture.guard import Archive

            Archive(recording).stop()
        return 0
    if args.action == "sender":
        sender(root, args.speaker)
        return 0
    try:
        return execute(root)
    except Exception as exc:
        atomic_json(
            root / "result.json",
            dict(
                ok=False,
                error=type(exc).__name__,
                detail=str(exc)[:500],
                finished=time.time(),
                finalized=False,
            ),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
