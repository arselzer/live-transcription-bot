#!/usr/bin/env python3
"""Guest-only real-browser DOM drift drill using a locally fulfilled fake page."""

import json
from pathlib import Path
import tempfile
import time
from playwright.sync_api import sync_playwright
from live_transcript.providers.rescue import UIRescue


def main():
    with tempfile.TemporaryDirectory(prefix="ui-rescue-fixture-") as tmp, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--mute-audio"])
        try:
            page = browser.new_page()
            page.route(
                "https://app.zoom.us/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="""
              <body><button class="totally-new-provider-class" aria-label="Join with Computer Audio"
               onclick="window.restored=true;this.remove()">Sound</button>
               <button onclick="window.left=true" aria-label="Leave Meeting">Leave</button></body>""",
                ),
            )
            page.goto("https://app.zoom.us/wc/fake-join")
            rescue = UIRescue(tmp, "zoom")
            started = time.monotonic()
            rescue.poll(page, True)
            assert time.monotonic() - started < 2, "planner blocked browser loop"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not page.evaluate("!!window.restored"):
                page.wait_for_timeout(250)
                rescue.next_check = 0
                rescue.poll(page, True)
            assert page.evaluate("!!window.restored"), "approved audio recovery did not execute"
            assert not page.evaluate("!!window.left"), "destructive action occurred"
            events = [
                json.loads(line) for line in (Path(tmp) / "events.jsonl").read_text().splitlines()
            ]
            assert any(e["kind"] == "ui_recovery_action" and e["applied"] for e in events)
            page.set_content('<button aria-label="Join Audio">Join Audio</button>')
            stale = rescue.collect(page)[0]
            page.locator("button").evaluate("e=>e.setAttribute('aria-label','Leave Meeting')")
            assert not rescue.execute(page, stale), "stale plan clicked replaced control"
            rescue.missing_since = time.monotonic() - 30
            rescue.next_check = 0
            rescue.poll(page, False)
            assert (
                json.loads((Path(tmp) / "ui-health.json").read_text())["state"] == "needs_operator"
            )
            print(
                json.dumps(
                    dict(
                        ok=True,
                        events=events,
                        stale_plan_rejected=True,
                        unknown_ui_escalated=True,
                        fixture_only=True,
                    )
                ),
                flush=True,
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
