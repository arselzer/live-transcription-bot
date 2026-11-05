#!/usr/bin/env python3
"""Google Meet adapter for the existing Live Transcript VM and caption daemon.

Selectors rely on Meet's stable aria-labels (Join now, Leave call,
Turn on/off microphone, Turn on/off camera), not on obfuscated class names.
Transport health reuses the generic RTCPeerConnection observer.
Archival capture is independent of live captions and keeps source evidence.
"""
import argparse
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
VM_HOME = Path(get('vm', 'home'))


class AdmissionRefused(RuntimeError):
    pass


def is_google_url(url):
    return urlsplit(url).hostname == "meet.google.com"


def validate_url(url):
    p = urlsplit(url)
    if (p.scheme != "https" or p.netloc != "meet.google.com"
            or not re.fullmatch(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}/?", p.path)):
        raise ValueError("Expected a full https://meet.google.com/xxx-xxxx-xxx meeting link")
    return url


def public_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def visible(locator):
    return locator.first.is_visible()


def button(pg, label):
    return pg.get_by_role("button", name=label, exact=True).first


def click_if_visible(pg, label):
    try:
        b = button(pg, label)
        if b.is_visible() and b.is_enabled():
            b.click(timeout=2500)
            return True
    except Exception:
        return False  # a transient UI action must not end archival capture
    return False


def joined(pg):
    try:
        return visible(button(pg, "Leave call"))
    except Exception:
        return False


def optional_feature(callback, label, default=None):
    try:
        return callback()
    except Exception as exc:
        print(f'{label} unavailable; archival recording continues: {type(exc).__name__}', flush=True)
        return default


def meeting_ended(page):
    """Use explicit UI end notices, never arbitrary participant/chat text."""
    pattern = re.compile(r'(?:call (?:has )?ended|this call has ended|you (?:have )?left (?:the )?call|'
                         r'the host (?:ended|declined)|(?:you were|have been) removed)', re.I)
    frames = getattr(page, 'frames', [page])
    for frame in frames:
        for role in ('dialog', 'heading'):
            try:
                if any(pattern.search(text) for text in frame.get_by_role(role).all_text_contents()):
                    return True
            except Exception:
                pass
    return False


def prejoin(pg, name, timeout=330):
    """Submit once; Meet instant meetings admit guests immediately, while a
    locked meeting needs the host to approve 'Ask to join'."""
    end = time.monotonic() + timeout
    asked = False
    last_status = ""
    while time.monotonic() < end:
        try:
            if joined(pg):
                return
            dialogs = pg.get_by_role("dialog").all_text_contents()
            fatal = re.compile(r"(?:meeting|call) code (?:is )?(?:invalid|doesn.t exist|not found)|"
                               r"this (?:meeting|call) (?:has ended|does(?:n.t)? exist)|"
                               r"(?:unable|failed) to (?:join|connect)|you (?:can.t|cannot) join", re.I)
            if any(fatal.search(t) for t in dialogs):
                raise AdmissionRefused("Google Meet refused the join; inspect meet-failure.png in the VM")
            # Meet renders hard join denials as body text, not dialogs.
            body = pg.evaluate("() => document.body.innerText.slice(0, 4000)")
            if re.search(r"you (?:can.t|cannot) join this (?:video call|meeting)|"
                         r"returning to home screen|no one can join a meeting unless", body, re.I):
                raise AdmissionRefused("Google Meet denies anonymous joins for this room; "
                                       "a signed-in bot profile or open guest access is required")
            field = pg.get_by_role("textbox").first
            if not asked and field.is_visible():
                field.fill(name)
                # The bot listens with its microphone muted, including pre-join.
                click_if_visible(pg, "Turn off microphone")
                if click_if_visible(pg, "Join now"):
                    asked = True
            # A locked meeting queues the guest behind an explicit request.
            click_if_visible(pg, "Ask to join")
            status = "Waiting for Google Meet/host admission" if asked else "Waiting for Google Meet pre-join form"
            if status != last_status:
                print(status, flush=True)
                last_status = status
            pg.wait_for_timeout(1000)
        except Exception as exc:
            if any(message in str(exc) for message in (
                    'Execution context was destroyed', 'Cannot find context', 'Frame was detached')):
                pg.wait_for_timeout(500)
                continue
            raise
    raise TimeoutError("No confirmed admission within 330 seconds; no recording started")


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
  const stats = {audioBytes: 0, audioPackets: 0, audioEnergy: 0, states: []};
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


class Children:
    """Own complete process groups, including renderer/capture descendants."""
    def __init__(self):
        self.procs = []

    def start(self, args, log):
        with open(log, "ab") as fh:
            proc = subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
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
    ap.add_argument("--name", default=get('identity', 'name'))
    ap.add_argument("--out")
    ap.add_argument("--max-min", type=int, default=180)
    ap.add_argument("--dry", action="store_true", help="inspect pre-join without joining or recording")
    ap.add_argument("--passcode", help="unused: Google Meet needs no passcode for room links")
    a = ap.parse_args(argv)
    if not 1 <= a.max_min <= 480 or not a.name.strip() or len(a.name) > 64:
        ap.error("--max-min must be positive and --name must contain 1–64 characters")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("Run under xvfb-run: headless Chromium cannot supply meeting audio")
    from playwright.sync_api import sync_playwright
    from live_transcript.speech.browser import MIC_SCRIPT
    from live_transcript.speech.controller import VoiceController
    from live_transcript.capture.guard import Archive, event

    os.umask(0o077)
    os.environ['CAPTION_LAYOUT'] = 'standard'
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
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    try:
        with sync_playwright() as p:
            try:
                if not a.dry:
                    # Pre-arm only the camera writer, not audio capture, before Chrome
                    # enumerates V4L2. The renderer must win the device format race.
                    Path("/tmp/cap_session_start").write_text(str(int(time.time())))
                    renderer = children.start(["python3", str(VM_HOME / "caption_render.py")],
                                              "/tmp/meet-render.log")
                    time.sleep(3)
                ctx = p.chromium.launch_persistent_context(
                    str(VM_HOME / "chrome-meet-prof"), headless=False,
                    env=dict(os.environ, PULSE_SINK='scribe'),
                    ignore_default_args=["--mute-audio"], locale="en-US",
                    args=["--no-sandbox", "--hide-crash-restore-bubble", "--autoplay-policy=no-user-gesture-required",
                          "--remote-debugging-port=9222", "--remote-debugging-address=127.0.0.1"],
                    viewport={"width": 1280, "height": 800})
                ctx.grant_permissions(["camera", "microphone"], origin=ORIGIN)
                ctx.add_init_script(PEERS_JS)
                ctx.add_init_script(MIC_SCRIPT)
                pg = ctx.new_page()
                pg.on("dialog", lambda dialog: dialog.dismiss())
                pg.goto(a.meeting, wait_until="domcontentloaded", timeout=60000)
                if a.dry:
                    pg.wait_for_timeout(8000)
                    pg.screenshot(path=str(out.parent / "meet-prejoin.png"))
                    labels = pg.evaluate("""() => [...document.querySelectorAll('[role=button],button,[aria-label]')]
                        .map(e => (e.getAttribute('aria-label')||e.textContent||'').trim().slice(0,60)).filter(Boolean).slice(0,80)""")
                    print("DRY BUTTONS:", json.dumps(sorted(set(labels))), flush=True)
                    print("DRY TEXT:", json.dumps(pg.evaluate("() => document.body.innerText.slice(0,1200)")), flush=True)
                    print("DRY: inspected pre-join; did not join or record", flush=True)
                    return
                prejoin(pg, a.name)
                print("JOINED Google Meet (Leave call confirmed)", flush=True)
                t0 = time.monotonic()
                started = time.time()
                (VM_HOME / "rec" / "speakers.jsonl").write_text("")
                Path("/tmp/cap_session_start").write_text(str(started))
                archive = Archive(out)
                archive.start(started, a.max_min * 60, provider='google')
                print('ARCHIVE to', archive.root, flush=True)
                # Record immediately after admission, before camera setup delays.
                audio = children.start(["ffmpeg", "-nostdin", "-loglevel", "error",
                                        "-f", "pulse", "-i", "scribe.monitor", "-ac", "1",
                                        "-ar", "16000", "-n", str(out)], str(out) + ".ffmpeg.log")
                event(archive.root, 'primary_capture_started', file=out.name)
                recording = True
                print("RECORDING to", out, flush=True)
                Path(str(out) + ".json").write_text(json.dumps({
                    "out": str(out), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "meeting": public_url(a.meeting), "name": a.name, "provider": "google"}))
                streamer = children.start(["python3", str(VM_HOME / "caption_stream.py"),
                                           out.stem, "--no-video"], "/tmp/capstream.log")
                children.start(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "x11grab",
                                "-framerate", "5", "-video_size", "1280x800", "-i", os.environ["DISPLAY"],
                                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                                "-pix_fmt", "yuv420p", "-n", str(out.with_suffix("")) + "-screen.mp4"],
                               str(out) + ".screen.log")
                voice = VoiceController(pg, out.stem, a.name)
                from live_transcript.providers.rescue import UIRescue
                ui_rescue = UIRescue(archive.root, 'google')
                missing_since = None
                next_stats = 0
                last_audio_bytes = 0
                next_camera_attempt = 0
                next_streamer_attempt = 0
                renderer_started = time.monotonic()
                while time.monotonic() - t0 < a.max_min * 60:
                    if pg.is_closed():
                        raise RuntimeError('Meeting tab closed unexpectedly')
                    now = time.monotonic()
                    optional_feature(lambda: ui_rescue.poll(pg, joined(pg)), 'UI recovery')
                    if not joined(pg):
                        missing_since = missing_since or now
                        if now - missing_since > 45:
                            if optional_feature(lambda: meeting_ended(pg), 'meeting end notice', False):
                                print('Meeting ended (explicit UI notice); finalizing recording', flush=True)
                                break
                            print('Meeting controls unavailable; archival capture continues until an explicit end or deadline', flush=True)
                            missing_since = now
                    else:
                        missing_since = None
                        click_if_visible(pg, "Turn off microphone")
                        click_if_visible(pg, "Tap to unblock audio playback")
                        if now >= next_camera_attempt:
                            click_if_visible(pg, "Turn on camera")
                            next_camera_attempt = now + 15
                    if audio.poll() is not None:
                        # The full-quality lossless archive remains independent.
                        # Never abandon the call because the convenience WAV died.
                        print('WARNING: convenience WAV stopped; independent archive continues', flush=True)
                        fallback = archive.root / f'fallback-{time.time_ns()}.wav'
                        audio = children.start(['ffmpeg', '-nostdin', '-loglevel', 'error',
                                                '-f', 'pulse', '-i', 'scribe.monitor', '-ac', '1',
                                                '-ar', '16000', '-n',
                                                str(fallback)],
                                               str(out) + '.ffmpeg.log')
                        event(archive.root, 'primary_capture_started', file=fallback.name)
                    hb = Path("/tmp/caprender.hb")
                    stale = now - renderer_started > 20 and (
                        not hb.exists() or time.time() - hb.stat().st_mtime > 20)
                    if renderer.poll() is not None or stale:
                        # Release Chrome's camera reader before restarting a stalled
                        # writer, so the writer wins the V4L2 format negotiation.
                        click_if_visible(pg, "Turn off camera")
                        children.stop(renderer)
                        renderer = children.start(["python3", str(VM_HOME / "caption_render.py")],
                                                  "/tmp/meet-render.log")
                        renderer_started = now
                        next_camera_attempt = now + 5
                    if streamer.poll() is not None and now >= next_streamer_attempt:
                        print('Caption streamer stopped; recording continues while captions restart', flush=True)
                        children.stop(streamer)
                        streamer = children.start(['python3', str(VM_HOME / 'caption_stream.py'),
                                                   out.stem, '--no-video'], '/tmp/capstream.log')
                        next_streamer_attempt = now + 10
                    who = optional_feature(lambda: pg.evaluate(SPEAKERS_JS), 'live speakers', [])
                    with open(VM_HOME / "rec" / "speakers.jsonl", "a") as fh:
                        fh.write(json.dumps({"t": round(now - t0, 2), "who": who}) + "\n")
                    optional_feature(voice.poll, 'voice queue')
                    if now >= next_stats:
                        if not archive.check():
                            print('WARNING: independent archive unhealthy; inspect archive health/events', flush=True)
                        stats = optional_feature(lambda: pg.evaluate(STATS_JS), 'transport stats',
                                                 {'audioBytes': last_audio_bytes, 'states': ['unknown']})
                        stats["t"] = round(now - t0, 2)
                        stats['camera'] = optional_feature(lambda: 'on' if visible(button(pg, 'Turn off camera')) else 'off',
                                                           'camera controls', 'unknown')
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
                    optional_feature(lambda: ctx.pages[-1].screenshot(path=str(out.with_suffix('.denied.png'))), 'admission screenshot')
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
                    try:
                        archive.stop()
                    except Exception as exc:
                        print('WARNING: archive finalization:', exc, flush=True)
                if recording:
                    print("DONE", out, flush=True)
                if ctx:
                    ctx.close()
    finally:
        children.close()


if __name__ == "__main__":
    run()
