#!/usr/bin/env python3
"""Zoom meeting adapter: joins a meeting via the web client (headful Chromium under Xvfb,
records the decoded meeting audio independently of the live-caption pipeline.
Usage: live-transcript start <meeting-url-or-id> [--name NAME] [--out FILE] [--max-min N] [--dry]"""

from live_transcript.worker import command as worker_command

import argparse, json, os, re, signal, subprocess, sys, time, uuid
from datetime import datetime
from live_transcript.core.options import zoom_invitation, validate_output
from live_transcript.core.config import get
from pathlib import Path

VM_HOME = get("vm", "home")


def meeting_url(arg):
    return zoom_invitation(arg)[0]


def admission_refused(page):
    pattern = re.compile(
        r"(?:host.*denied|(?:entry|admission|request).*denied|meeting is locked|"
        r"automated bots (?:are not|aren[\x27\u2019]t) allowed to join this meeting)",
        re.I,
    )
    for frame in page.frames:
        try:
            if any(
                pattern.search(text) for text in frame.get_by_role("dialog").all_text_contents()
            ):
                return True
            # Provider bot-policy denial is sometimes plain prejoin text rather
            # than a dialog. Never treat meeting chat as an admission error.
            if not joined_controls(page):
                text = frame.locator("body").inner_text(timeout=1000)
                if pattern.search(text):
                    return True
        except Exception:
            pass
    return False


def joined_controls(page):
    """Require visible in-call controls; pre-join text is never admission."""
    leave, participants = False, False
    for frame in page.frames:
        try:
            controls = frame.evaluate("""() => [...document.querySelectorAll('button,[role="button"],li[role="menuitem"]')]
             .filter(e=>e.getClientRects().length).map(e=>[(e.getAttribute('aria-label')||''),
             (e.getAttribute('title')||''),(e.textContent||'')].join(' ').trim())""")
            leave = leave or any(
                re.search(r"\bleave(?: this)?(?: meeting)?\b", item, re.I) for item in controls
            )
            participants = participants or any(
                re.search(r"\bparticipants\b", item, re.I) for item in controls
            )
        except Exception:
            continue
    return leave and participants


def host_cmd_raw(msg: bytes, timeout=40) -> bytes:
    import socket as _s

    try:
        s = _s.create_connection((get("caption", "host"), get("caption", "port")), timeout=timeout)
        s.sendall(msg + b"\n")
        s.shutdown(_s.SHUT_WR)
        d = b""
        while True:
            b = s.recv(65536)
            if not b:
                break
            d += b
        s.close()
        return d
    except Exception:
        return b""


def host_cmd(msg: bytes) -> str:
    return host_cmd_raw(msg, timeout=25).decode(errors="replace").strip()


def chat_reply(pg, text):
    """Type a reply into the Zoom chat input (best effort across frames)."""
    SELS = (
        'textarea[aria-label*="Message"]',
        'input[aria-label*="Message"]',
        'input[placeholder*="Type a message"]',
        'div[contenteditable="true"]',
    )
    for fr in pg.frames:
        for sel in SELS:
            try:
                el = fr.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    pg.keyboard.type(text[:500], delay=5)
                    pg.keyboard.press("Enter")
                    return True
            except Exception:
                continue
    return False


