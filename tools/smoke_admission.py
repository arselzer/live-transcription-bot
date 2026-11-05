#!/usr/bin/env python3
"""Guest-only bot-denial fixture; no connection to a real Zoom meeting."""

import json
from pathlib import Path
import tempfile
from playwright.sync_api import sync_playwright
from live_transcript.runtime.entry import joined_controls
from live_transcript.providers.admission import inspect_zoom, save_failure


def main():
    with tempfile.TemporaryDirectory(prefix="admission-fixture-") as tmp, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--mute-audio"])
        try:
            page = browser.new_page()
            page.route(
                "https://app.zoom.us/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="""
              <h1>Enter Meeting Info</h1><p>Automated bots aren't allowed to join this meeting.</p>
              <button disabled onclick="window.joined=true">Join</button><button>Sign in to join</button>""",
                ),
            )
            page.goto("https://app.zoom.us/wc/fixture/join?pwd=fixture-secret")
            reason = inspect_zoom(page, joined=joined_controls(page))
            assert reason == "zoom_automation_blocked"
            save_failure(page, tmp, reason, "Live Transcript")
            assert (Path(tmp) / "admission.jpg").stat().st_size > 1000
            assert "fixture-secret" not in (Path(tmp) / "failure.json").read_text()
            assert not page.evaluate("!!window.joined")
            page.set_content(
                "<button>Leave Meeting</button><button>Participants</button><p>Automated bots aren't allowed to join this meeting.</p>"
            )
            assert joined_controls(page)
            assert inspect_zoom(page, joined=joined_controls(page)) is None
            print(
                json.dumps(
                    dict(
                        ok=True,
                        reason=reason,
                        private_screenshot=True,
                        joined_chat_ignored=True,
                        fixture_only=True,
                    )
                )
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
