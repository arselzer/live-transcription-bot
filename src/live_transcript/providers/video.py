#!/usr/bin/env python3
"live_transcript.providers.video.py — attach to joiner's Chromium via CDP, report + force video state."


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        pg = None
        for ctx in b.contexts:
            for q in ctx.pages:
                if "/wc/" in (q.url or "") or (
                    q.url.startswith("https://meet.proton.me/") and "/join/" in q.url
                ):
                    pg = q
                    break
        if pg is None:
            print("NO MEETING PAGE")
            raise SystemExit(2)

        if pg.url.startswith("https://meet.proton.me/"):
            import sys

            on = pg.get_by_role("button", name="Turn off camera", exact=True).first.is_visible()
            if not on and "--report-only" not in sys.argv:
                pg.get_by_role("button", name="Turn on camera", exact=True).click(timeout=3000)
                pg.wait_for_timeout(2500)
                on = pg.get_by_role("button", name="Turn off camera", exact=True).first.is_visible()
            print("STATE:", "on" if on else "off")
            raise SystemExit(0 if on else 3)

        def state():
            return pg.evaluate("""() => {
                const out = [];
                document.querySelectorAll('button[aria-label]').forEach(b => {
                    const l = (b.getAttribute('aria-label') || '').toLowerCase();
                    if (l.includes('my video') || l.includes('stop my video') || l.includes('start my video'))
                        out.push({label: b.getAttribute('aria-label'), vis: !!(b.offsetWidth||b.offsetHeight)});
                });
                return {btns: out, modal: [...document.querySelectorAll('[aria-label]')].map(e=>e.getAttribute('aria-label')).filter(t=>/got it|cancel|not now/i.test(t)).slice(0,4)};
            }""")

        s = state()
        print("before:", s)
        import sys

        if "--report-only" in sys.argv:
            on = any("stop my video" in (b0["label"] or "").lower() for b0 in s["btns"])
            print("STATE:", "stop my video" if on else "start my video")
            raise SystemExit(0 if on else 3)

        # dismiss modals then click video-on if off
        for lbl in ("Got it", "Cancel", "Not now"):
            try:
                pg.locator(f'button:has-text("{lbl}")').first.click(timeout=1200)
                print("dismissed", lbl)
            except Exception:
                pass
        s = state()
        on = any("stop my video" in (b0["label"] or "").lower() for b0 in s["btns"])
        if not on:
            try:
                pg.evaluate("""() => { for (const b of document.querySelectorAll('button[aria-label]')) {
                    const l=(b.getAttribute('aria-label')||'').toLowerCase();
                    if (l.includes('start my video')) { b.click(); return true } } return false }""")
                print("clicked start-my-video via JS")
            except Exception as e:
                print("click failed", e)
        pg.wait_for_timeout(3500)
        print("after:", state())


if __name__ == "__main__":
    main()
