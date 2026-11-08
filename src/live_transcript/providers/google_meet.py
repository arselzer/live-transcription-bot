#!/usr/bin/env python3
"""Google Meet adapter for the existing Live Transcript VM and caption daemon.

Selectors rely on Meet's stable aria-labels (Join now, Leave call,
Turn on/off microphone, Turn on/off camera), not on obfuscated class names.
Transport health reuses the generic RTCPeerConnection observer.
Archival capture is independent of live captions and keeps source evidence.
"""

from live_transcript.worker import command as worker_command
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

ORIGIN = "https://meet.google.com"
from live_transcript.core.config import get

from live_transcript.providers.google_admission import (
    AdmissionRefused as AdmissionRefused,
    button as button,
    click_if_visible as click_if_visible,
    visible as visible,
    joined as joined,
    prejoin as prejoin,
    admit,
    navigation_url,
    watch_refusals,
)

VM_HOME = Path(get("vm", "home"))


def is_google_url(url):
    return urlsplit(url).hostname == "meet.google.com"


def validate_url(url):
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.netloc != "meet.google.com"
        or not re.fullmatch(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}/?", p.path)
    ):
        raise ValueError("Expected a full https://meet.google.com/xxx-xxxx-xxx meeting link")
    return url


def public_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def optional_feature(callback, label, default=None):
    try:
        return callback()
    except Exception as exc:
        print(
            f"{label} unavailable; archival recording continues: {type(exc).__name__}", flush=True
        )
        return default


def meeting_ended(page):
    """Use explicit UI end notices, never arbitrary participant/chat text."""
    pattern = re.compile(
        r"(?:call (?:has )?ended|this call has ended|you (?:have )?left (?:the )?call|"
        r"the host (?:ended|declined)|(?:you were|have been) removed)",
        re.I,
    )
    frames = getattr(page, "frames", [page])
    for frame in frames:
        for role in ("dialog", "heading"):
            try:
                if any(
                    pattern.search(text) for text in frame.get_by_role(role).all_text_contents()
                ):
                    return True
            except Exception:
                pass
    return False


# Meet tile internals are obfuscated; names come from participant tile
# aria-labels where present. An empty list is safe: diarization attributes
# speech by voice independently of the visual tile scrape.
SPEAKERS_JS = """() => [...document.querySelectorAll('[aria-label]')]
  .filter(e => e.closest('[data-realmlib-id], [jsname]') && /speaking|joined|left/.test(e.getAttribute('aria-label')))
  .map(e => ({role: /is speaking/.test(e.getAttribute('aria-label')) ? 'active' : 'tile',
              name: e.getAttribute('aria-label').replace(/ is speaking$/,'').trim().slice(0,64)}))
  .filter(x => x.name).slice(0, 100)"""

# Observe transport counters without touching Meet internals or media.
PEERS_JS = """(() => {
  const peers = window.__liveTranscriptPeers = [];
  const Native = window.RTCPeerConnection;
  window.RTCPeerConnection = new Proxy(Native, {construct(target, args) {
    const pc = Reflect.construct(target, args);
    for (let i=peers.length-1; i>=0; --i)
      if (peers[i].connectionState === 'closed') peers.splice(i, 1);
    peers.push(pc); return pc;
  }});
})()"""

STATS_JS = """async () => {
  const stats = {available: Array.isArray(window.__liveTranscriptPeers),
    audioBytes: 0, audioPackets: 0, audioEnergy: 0, states: []};
  await Promise.all((window.__liveTranscriptPeers || []).map(async pc => {
    stats.states.push(pc.connectionState);
    const report = await Promise.race([pc.getStats().catch(() => []),
      new Promise(resolve => setTimeout(() => resolve([]), 1500))]);
    report.forEach(s => {
      if (s.type === 'inbound-rtp' && (s.kind || s.mediaType) === 'audio') {
        stats.audioBytes += s.bytesReceived || 0;
        stats.audioPackets += s.packetsReceived || 0;
        stats.audioEnergy += s.totalAudioEnergy || 0;
      }
    });
  }));
  return stats;
}"""