def main(name_retry_allowed=False, absolute_deadline=None):
    from live_transcript.providers.proton import is_proton_url, run as run_proton

    if len(sys.argv) > 1 and is_proton_url(sys.argv[1]):
        return run_proton(sys.argv[1:])
    from live_transcript.providers.google_meet import is_google_url, run as run_google

    if len(sys.argv) > 1 and is_google_url(sys.argv[1]):
        return run_google(sys.argv[1:])
    ap = argparse.ArgumentParser()
    ap.add_argument("meeting")
    ap.add_argument("--name", default=get("identity", "name"))
    ap.add_argument("--passcode", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-min", type=int, default=180)
    ap.add_argument("--dry", action="store_true", help="stop at first page, print text")
    a = ap.parse_args()
    if not 1 <= a.max_min <= 480 or not a.name.strip() or len(a.name) > 64:
        ap.error("name must contain 1–64 characters; duration must be 1–480 minutes")
    from playwright.sync_api import sync_playwright
    from live_transcript.speech.browser import MIC_SCRIPT
    from live_transcript.speech.controller import VoiceController

    os.umask(0o077)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    global DIGITS
    _, DIGITS = zoom_invitation(a.meeting)
    out = a.out or os.path.expanduser(f"{VM_HOME}/rec/{ts}-{uuid.uuid4().hex[:6]}.wav")
    try:
        out = str(validate_output(out))
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    os.makedirs(os.path.dirname(out), exist_ok=True)

    PROCS = []
    archive = None

    def stop_rec():
        nonlocal archive
        for proc in PROCS:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except Exception:
                    proc.kill()
        if archive:
            try:
                archive.stop()
            except Exception as exc:
                print("Archive finalization warning:", exc, flush=True)
            archive = None

    import atexit

    _stopping = []

    def _hard_stop(*_):
        if not _stopping:
            _stopping.append(1)
            stop_rec()

    atexit.register(_hard_stop)  # exceptions + normal exit
    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # SIGHUP: ssh wrapper died
        try:
            signal.signal(_sig, lambda *x: (_hard_stop(), sys.exit(0)))
        except (ValueError, OSError):
            pass

    with sync_playwright() as p:
        if not os.environ.get("DISPLAY"):
            print(
                "FATAL: no DISPLAY — xvfb-run wrapper missing, would run silent-headless",
                flush=True,
            )
            sys.exit(6)
        ctx = p.chromium.launch_persistent_context(
            (VM_HOME + "/chrome-prof"),
            env=dict(os.environ, PULSE_SINK=get("audio", "sink")),
            headless=False,
            ignore_default_args=["--mute-audio"],
            args=[
                "--no-sandbox",
                "--hide-crash-restore-bubble",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=PreconnectToRemoteAddress",
                "--use-fake-ui-for-media-stream",
                "--disable-blink-features=AutomationControlled",
                # live-inspection port: a supervising agent can attach via CDP
                # (http://<vm-ip>:9222) mid-meeting to see the page and drive it (takeover/repair)
                "--remote-debugging-port=9222",
                "--remote-debugging-address=127.0.0.1",
                "--remote-allow-origins=*",
            ],
            user_agent=None,
            viewport={"width": 1280, "height": 800},
        )
        br = ctx  # persistent ctx: close() lives on ctx
        for _org in ("https://zoom.us", "https://us06web.zoom.us", "https://app.zoom.us"):
            try:
                ctx.grant_permissions(["camera", "microphone"], origin=_org)
            except Exception:
                pass
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        ctx.add_init_script(MIC_SCRIPT)
        ctx.add_init_script("""
            (function(){ // stop zoom's invisible iframe from triggering the external-app dialog
              const oc = HTMLIFrameElement.prototype.appendChild;
              const ox = window.open;
              window.open = function(u){ try{ if(typeof u==='string' && !/^https?:/.test(u)) return null; }catch(e){} return ox.apply(this, arguments); };
            })();""")
        pg = ctx.new_page()
        pg.goto(meeting_url(a.meeting), wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(3500)
        try:
            pg.screenshot(path=(VM_HOME + "/rec/stage-landing.png"))
        except Exception:
            pass
        if a.dry:
            print("DRY: inspected initial page; no join buttons clicked", flush=True)
            br.close()
            return

        def click_any(sels, t=4000):
            for sel in sels:
                try:
                    for control in pg.locator(sel).all():
                        if control.is_visible() and control.is_enabled():
                            control.click(timeout=min(t, 2000))
                            return True
                except Exception:
                    pass
            return False

        def dismiss_media_modals(n=3):
            # legacy no-op: clicking "continue without mic/cam" skips the audio channel
            # entirely and kills transcription. Audio connection is handled by
            # ensure_audio() in the join loop. Kept as a stub so existing call sites are safe.
            return False

        def ftext():
            parts = []
            for f in pg.frames:
                try:
                    parts.append(f.inner_text("body", timeout=2000))
                except Exception:
                    pass
            return " ".join(parts).lower()

        def js_click_aria(frag):
            """Zoom's canvas-client toolbar buttons have NO text content — only
            aria-labels. The text-based js_click silently no-ops on them (a previous video watchdog reported clicks while video stayed off)."""
            for fr in pg.frames:
                try:
                    if fr.evaluate(
                        """(frag) => {
                        for (const b of document.querySelectorAll('button[aria-label]')) {
                            const l = (b.getAttribute('aria-label') || '').toLowerCase();
                            if (l.includes(frag)) { b.click(); return true }
                        }
                        return false }""",
                        frag,
                    ):
                        return True
                except Exception:
                    pass
            return False

        def js_click(text):
            for f in pg.frames:
                try:
                    ok = f.evaluate(
                        """(txt) => {
                        const vis=x=>x.offsetParent!==null;
                        const txtis=x=>((x.textContent||x.value||"").trim().toLowerCase()===txt);
                        const b=[...document.querySelectorAll("button,input[type=submit]")]
                            .find(x=>vis(x)&&txtis(x));
                        if(b){ b.click(); return true; }
                        const a=[...document.querySelectorAll("a")].find(x=>vis(x)&&txtis(x));
                        if(a){ a.click(); return true; } return false; }""",
                        text,
                    )
                    if ok:
                        return True
                except Exception:
                    pass
            return False

        dismiss_media_modals()

        def plain_form_fill():
            if "personal link name" not in ftext():
                return False
            print("plain join form detected — entering ID", flush=True)
            SEL = 'input[placeholder*="eeting"], input[name*="confno" i], input[id*="confno" i]'
            filled = False
            for f in pg.frames:
                try:
                    for el in f.query_selector_all(SEL):
                        if el.is_visible():
                            el.click(timeout=4000)
                            el.fill(DIGITS, timeout=4000)
                            pg.wait_for_timeout(500)
                            el.press("Enter")
                            filled = True
                            break
                except Exception:
                    pass
                if filled:
                    break
            if not filled:
                print("plain fill failed — trying positional", flush=True)
                for f in pg.frames:
                    try:
                        for el in f.query_selector_all("input:not([type=hidden])"):
                            if el.is_visible() and not el.input_value():
                                el.fill(DIGITS, timeout=4000)
                                filled = True
                                break
                    except Exception:
                        pass
                    if filled:
                        break
                    try:
                        f.locator(SEL).first.press_sequentially(DIGITS, delay=30)
                        f.locator(SEL).first.press("Enter")
                        filled = True
                    except Exception:
                        pass
                    if filled:
                        break
            pg.wait_for_timeout(5000)
            if "personal link name" in ftext() and js_click("join"):
                print("js-clicked Join", flush=True)
            pg.wait_for_timeout(4000)
            # app-picker interstitial: dismiss xdg-open dialog, click Join from browser (retry; may be disabled briefly)
            for _ in range(6):
                pg.keyboard.press("Escape")
                pg.wait_for_timeout(500)
                if click_any_frames(('button:has-text("Join from browser")',)):
                    print("clicked Join from browser", flush=True)
                    break
                js_click("join from browser")
                pg.wait_for_timeout(3000)
            pg.wait_for_timeout(6000)
            # pre-join screen (web client loaded, asking mic/cam): don't re-click Join-from-browser
            try:
                if "app.zoom.us" in pg.url:
                    print("web client loaded (pre-join)", flush=True)
                    return True
            except Exception:
                pass
            return True

        # plain "Join Meeting" form fallback: type the ID in and submit
        plain_form_fill()

        # NB: never click "Continue without microphone and camera" here — it joins
        # video-only and Zoom then never subscribes to the room audio (no transcription).
        pg.wait_for_timeout(1500)

        # cookie consent gate must be dismissed before anything is clickable
        click_any(
            (
                'button:has-text("ACCEPT COOKIES")',
                'button:has-text("Accept All Cookies")',
                'text="DECLINE COOKIES"',
                'text="Accept All"',
            )
        )
        pg.wait_for_timeout(2000)
        # /s/ personal-room landing: choose attendee path
        click_any(
            ('text="Join Meeting as an Attendee"', 'a:has-text("Join Meeting as an Attendee")')
        )
        pg.wait_for_timeout(2500)
        # cookie layer reappears on the /j/ join page and overlays the buttons
        click_any(
            (
                'button:has-text("Accept All Cookies")',
                'button:has-text("ACCEPT COOKIES")',
                'text="DECLINE COOKIES"',
            )
        )
        pg.wait_for_timeout(1500)
        body = pg.inner_text("body")[:600]

        # interstitial: prefer browser client if offered (real buttons, not anchors)
        clicked_jfb = click_any(
            (
                'button:has-text("Join from browser")',
                'text="Join from browser"',
                'a:has-text("Join from your browser")',
                'text="Join from your browser"',
            )
        )
        pg.wait_for_timeout(3000)
        if clicked_jfb and "enter meeting info" not in ftext():
            # Zoom often ignores the first click until the app-launch attempt fails
            click_any(('button:has-text("Join from browser")', 'text="Join from browser"'))
            pg.wait_for_timeout(4000)

        # Fill join form deterministically: visible empty text inputs in DOM order.
        # Zoom pre-join form renders Passcode above Name.
        vis_inputs = []
        for f in pg.frames:
            try:
                for el in f.query_selector_all(
                    "input:not([type=hidden]):not([type=checkbox]):not([type=radio])"
                ):
                    try:
                        if el.is_visible() and not (el.input_value() or ""):
                            vis_inputs.append(el)
                    except Exception:
                        pass
            except Exception:
                pass
        if not vis_inputs:
            plain_form_fill()
            for f in pg.frames:
                try:
                    for el in f.query_selector_all("input[type=text]"):
                        try:
                            if el.is_visible() and not (el.input_value() or ""):
                                vis_inputs.append(el)
                        except Exception:
                            pass
                except Exception:
                    pass
        try:
            if a.passcode and len(vis_inputs) >= 2:
                vis_inputs[0].fill(a.passcode, timeout=4000)
                vis_inputs[1].fill(a.name, timeout=4000)
                print("filled passcode+name (positional)", flush=True)
            elif a.passcode and len(vis_inputs) == 1:
                vis_inputs[0].fill(a.name, timeout=4000)  # single field = name (no passcode gate)
                print("filled name only (1 field)", flush=True)
            elif vis_inputs:
                vis_inputs[-1].fill(a.name, timeout=4000)
                print("filled name (no passcode requested)", flush=True)
            else:
                print("NO VISIBLE INPUTS FOUND", flush=True)
        except Exception as e:
            print("fill failed:", e.__class__.__name__, flush=True)
        dismiss_media_modals(2)
        # field diagnostic
        for f in pg.frames:
            try:
                vals = f.evaluate("""() => [...document.querySelectorAll("input")].map(i =>
                    ({ph: i.placeholder, name: i.name, type: i.type,
                      val: (i.type==="password"||i.value.length>20)?"set("+i.value.length+")":i.value,
                      vis: !!(i.offsetWidth||i.offsetHeight)}))""")
                if vals:
                    pc = getattr(a, "passcode", None)  # never echo the passcode to logs
                    if pc:
                        vals = [
                            {**v, "val": "set(pass)"} if v.get("val") == pc else v for v in vals
                        ]
                    print("INPUTS frame:", vals, flush=True)
            except Exception:
                pass
        click_any(('button:has-text("Join")', "input[type=submit]", 'text="Join"'))
        pg.wait_for_timeout(6000)
        try:
            pg.screenshot(path=(VM_HOME + "/rec/stage-join.png"))
        except Exception:
            pass
        body = pg.inner_text("body")[:800]
        if "invalid" in body.lower():
            print("REJECTED BY ZOOM:", body.replace("\n", " | ")[:300])
            br.close()
            sys.exit(2)

        # audio connect dialog (may live inside the web-client iframe)
        click_any(
            (
                'text="Join with Computer Audio"',
                'button:has-text("Join with Computer Audio")',
                'text="Join audio"',
            )
        )

        def all_text():
            parts = []
            for f in pg.frames:
                try:
                    parts.append(f.inner_text("body", timeout=2000)[:3000])
                except Exception:
                    pass
            return (" " + " ".join(parts)).lower()

        def click_any_frames(sels, t=2000):
            for f in pg.frames:
                for s in sels:
                    try:
                        for el in f.query_selector_all(s):
                            if el.is_visible():
                                el.click(timeout=t)
                                return True
                    except Exception:
                        pass
            return False

        # CONFIRM we are actually in the meeting before recording anything
        joined = False
        audio_connected = False

        JS_CA = """() => {
            let hit=null;
            function walk(root){for (const e of root.querySelectorAll("button,[role=button]")) {
                if (!e.offsetParent) continue;
                const t=(((e.getAttribute&&e.getAttribute("aria-label"))||"")+" "+(e.textContent||"")).toLowerCase();
                if (t.includes("computer audio") && !t.includes("controls")) { e.click(); hit=t.trim().slice(0,40); return true; }
                if (e.shadowRoot && walk(e.shadowRoot)) return true; } return false; }
            walk(document); return hit; }"""

        def shadow_click_ca():
            for f in pg.frames:
                try:
                    r = f.evaluate(JS_CA)
                    if r:
                        return r
                except Exception:
                    pass
            return None

        def audio_live():
            # a mute/unmute-microphone toolbar button exists only when audio is connected
            for f in pg.frames:
                try:
                    r = f.evaluate("""() => [...document.querySelectorAll("button[aria-label]")]
                        .some(e=>e.offsetParent && /^(un)?mute(?:\\s|$)/i.test(e.getAttribute("aria-label")||e.textContent||""))""")
                    if r:
                        return True
                except Exception:
                    pass
            return False

        def try_join_audio():
            if click_any_frames(
                ('text="Join with Computer Audio"', 'button:has-text("Join with Computer Audio")'),
                t=1500,
            ):
                return "selector"
            r = shadow_click_ca()
            if r:
                return f"shadow:{r}"
            # last resort: real mouse click on the toolbar mic button opens the audio
            # panel, then retry the computer-audio button (zoom wants pointer events)
            for f in pg.frames:
                try:
                    box = f.evaluate("""() => { const e=[...document.querySelectorAll("button[aria-label]")]
                        .find(x=>x.offsetParent && /^audio$/i.test((x.getAttribute("aria-label")||"").trim()));
                        if(!e) return null; const r=e.getBoundingClientRect();
                        return {x:r.x+r.width/2, y:r.y+r.height/2}; }""")
                    if box:
                        pg.mouse.click(box["x"], box["y"])
                        pg.wait_for_timeout(2500)
                        r = shadow_click_ca()
                        if r:
                            return f"mouse+shadow:{r}"
                except Exception:
                    pass
            return None

        admission_deadline = time.monotonic() + 600
        if absolute_deadline is not None:
            admission_deadline = min(
                admission_deadline, time.monotonic() + max(0, absolute_deadline - time.time())
            )
        i = 0
        while time.monotonic() < admission_deadline:
            i += 1
            pg.wait_for_timeout(2000)
            from live_transcript.providers.admission import (
                inspect_zoom,
                save_failure,
                AdmissionFailure,
            )

            reason = inspect_zoom(pg, joined=joined_controls(pg))
            if reason or admission_refused(pg):
                save_failure(
                    pg,
                    str(Path(out).with_suffix(".admission")),
                    reason or "host_denied",
                    a.name,
                    automatic_retry=name_retry_allowed and reason == "zoom_automation_blocked",
                )
                try:
                    br.close()
                finally:
                    raise AdmissionFailure(reason or "host_denied")
            # PRIORITY 1: connect computer audio. Without it the web client subscribes
            # to NO audio at all and transcription has nothing to chew on.
            if not audio_connected:
                how = try_join_audio()
                if how:
                    print(f"clicked Join with Computer Audio ({how})", flush=True)
                    pg.wait_for_timeout(3000)
                if audio_live():
                    audio_connected = True
                    print("AUDIO CONNECTED (mic control present)", flush=True)
            t = all_text()
            if "join with computer audio" in t and not audio_connected:
                continue
            if "invalid (3,001)" in t or "back skip to main content" in t:
                # Calendar joins start before the host. A not-yet-live room is
                # different from explicit rejection and must get its admission grace.
                print(
                    "Zoom room not live yet; waiting for host within admission window", flush=True
                )
                pg.wait_for_timeout(10000)
                pg.goto(meeting_url(a.meeting), wait_until="domcontentloaded", timeout=45000)
                continue
            if "download the zoom app" in t or "open the zoom app" in t:
                # app-download detour: force the browser client again
                if not click_any_frames(('button:has-text("Join from browser")',)):
                    js_click("join from browser")
                pg.wait_for_timeout(4000)
                continue
            if "personal link name" in t:
                # bounced to plain join form mid-run — re-enter ID
                plain_form_fill()
                continue
            if "not a robot" in t or "your browser may be automated" in t:
                save_failure(
                    pg, str(Path(out).with_suffix(".admission")), "captcha_required", a.name
                )
                print(
                    "CAPTCHA requires human attention; no automated challenge attempts", flush=True
                )
                try:
                    pg.screenshot(path=(VM_HOME + "/rec/fail-captcha.png"))
                except Exception:
                    pass
                br.close()
                sys.exit(4)
            if "your name" in t and "join meeting" in t:
                js_click("join")
                pg.wait_for_timeout(5000)
                continue
            if joined_controls(pg):
                if "join with computer audio" in t and not audio_connected:
                    if not click_any_frames(
                        (
                            'text="Join with Computer Audio"',
                            'button:has-text("Join with Computer Audio")',
                        )
                    ):
                        click_any(('text="Join with Computer Audio"',), t=2000)
                    continue
                joined = True
                break
            if i == 30:
                print(f"still waiting; frames={len(pg.frames)} text_head={t[:200]}", flush=True)
        if not joined:
            print("NOT IN MEETING after admission window — no recording started", flush=True)
            print("PAGE:", pg.inner_text("body")[:400].replace("\n", " | "), flush=True)
            try:
                pg.screenshot(path=(VM_HOME + "/rec/fail.png"))
            except Exception:
                pass
            br.close()
            sys.exit(3)

        from live_transcript.capture.guard import Archive, event

        print("ADMISSION_SUCCEEDED " + json.dumps(dict(name=a.name)), flush=True)
        archive = Archive(out)
        archive_started = time.time()
        budget = (
            a.max_min * 60
            if absolute_deadline is None
            else max(1, absolute_deadline - archive_started)
        )
        archive.start(archive_started, budget, provider="zoom")
        print("ARCHIVE to", archive.root, flush=True)
        ff = subprocess.Popen(
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
                out,
            ]
        )
        PROCS.append(ff)
        event(archive.root, "primary_capture_started", file=os.path.basename(out))
        print("RECORDING to", out, flush=True)
        open("/tmp/cap_session_start", "w").write(str(archive_started))
        voice = VoiceController(pg, os.path.basename(out)[:-4], a.name, provider="zoom")

        # --- get the caption camera up IMMEDIATELY (video used to wait for the
        #     audio phase + recording start → users saw "bot joined but no video")
        def video_state():
            for fr in pg.frames:
                try:
                    a = fr.evaluate(
                        "() => [...document.querySelectorAll('button[aria-label]')]"
                        ".map(e=>e.getAttribute('aria-label'))"
                        ".find(a=>/^(start|stop) my video/i.test(a||''))"
                    )
                    if a:
                        return "on" if "stop" in a.lower() else "off"
                except Exception:
                    pass
            return None

        video_on = False
        # start the live-caption camera supervisor FIRST: its renderer must win
        # /dev/video0 BEFORE Chrome opens the camera as reader — if Chrome wins
        # the race, the writer's format call fails forever ("Invalid argument")
        # and the tile stays blank/frozen for the whole meeting.
        cs = None
        try:
            cs = subprocess.Popen(
                worker_command("stream", os.path.basename(out)[:-4]),
                stdout=open("/tmp/capstream.log", "a"),
                stderr=subprocess.STDOUT,
            )
            PROCS.append(cs)
            print("CAPTION CAMERA streaming (pre-armed before video)", flush=True)
            pg.wait_for_timeout(2500)  # let the renderer grab /dev/video0
        except Exception as e:
            print("caption stream skipped:", e, flush=True)
        for _vi in range(12):
            vs_early = video_state()
            if vs_early == "on":
                video_on = True
                print("VIDEO ON (caption camera, confirmed, early)", flush=True)
                break
            if vs_early == "off":
                (
                    click_any_frames(
                        (
                            'button[aria-label*="start my video" i]',
                            'button[aria-label*="start" i][aria-label*="video" i]',
                        )
                    )
                    or js_click("start my video")
                )
            elif _vi == 11:
                # aria scan found nothing (canvas-rendered toolbar) — coordinate fallback
                print("VIDEO: button not in DOM, canvas coordinate fallback", flush=True)
                try:
                    pg.mouse.click(45, 775)
                except Exception:
                    pass
            pg.wait_for_timeout(1500)

        # NOTE: caption_stream/renderer was pre-armed BEFORE the video enable
        # (device-race rule above) — do NOT start a second instance here.
        # screen video track: the Xvfb display shows the live web client incl. active-speaker view
        ffv = None
        try:
            disp = os.environ.get("DISPLAY", "")
            if disp:
                ffv = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-loglevel",
                        "error",
                        "-f",
                        "x11grab",
                        "-framerate",
                        "5",
                        "-video_size",
                        "1280x800",
                        "-i",
                        disp,
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "30",
                        "-pix_fmt",
                        "yuv420p",
                        "-y",
                        os.path.splitext(out)[0] + "-screen.mp4",
                    ]
                )
                PROCS.append(ffv)
                print(f"VIDEO to {out.replace('.wav', '-screen.mp4')}", flush=True)
        except Exception as e:
            print("video capture skipped:", e, flush=True)
        print(f"RECORDING to {out}", flush=True)
        main._rec_t0 = archive_started
        from live_transcript.providers.proton import public_url

        meta = {
            "out": out,
            "started": datetime.now().isoformat(),
            "meeting": public_url(a.meeting),
            "name": a.name,
            "provider": "zoom",
        }
        open(out + ".json", "w").write(json.dumps(meta))

        deadline = (
            absolute_deadline if absolute_deadline is not None else time.time() + a.max_min * 60
        )
        # (video_on already initialised early)
        chat_open = False
        seen_cmds = set()
        stop_req = False
        voice_mode = False  # wake-word answering (opt-in via !voice on)
        voice_lang = "de"  # TTS voice language (!voice de|en)
        last_speak = 0.0
        last_wake_txt = ""
        chat_seen = set()  # chat messages already captured
        rec_id = os.path.basename(out)[:-4]

        def dismiss_modals():
            for fr in pg.frames:
                try:
                    fr.evaluate(
                        "() => { for (const e of document.querySelectorAll('button,[role=button]')) {"
                        " if (!e.offsetParent) continue;"
                        " const t=((e.getAttribute('aria-label')||'')+' '+(e.textContent||'')).trim().toLowerCase();"
                        " if (t==='got it'||t==='cancel'||t.startsWith('got it ')||t.startsWith('not now')) e.click(); } }"
                    )
                except Exception:
                    pass

        if not audio_connected:
            print(
                "WARNING: AUDIO-NOT-CONNECTED after join phase — recording will be SILENT",
                flush=True,
            )
        last_vid_check = time.time()
        from live_transcript.providers.rescue import UIRescue

        ui_rescue = UIRescue(archive.root, "zoom")
        next_caption_restart = 0
        while time.time() < deadline:
            if pg.is_closed():
                raise RuntimeError("Meeting tab closed unexpectedly")
            try:
                voice.poll()
            except Exception as exc:
                print(
                    "Voice queue unavailable; recording continues:", type(exc).__name__, flush=True
                )
            if (cs is None or cs.poll() is not None) and time.time() >= next_caption_restart:
                try:
                    cs = subprocess.Popen(
                        worker_command("stream", rec_id),
                        stdout=open("/tmp/capstream.log", "a"),
                        stderr=subprocess.STDOUT,
                    )
                    PROCS.append(cs)
                except OSError as exc:
                    print("Captions unavailable; archival recording continues:", exc, flush=True)
                next_caption_restart = time.time() + 10
            if ff.poll() is not None:
                fallback = str(archive.root / f"fallback-{time.time_ns()}.wav")
                ff = subprocess.Popen(
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
                        fallback,
                    ]
                )
                PROCS.append(ff)
                event(archive.root, "primary_capture_started", file=os.path.basename(fallback))
            try:
                t = all_text()
            except Exception:
                t = ""  # transient DOM failures must not terminate audio capture
            try:
                ui_rescue.poll(pg, joined_controls(pg))
            except Exception as exc:
                print(
                    "UI recovery unavailable; recording continues:", type(exc).__name__, flush=True
                )
            # zoom throws first-run onboarding modals that swallow every other click
            dismiss_modals()
            # turn on the fake caption camera, CONFIRMED via the button's aria-label
            # (zoom renders to canvas, so body text never shows 'stop my video' — the
            #  old gate false-passed; poll the real button state until it flips)
            if not video_on:
                vs = video_state()
                if vs == "on":
                    video_on = True
                    print("VIDEO ON (caption camera, confirmed)", flush=True)
                elif vs == "off":
                    if (
                        click_any_frames(
                            (
                                'button[aria-label*="start my video" i]',
                                'button[aria-label*="start" i][aria-label*="video" i]',
                                'button[aria-label="Start Video"]',
                            )
                        )
                        or js_click("start my video")
                        or js_click("start video")
                    ):
                        pg.wait_for_timeout(2500)
                        if video_state() == "on":
                            video_on = True
                            print("VIDEO ON (caption camera, confirmed)", flush=True)
            # Zoom silently switches the camera off if the fake feed stalls even
            # briefly (e.g. renderer race at start) — re-arm every 30s. This runs
            # EVEN IF the initial confirm never passed (incident 2026-11-03: join
            # proceeded with video never confirmed and the watchdog never armed).
            if time.time() - last_vid_check > 30:
                last_vid_check = time.time()
                st_now = video_state()
                if st_now == "off":
                    print(
                        (
                            "VIDEO DROPPED — re-enabling"
                            if video_on
                            else "VIDEO NEVER CONFIRMED — enabling now"
                        ),
                        flush=True,
                    )
                    js_click_aria("start my video")
                    click_any_frames(('button[aria-label*="start my video" i]',), t=1200)
                    pg.wait_for_timeout(3500)
                    video_on = video_state() == "on"
                elif st_now == "on":
                    video_on = True
                    if (
                        subprocess.run(
                            ["pgrep", "-f", "rawvideo.*v4l2"], capture_output=True
                        ).returncode
                        != 0
                    ):
                        # camera reads but NO writer holds /dev/video0 (renderer lost
                        # the device race): bounce the camera so the renderer's 2s
                        # retry loop wins the device while Chrome's reader is closed
                        print("CAMERA WRITER LOST — bouncing video to recover device", flush=True)
                        js_click_aria("stop my video")
                        click_any_frames(('button[aria-label*="stop my video" i]',), t=1200)
                        pg.wait_for_timeout(4500)
                        js_click_aria("start my video")
                        click_any_frames(('button[aria-label*="start my video" i]',), t=1200)
                        pg.wait_for_timeout(2500)
            from live_transcript.providers.proton import meeting_ended

            try:
                ended = meeting_ended(pg)
            except Exception:
                ended = False
            if ended:
                print("MEETING ENDED — closing out cleanly", flush=True)
                js_click("ok")
                pg.wait_for_timeout(3000)
                break
            pg.wait_for_timeout(1000)
            # in-meeting chat commands: open chat once, scan for !commands, reply in chat
            try:
                if not chat_open:
                    if click_any_frames(
                        (
                            'button[aria-label="Chat"]',
                            'li[aria-label*="Chat"]',
                            'button:has-text("Chat")',
                        ),
                        t=800,
                    ) or js_click("chat"):
                        chat_open = True
                        print("CHAT PANEL OPEN", flush=True)
                if chat_open:
                    chat_lines = ""
                    for fr in pg.frames:
                        try:
                            chat_lines += (
                                fr.evaluate(
                                    "() => [...document.querySelectorAll('[class*=chatMessageText],"
                                    "[class*=chat-message],[class*=chatItemText],[class*=chat-item]')]"
                                    ".map(e=>e.innerText||'').join('\\n')"
                                )
                                + "\n"
                            )
                        except Exception:
                            pass
                    # capture ALL chat traffic verbatim -> rec/chat.txt (name/time/text blocks)
                    try:
                        blocks = []
                        for fr in pg.frames:
                            try:
                                blocks += fr.evaluate(
                                    "() => [...document.querySelectorAll('[class*=chatMessageItem],"
                                    "[class*=chat-message-container],[class*=chatItemContainer]')]"
                                    ".slice(-50).map(e=>(e.innerText||'').trim()).filter(t=>t.length>1)"
                                )
                            except Exception:
                                pass
                        new = []
                        for b in blocks:
                            norm = " ".join(b.split())
                            if norm and norm not in chat_seen:
                                chat_seen.add(norm)
                                new.append(norm)
                        if len(chat_seen) > 800:
                            chat_seen = set(list(chat_seen)[-400:])
                        if new:
                            with open((VM_HOME + "/rec/chat.txt"), "a") as cf:
                                for nline in new:
                                    cf.write(nline + "\n")
                            with open(archive.root / "chat.txt", "a") as cf:
                                cf.write("\n".join(new) + "\n")
                                cf.flush()
                                os.fsync(cf.fileno())
                    except Exception:
                        pass
                    counts = {}
                    for ln in chat_lines.splitlines():
                        ln = ln.strip()
                        if not ln.startswith("!"):
                            continue
                        counts[ln] = counts.get(ln, 0) + 1
                        if (ln, counts[ln]) in seen_cmds:  # allow re-issuing the same command
                            continue
                        seen_cmds.add((ln, counts[ln]))
                        parts = ln[1:].split(" ", 1)
                        cmd = parts[0].lower()
                        arg = parts[1].strip() if len(parts) > 1 else ""
                        print(f"CHAT CMD {cmd!r} {arg!r}", flush=True)
                        if cmd == "stop":
                            chat_reply(pg, "🤖 stopping the recording now")
                            stop_req = True
                        elif cmd == "help":
                            chat_reply(
                                pg,
                                "🤖 commands: !status !todo !summary !ask <q> !note <text> !say <text> !voice on|off|de|en !stop",
                            )
                        elif cmd == "say":
                            if arg:
                                voice.enqueue(arg[:500], voice_lang)
                                last_speak = time.time()
                                chat_reply(pg, "🤖 speech queued")
                        elif cmd == "ask":
                            chat_reply(
                                pg,
                                "🤖 "
                                + (
                                    host_cmd(f"GETANS {rec_id} {arg[:250]}".encode())
                                    or "no answer (Qwen unreachable?)"
                                ),
                            )
                        elif cmd == "voice":
                            al = arg.lower().strip()
                            if al in ("on", "off"):
                                voice_mode = al == "on"
                                chat_reply(
                                    pg,
                                    f"🤖 voice answering {al} — wake me with 'Live Transcript, …'",
                                )
                            elif al in ("de", "en"):
                                voice_lang = al
                                chat_reply(pg, f"🤖 voice language → {al}")
                            else:
                                chat_reply(pg, "🤖 usage: !voice on|off  or  !voice de|en")
                        elif cmd == "note":
                            with open((VM_HOME + "/rec/notes.txt"), "a") as nf:
                                nf.write(f"[{int(time.time() - main._rec_t0)}s] {arg}\n")
                            with open(archive.root / "notes.txt", "a") as nf:
                                nf.write(f"[{int(time.time() - main._rec_t0)}s] {arg}\n")
                                nf.flush()
                                os.fsync(nf.fileno())
                            chat_reply(pg, "🤖 noted ✓")
                        elif cmd == "todo":
                            chat_reply(
                                pg,
                                "🤖 TODO: "
                                + (host_cmd(f"GETTODO {rec_id}".encode()) or "none yet"),
                            )
                        elif cmd == "summary":
                            chat_reply(
                                pg,
                                "🤖 " + (host_cmd(f"GETSUM {rec_id}".encode()) or "no summary yet"),
                            )
                        elif cmd == "status":
                            try:
                                gpu = json.load(open((VM_HOME + "/gpu.json")))
                            except Exception:
                                gpu = {}
                            el = int(time.time() - main._rec_t0)
                            chat_reply(
                                pg,
                                "🤖 GPU {}% {:.0f}W {:.0f}°C · STT {} w/s · rec {:02d}:{:02d}".format(
                                    gpu.get("util", 0),
                                    gpu.get("watts", 0),
                                    gpu.get("temp", 0),
                                    gpu.get("stt_wps", 0),
                                    el // 60,
                                    el % 60,
                                ),
                            )
                    if stop_req:
                        break
            except Exception:
                pass
            # voice mode: answer aloud when addressed by wake word
            if voice_mode and time.time() - last_speak > 25:
                try:
                    tail = open((VM_HOME + "/captions.tsv")).read().splitlines()[-2:]
                except Exception:
                    tail = []
                for l in tail:
                    tx = l.split("\t", 1)[-1] if "\t" in l else l
                    low = tx.lower()
                    if (
                        any(
                            w in low for w in ("live transcript", "recording bot", "transcript bot")
                        )
                        and tx != last_wake_txt
                    ):
                        last_wake_txt = tx
                        ans = host_cmd(f"GETANS {rec_id} {tx[:250]}".encode())
                        if ans:
                            voice.enqueue(ans[:400], voice_lang)
                            last_speak = time.time()
                        break
            # live speaker tracker: who does Zoom's active-speaker tile show right now
            try:
                sp = pg.evaluate("""() => {
                    const out=[];
                    const act=document.querySelector("[class*=speaker-active-container]");
                    if(act){
                      const lbl=act.querySelector("[class*=name], [class*=label], [title]");
                      out.push({role:"active", name:((lbl&&(lbl.textContent||lbl.title))||act.textContent||"").trim().slice(0,60)});
                    }
                    document.querySelectorAll("[class*=videoView] , [class*=tile]").forEach(t=>{
                      const n=t.querySelector("[class*=name-label],[class*=participant-name]");
                      if(n) out.push({role:"tile", name:n.textContent.trim().slice(0,60), speaking:/active|speaking/.test(t.className)});
                    });
                    return out.slice(0,12); }""")
                with open((VM_HOME + "/rec/speakers.jsonl"), "a") as _f:
                    _f.write(
                        json.dumps(
                            {
                                "t": round(time.time() - getattr(main, "_rec_t0", time.time()), 1),
                                "who": sp,
                            }
                        )
                        + "\n"
                    )
            except Exception:
                pass
        stop_rec()
        print("DONE", out, flush=True)  # BEFORE br.close: a hung browser close must
        try:
            br.close()  # not strand a finished recording
        except Exception:
            pass
    print("DONE", out, flush=True)
    # signal post pipeline


