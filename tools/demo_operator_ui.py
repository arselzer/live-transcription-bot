"""Render the real workspace against synthetic data; never connect to a VM.

Run with a development Playwright installation and Chromium. The fixture owns
only loopback HTTP servers and a temporary journal. Screenshots/GIF contain no
real meetings, identities, clipboard data, capability URL, or browser profile.
"""

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import tempfile
import threading
import time


LINES = [
    (0, "Facilitator", "Let's review the rollout plan and the remaining checks."),
    (
        12,
        "Participant 1",
        "The audio and video originals are saved independently of live recognition.",
    ),
    (29, "Participant 2", "Can we test recovery before the next rollout?"),
    (43, "Participant 1", "Yes. We'll disconnect recognition and confirm the recording continues."),
    (
        66,
        "Facilitator",
        "The deadline for the recovery check is Friday. Please keep the test results with the recording.",
    ),
    (
        81,
        "Participant 2",
        "I'll run that check and share the results. The full transcript will remain available for review.",
    ),
]


class FixtureBrowser(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"""<!doctype html><html class="noVNC_connected"><meta charset="utf-8">
<style>body{margin:0;background:#141923;color:#e2e7ee;font:16px system-ui;height:100vh;display:grid;place-content:center;text-align:center}p{color:#9faec3;max-width:460px;line-height:1.8}.room{border:1px solid #46536b;border-radius:12px;padding:48px}small{color:#a0b99f}</style>
<div class="room"><small>SYNTHETIC DEMONSTRATION</small><h1>Isolated meeting browser</h1>
<p>This is where the VM desktop appears. Finish joining here when a meeting needs manual admission.</p><p>No live meeting or browser is connected.</p></div></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--chromium", help="optional Chromium executable path")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    from playwright.sync_api import sync_playwright, expect
    import live_transcript.core.config as config
    from live_transcript.runtime.operator import OperatorServer, EventFeed
    from live_transcript.transcription.store import CaptionStore

    original = config.CONFIG
    servers = []
    try:
        with tempfile.TemporaryDirectory(prefix="live-transcript-demo-") as directory:
            root = Path(directory)
            config.CONFIG = deepcopy(config.DEFAULTS)
            config.CONFIG["paths"]["state"] = str(root)
            store = CaptionStore(root / "captions.sqlite3")

            class Bridge:
                recovery = False

                def call(self, request):
                    if request["op"] != "status":
                        return dict(ok=True)
                    return dict(
                        display=":88",
                        control=True,
                        session=dict(id="synthetic-demo", provider="proton"),
                        admission=dict(
                            stage="needs_operator" if self.recovery else "admitted",
                            utc=100 if self.recovery else 1,
                            deadline=time.time() + 90,
                        ),
                        capture=dict(
                            age=0.1,
                            elapsed=1040,
                            free_bytes=93 * 1024**3,
                            **{
                                k: dict(running=True, progress_age=0.1)
                                for k in ("audio", "screen", "snapshots")
                            },
                        ),
                        speech=dict(
                            age=0.1, queued_audio_s=0.8, decode_error=False, signal_flags=[]
                        ),
                    )

            bridge = Bridge()
            browser_server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureBrowser)
            events = EventFeed(root / "events.jsonl")
            events.add("admitted", "Meeting admission confirmed")
            events.add("capture_healthy", "Audio and video capture are running")
            server = OperatorServer(
                ("127.0.0.1", 0), browser_server.server_port, bridge, events, "127.0.0.1"
            )
            servers = [server, browser_server]
            for service in servers:
                threading.Thread(target=service.serve_forever, daemon=True).start()

            def checkpoint(lines, notes=False):
                store.checkpoint(
                    "synthetic-demo",
                    dict(
                        lines=lines,
                        board=dict(
                            kind="action",
                            bullets=[
                                "Run a recognition outage drill before Friday.",
                                "Confirm original audio and video keep recording.",
                                "Keep test results with the recording.",
                            ],
                        )
                        if notes
                        else {},
                    ),
                )

            checkpoint(LINES[:2])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chromium, headless=True)
                page = browser.new_page(
                    viewport=dict(width=1280, height=960), device_scale_factor=1
                )
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://{server.authority}/#token={server.token}")
                expect(page.get_by_text(LINES[1][2], exact=True)).to_be_visible()
                frames = []

                def frame():
                    page.wait_for_timeout(180)
                    frames.append(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))

                frame()
                checkpoint(LINES[:4])
                expect(page.get_by_text(LINES[3][2], exact=True)).to_be_visible(timeout=15000)
                frame()
                checkpoint(LINES, True)
                expect(page.get_by_text(LINES[5][2], exact=True)).to_be_visible(timeout=15000)
                expect(page.get_by_text("AI draft · verify against transcript")).to_be_visible()
                frame()
                search = page.get_by_role("searchbox", name="Search transcript")
                search.fill("deadline")
                expect(page.locator(".transcript-line")).to_have_count(1)
                frame()
                page.get_by_role("button", name="Follow live ↓", exact=True).click()
                expect(page.locator(".transcript-line")).to_have_count(6)
                with page.expect_download() as download:
                    page.get_by_role("button", name="Save text", exact=True).click()
                assert download.value.suggested_filename == "live-transcript.txt"
                assert "deadline" in Path(download.value.path()).read_text()
                iframe = page.locator("iframe").element_handle()
                page.get_by_role("button", name="VM console", exact=True).click()
                expect(page.locator(".viewer-frame")).to_be_visible()
                frame()
                page.get_by_role("button", name="Transcript", exact=True).click()
                assert iframe.evaluate("el => el.isConnected"), "tab changes must keep VNC mounted"
                frame()
                frames[2].save(args.output / "workspace.png")
                frames[0].save(
                    args.output / "workspace-demo.gif",
                    save_all=True,
                    append_images=frames[1:],
                    duration=[1800, 1800, 2600, 1600, 2200, 2000],
                    loop=0,
                    optimize=True,
                )
                # Exercise reading, incident handoff and narrow screens with the
                # same packaged assets. These frames never enter the public GIF.
                checkpoint(
                    [
                        (
                            n * 5,
                            "Participant 1",
                            f"Synthetic passage {n}: recording remains independent.",
                        )
                        for n in range(50)
                    ]
                )
                expect(page.locator(".transcript-line")).to_have_count(50, timeout=15000)
                page.locator(".transcript-scroll").evaluate(
                    "el => {el.scrollTop=0;el.dispatchEvent(new Event('scroll'));}"
                )
                expect(page.get_by_role("button", name="Follow live ↓", exact=True)).to_be_visible()
                bridge.recovery = True
                server.cached_status = None
                expect(page.get_by_role("button", name="Open console", exact=True)).to_be_visible(
                    timeout=15000
                )
                expect(page.locator(".viewer-frame")).to_be_visible()
                page.get_by_role("button", name="Transcript", exact=True).click()
                page.wait_for_timeout(4500)
                expect(page.locator(".transcript-panel")).to_be_visible()
                journal = root / "captions.sqlite3"
                journal.rename(root / "offline.sqlite3")
                expect(
                    page.get_by_text(
                        "Caption journal is unavailable; recording is independent", exact=True
                    )
                ).to_be_visible(timeout=15000)
                expect(page.locator(".transcript-line")).to_have_count(50)
                expect(page.locator(".badge.recording")).to_contain_text("Saving join evidence")
                (root / "offline.sqlite3").rename(journal)
                expect(
                    page.get_by_text(
                        "Caption journal is unavailable; recording is independent", exact=True
                    )
                ).to_have_count(0, timeout=15000)
                for width in (390, 760, 1024):
                    page.set_viewport_size(dict(width=width, height=900))
                    page.wait_for_timeout(100)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                        f"overflow at {width}"
                    )
                    page.screenshot(
                        path=str(args.output / f"workspace-{width}.png"), full_page=True
                    )
                assert not errors, errors
                browser.close()
            store.close()
    finally:
        for service in servers:
            service.shutdown()
            service.server_close()
        config.CONFIG = original
    print("Synthetic UI walkthrough and browser checks complete")


if __name__ == "__main__":
    main()