def configure_media_hooks(context):
    """Allow admission comparisons with native microphone and WebRTC APIs."""
    if not get("google", "browser_media_hooks"):
        return False
    from live_transcript.speech.browser import MIC_SCRIPT

    context.add_init_script(PEERS_JS)
    context.add_init_script(MIC_SCRIPT)
    return True


def monitor_audio(children, output, log):
    """The convenience WAV may fail independently of the archival recorder."""
    return optional_feature(
        lambda: children.start(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "pulse",
                "-i",
                get("audio", "sink") + ".monitor",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-n",
                str(output),
            ],
            log,
        ),
        "convenience WAV",
    )


class Children:
    """Own complete process groups, including renderer/capture descendants."""

    def __init__(self):
        self.procs = []

    def start(self, args, log):
        with open(log, "ab") as fh:
            proc = subprocess.Popen(
                args,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        self.procs.append(proc)
        return proc

    def stop(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        self.procs.remove(proc)

    def close(self):
        for proc in reversed(self.procs):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        end = time.monotonic() + 12
        for proc in reversed(self.procs):
            try:
                proc.wait(timeout=max(0.01, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        self.procs.clear()


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("meeting", type=validate_url)
    ap.add_argument("--name", default=get("identity", "name"))
    ap.add_argument("--out")
    ap.add_argument("--max-min", type=int, default=180)
    ap.add_argument(
        "--dry", action="store_true", help="inspect pre-join without joining or recording"
    )
    ap.add_argument("--passcode", help="unused: Google Meet needs no passcode for room links")
    a = ap.parse_args(argv)
    if not 1 <= a.max_min <= 480 or not a.name.strip() or len(a.name) > 60:
        ap.error("--max-min must be positive and --name must contain 1–60 characters")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("Run under xvfb-run: headless Chromium cannot supply meeting audio")
    from playwright.sync_api import sync_playwright
    from live_transcript.speech.controller import VoiceController
    from live_transcript.capture.guard import Archive, event

    os.umask(0o077)
    os.environ["CAPTION_LAYOUT"] = "standard"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    from live_transcript.core.options import validate_output

    try:
        out = validate_output(a.out or VM_HOME / "rec" / f"{stamp}-{uuid.uuid4().hex[:6]}.wav")
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix != ".wav" or out.exists():
        raise ValueError("Output must be a new .wav file")
    children = Children()
    recording = False
    ctx = None
    archive = None
    session_deadline = time.monotonic() + max(
        0,
        min(
            a.max_min * 60,
            float(os.environ.get("LT_DEADLINE", time.time() + a.max_min * 60)) - time.time(),
        ),
    )
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    try:
        with sync_playwright() as p, ExitStack() as browser_stack:
            try:
                if not a.dry:
                    if session_deadline <= time.monotonic():
                        raise AdmissionRefused(
                            "Original meeting deadline expired", "google_admission_timeout"
                        )
                    # Archive the complete join/recovery sequence independently
                    # of browser automation and caption transport.
                    archive = Archive(out)
                    evidence_started = time.time()
                    evidence_clock = time.monotonic()
                    archive.start(
                        evidence_started,
                        max(0, session_deadline - time.monotonic()),
                        provider="google",
                    )
                    print("ARCHIVE to", archive.root, flush=True)
                    # Pre-arm the camera writer before Chrome enumerates V4L2.
                    # The renderer must win the device format race.
                    Path("/tmp/cap_session_start").write_text(str(int(time.time())))
                    renderer = optional_feature(
                        lambda: children.start(worker_command("render"), "/tmp/meet-render.log"),
                        "caption camera",
                    )
                    time.sleep(3)
                from live_transcript.providers.browser import google_context

                ctx = browser_stack.enter_context(
                    google_context(
                        p,
                        VM_HOME / "chrome-meet-prof",
                        env=dict(os.environ, PULSE_SINK=get("audio", "sink")),
                        args=[
                            "--no-sandbox",
                            "--hide-crash-restore-bubble",
                            "--autoplay-policy=no-user-gesture-required",
                            "--remote-debugging-port=9222",
                            "--remote-debugging-address=127.0.0.1",
                        ],
                    )
                )
                if not get("browser", "google_private"):
                    from live_transcript.providers.browser import google_profile_ready

                    if not google_profile_ready(ctx):
                        raise AdmissionRefused(
                            "Dedicated Google profile is not signed in",
                            "google_auth_session_missing",
                        )
                ctx.grant_permissions(["camera", "microphone"], origin=ORIGIN)
                media_hooks = configure_media_hooks(ctx)
                print("GOOGLE_MEDIA_HOOKS", "enabled" if media_hooks else "disabled", flush=True)
                pg = ctx.new_page()
                pg.set_default_timeout(2500)
                pg.set_viewport_size({"width": 1280, "height": 800})
                pg.on("dialog", lambda dialog: dialog.dismiss())
                watch_refusals(pg)
                navigation_failure = None
                try:
                    pg.goto(
                        navigation_url(a.meeting),
                        wait_until="domcontentloaded",
                        timeout=max(1, min(60000, (session_deadline - time.monotonic()) * 1000)),
                    )
                except Exception as exc:
                    if a.dry:
                        raise
                    navigation_failure = AdmissionRefused(
                        "Google navigation failed: " + type(exc).__name__,
                        "google_navigation_failed",
                    )
                if a.dry:
                    pg.wait_for_timeout(8000)
                    pg.screenshot(path=str(out.parent / "meet-prejoin.png"))
                    labels = pg.evaluate("""() => [...document.querySelectorAll('[role=button],button,[aria-label]')]
                        .map(e => (e.getAttribute('aria-label')||e.textContent||'').trim().slice(0,60)).filter(Boolean).slice(0,80)""")
                    print("DRY BUTTONS:", json.dumps(sorted(set(labels))), flush=True)
                    print(
                        "DRY TEXT:",
                        json.dumps(pg.evaluate("() => document.body.innerText.slice(0,1200)")),
                        flush=True,
                    )
                    print("DRY: inspected pre-join; did not join or record", flush=True)
                    return
                interaction = None
                if (
                    not get("google", "manual_join")
                    and navigation_failure is None
                    and get("google", "input_mode") == "native"
                ):
                    from live_transcript.providers.interaction import NativeInput, NativeInputError

                    try:
                        interaction = NativeInput(pg)
                    except NativeInputError as exc:
                        navigation_failure = AdmissionRefused(str(exc), "google_input_unavailable")
                remaining = session_deadline - time.monotonic()
                if remaining <= 0:
                    raise AdmissionRefused(
                        "Original meeting deadline expired", "google_admission_timeout"
                    )
                join_mode = admit(
                    pg,
                    a.name,
                    deadline=session_deadline,
                    timeout=min(get("google", "admission_timeout"), remaining),
                    operator_timeout=get("google", "operator_recovery_seconds"),
                    manual=get("google", "manual_join"),
                    interaction=interaction,
                    require_sign_in=not get("browser", "google_private"),
                    diagnostics=out.with_suffix(".admission"),
                    status_file=archive.root / "admission.json",
                    initial_failure=navigation_failure,
                )
                print("JOINED Google Meet (Leave call confirmed)", flush=True)
                if os.environ.get("LT_SESSION_MARKER"):
                    Path(os.environ["LT_SESSION_MARKER"]).write_text(
                        json.dumps(dict(utc=time.time(), provider="google"))
                    )
                print(
                    "ADMISSION_SUCCEEDED "
                    + json.dumps(
                        dict(
                            name=a.name
                            if get("browser", "google_private") and join_mode == "automatic"
                            else None,
                            provider="google",
                            join_mode=join_mode,
                            identity_source="guest"
                            if get("browser", "google_private")
                            else "profile",
                        )
                    ),
                    flush=True,
                )
                t0 = evidence_clock
                started = time.time()
                optional_feature(
                    lambda: (VM_HOME / "rec" / "speakers.jsonl").write_text(""), "live speaker log"
                )
                # Captions, voice events and browser speaker observations share
                # the archive's clock, including the prejoin interval.
                Path("/tmp/cap_session_start").write_text(str(evidence_started))
                remaining = session_deadline - time.monotonic()
                if remaining <= 0:
                    raise AdmissionRefused(
                        "Original meeting deadline expired", "google_admission_timeout"
                    )
                recording_deadline = session_deadline
                event(archive.root, "admission_confirmed", recording_started=started)
                # Record immediately after admission, before camera setup delays.
                audio = monitor_audio(children, out, str(out) + ".ffmpeg.log")
                if audio is not None:
                    event(archive.root, "primary_capture_started", file=out.name)
                    print("RECORDING to", out, flush=True)
                else:
                    print("RECORDING independent archive; convenience WAV unavailable", flush=True)
                recording = True
                Path(str(out) + ".json").write_text(
                    json.dumps(
                        {
                            "out": str(out),
                            "started": time.strftime(
                                "%Y-%m-%dT%H:%M:%S%z", time.localtime(evidence_started)
                            ),
                            "admitted_at": started,
                            "join_mode": join_mode,
                            "meeting": public_url(a.meeting),
                            "name": a.name
                            if get("browser", "google_private") and join_mode == "automatic"
                            else None,
                            "requested_name": a.name,
                            "identity_source": "guest"
                            if get("browser", "google_private")
                            else "profile",
                            "provider": "google",
                        }
                    )
                )
                streamer = optional_feature(
                    lambda: children.start(
                        worker_command("stream", out.stem, "--no-video"), "/tmp/capstream.log"
                    ),
                    "caption transport",
                )
                optional_feature(
                    lambda: children.start(
                        [
                            "ffmpeg",
                            "-nostdin",
                            "-loglevel",
                            "error",
                            "-f",
                            "x11grab",
                            "-framerate",
                            "5",
                            "-i",
                            os.environ["DISPLAY"],
                            "-vf",
                            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                            "-c:v",
                            "libx264",
                            "-preset",
                            "ultrafast",
                            "-crf",
                            "30",
                            "-pix_fmt",
                            "yuv420p",
                            "-n",
                            str(out.with_suffix("")) + "-screen.mp4",
                        ],
                        str(out) + ".screen.log",
                    ),
                    "convenience screen video",
                )
                voice = (
                    optional_feature(
                        lambda: VoiceController(pg, out.stem, a.name, provider="google"),
                        "voice controls",
                    )
                    if media_hooks
                    else None
                )
                from live_transcript.providers.rescue import UIRescue

                ui_rescue = UIRescue(archive.root, "google")
                missing_since = None
                next_stats = 0
                last_audio_bytes = 0
                next_camera_attempt = 0
                next_streamer_attempt = 0
                next_renderer_attempt = 0
                next_audio_attempt = 0
                renderer_started = time.monotonic()
                while time.monotonic() < recording_deadline:
                    if pg.is_closed():
                        raise RuntimeError("Meeting tab closed unexpectedly")
                    now = time.monotonic()
                    optional_feature(lambda: ui_rescue.poll(pg, joined(pg)), "UI recovery")
                    if not joined(pg):
                        missing_since = missing_since or now
                        if now - missing_since > 45:
                            if optional_feature(
                                lambda: meeting_ended(pg), "meeting end notice", False
                            ):
                                print(
                                    "Meeting ended (explicit UI notice); finalizing recording",
                                    flush=True,
                                )
                                break
                            print(
                                "Meeting controls unavailable; archival capture continues until an explicit end or deadline",
                                flush=True,
                            )
                            missing_since = now
                    else:
                        missing_since = None
                        click_if_visible(pg, "Turn off microphone")
                        click_if_visible(pg, "Tap to unblock audio playback")
                        if now >= next_camera_attempt:
                            click_if_visible(pg, "Turn on camera")
                            next_camera_attempt = now + 15
                    if now >= next_audio_attempt and (audio is None or audio.poll() is not None):
                        # The full-quality lossless archive remains independent.
                        # Never abandon the call because the convenience WAV died.
                        print(
                            "WARNING: convenience WAV stopped; independent archive continues",
                            flush=True,
                        )
                        fallback = archive.root / f"fallback-{time.time_ns()}.wav"
                        audio = monitor_audio(children, fallback, str(out) + ".ffmpeg.log")
                        next_audio_attempt = now + 10
                        if audio is not None:
                            event(archive.root, "primary_capture_started", file=fallback.name)
                    hb = Path("/tmp/caprender.hb")
                    stale = now - renderer_started > 20 and (
                        not hb.exists() or time.time() - hb.stat().st_mtime > 20
                    )
                    if now >= next_renderer_attempt and (
                        renderer is None or renderer.poll() is not None or stale
                    ):
                        # Release Chrome's camera reader before restarting a stalled
                        # writer, so the writer wins the V4L2 format negotiation.
                        click_if_visible(pg, "Turn off camera")
                        if renderer is not None:
                            children.stop(renderer)
                        renderer = optional_feature(
                            lambda: children.start(
                                worker_command("render"), "/tmp/meet-render.log"
                            ),
                            "caption camera",
                        )
                        renderer_started = now
                        next_renderer_attempt = now + 10
                        next_camera_attempt = now + 5
                    if now >= next_streamer_attempt and (
                        streamer is None or streamer.poll() is not None
                    ):
                        print(
                            "Caption streamer stopped; recording continues while captions restart",
                            flush=True,
                        )
                        if streamer is not None:
                            children.stop(streamer)
                        streamer = optional_feature(
                            lambda: children.start(
                                worker_command("stream", out.stem, "--no-video"),
                                "/tmp/capstream.log",
                            ),
                            "caption transport",
                        )
                        next_streamer_attempt = now + 10
                    who = optional_feature(lambda: pg.evaluate(SPEAKERS_JS), "live speakers", [])

                    def save_speakers():
                        with open(VM_HOME / "rec" / "speakers.jsonl", "a") as fh:
                            fh.write(json.dumps({"t": round(now - t0, 2), "who": who}) + "\n")

                    optional_feature(save_speakers, "live speaker log")
                    if voice is not None:
                        optional_feature(voice.poll, "voice queue")
                    if now >= next_stats:
                        if not archive.check():
                            print(
                                "WARNING: independent archive unhealthy; inspect archive health/events",
                                flush=True,
                            )
                        stats = optional_feature(
                            lambda: pg.evaluate(STATS_JS),
                            "transport stats",
                            {
                                "available": False,
                                "audioBytes": last_audio_bytes,
                                "states": ["unknown"],
                            },
                        )
                        stats["t"] = round(now - t0, 2)
                        stats["camera"] = optional_feature(
                            lambda: "on" if visible(button(pg, "Turn off camera")) else "off",
                            "camera controls",
                            "unknown",
                        )
                        stats["audioBytesDelta"] = max(0, stats["audioBytes"] - last_audio_bytes)
                        last_audio_bytes = stats["audioBytes"]
                        with open(str(out) + ".transport.jsonl", "a") as fh:
                            fh.write(json.dumps(stats) + "\n")
                        print("MEET HEALTH", json.dumps(stats), flush=True)
                        # Zero traffic is reported, never equated with a broken
                        # connection: other participants may simply be muted.
                        next_stats = now + 15
                    pg.wait_for_timeout(500)
            except AdmissionRefused as refusal:
                print(f"ADMISSION_REFUSED {refusal}", flush=True)
                if ctx and ctx.pages:
                    from live_transcript.providers.admission import save_failure

                    save_failure(
                        ctx.pages[-1],
                        out.with_suffix(".admission"),
                        refusal.reason,
                        a.name,
                        provider="google",
                    )
                raise SystemExit(4)  # a host rejection is terminal, never an auto-rejoin loop
            except Exception:
                if ctx and ctx.pages:
                    try:
                        ctx.pages[-1].screenshot(path=str(out.parent / "meet-failure.png"))
                    except Exception:
                        pass
                raise
            finally:
                children.close()
                if archive:
                    import shutil

                    if out.with_suffix(".admission").is_dir():
                        optional_feature(
                            lambda: shutil.copytree(
                                out.with_suffix(".admission"),
                                archive.root / "admission",
                                dirs_exist_ok=True,
                            ),
                            "admission evidence copy",
                        )
                    try:
                        archive.stop()
                    except Exception as exc:
                        print("WARNING: archive finalization:", exc, flush=True)
                if recording:
                    print("DONE", out, flush=True)
    finally:
        children.close()


if __name__ == "__main__":
    run()