def with_option(args, key, value):
    """Replace either option form without reparsing secrets through a shell."""
    result = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == key:
            index += 2
            continue
        if item.startswith(key + "="):
            index += 1
            continue
        if item in ("--name", "--max-min", "--passcode", "--out"):
            result.extend(args[index : index + 2])
            index += 2
            continue
        result.append(item)
        index += 1
    return [*result, key, value]


def run_with_name_fallbacks():
    from live_transcript.providers.admission import AdmissionFailure
    from live_transcript.core.options import rejoin_options
    from live_transcript.providers.proton import is_proton_url
    from live_transcript.providers.google_meet import is_google_url

    original = list(sys.argv)
    if (
        len(original) < 2
        or "--dry" in original
        or is_proton_url(original[1])
        or is_google_url(original[1])
    ):
        return main()
    options, minutes = rejoin_options(original[2:])
    first = options.get("--name", get("identity", "name"))
    configured = get("admission", "zoom_name_fallbacks")
    if first in configured:
        configured = configured[configured.index(first) + 1 :]
    names = list(dict.fromkeys([first, *configured]))
    deadline = float(os.environ.get("LT_DEADLINE") or time.time() + minutes * 60)
    try:
        for attempt, name in enumerate(names):
            if time.time() >= deadline:
                raise AdmissionFailure("admission_budget_exhausted")
            sys.argv = [original[0], *with_option(original[1:], "--name", name)]
            try:
                return main(name_retry_allowed=attempt + 1 < len(names), absolute_deadline=deadline)
            except AdmissionFailure as exc:
                if exc.reason != "zoom_automation_blocked" or attempt + 1 == len(names):
                    raise
                delay = get("admission", "name_retry_delay_seconds")
                if time.time() + delay >= deadline:
                    raise
                print(
                    "ADMISSION_RETRY "
                    + json.dumps(
                        dict(
                            reason=exc.reason,
                            attempt=attempt + 2,
                            name=names[attempt + 1],
                            remaining_seconds=round(deadline - time.time()),
                        )
                    ),
                    flush=True,
                )
                time.sleep(delay)
    finally:
        sys.argv = original


if __name__ == "__main__":
    run_with_name_fallbacks()
