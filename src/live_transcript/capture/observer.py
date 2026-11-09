#!/usr/bin/env python3
"""Independent speaker/participant evidence collector; no caption dependency."""

import argparse
import json
import os
from pathlib import Path
import time
from live_transcript.capture.guard import atomic_json
from live_transcript.core.options import provider_origin

ZOOM_SPEAKERS_JS = """() => {
 const tiles=[...document.querySelectorAll('[class*=speaker-bar-container__video-frame],'
  +'[class*=speaker-active-container__video-frame],[class*=gallery-video-container__video-frame],'
  +'[class*=speaker-active-container],[class*=videoView],[class*=tile]')];
 const out=[];const seen=new Set();
 for(const e of tiles){
  const label=e.querySelector('.video-avatar__avatar-footer span,[class*=video-avatar__avatar-name],'
   +'[class*=name-label],[class*=participant-name],[class*=video-player__name],[class*=avatar-footer]');
  const name=(label?.textContent||'').trim().slice(0,100);if(!name)continue;
  const role=/speaker-active-container|--active|speaking/.test(e.className)?'active':'tile';
  const key=name+'|'+role;if(seen.has(key))continue;seen.add(key);out.push({role,name});
 }return out.slice(0,100);
}"""


def speaker_script(provider):
    if provider == "proton":
        from live_transcript.providers.proton import SPEAKERS_JS

        return SPEAKERS_JS
    if provider == "google":
        from live_transcript.providers.google_meet import SPEAKERS_JS

        return SPEAKERS_JS
    if provider == "jitsi":
        from live_transcript.providers.jitsi import SPEAKERS_JS

        return SPEAKERS_JS
    if provider == "zoom":
        return ZOOM_SPEAKERS_JS
    raise ValueError("Unsupported evidence provider")


def collect(root):
    from playwright.sync_api import sync_playwright

    root = Path(root)
    config = json.loads((root / "config.json").read_text())
    started = config["started"]
    source = speaker_script(config["provider"])
    # The scoped sidecar survives later meetings clearing legacy scratch files.
    paths = [root / "speakers.jsonl", root.with_suffix(".speakers.jsonl")]
    last_sync = 0
    last_success = time.time()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(config["cdp"], timeout=5000)
        try:
            while time.time() < config["deadline"] and not (root / "stop.json").exists():
                now = time.time()
                row = dict(t=round(now - started, 3), utc=now, who=[], source="browser-dom")
                try:
                    pages = [
                        pg
                        for ctx in browser.contexts
                        for pg in ctx.pages
                        if provider_origin(config["provider"], pg.url)
                    ]
                    if not pages:
                        raise RuntimeError("meeting page unavailable")
                    page = pages[-1]
                    frames = page.frames if config["provider"] == "zoom" else [page.main_frame]
                    seen = set()
                    for frame in frames:
                        for person in frame.evaluate(source):
                            key = (person["name"], person["role"])
                            if key not in seen:
                                row["who"].append(person)
                                seen.add(key)
                    if not row["who"]:
                        row["error"] = "participant selectors returned no names"
                    else:
                        last_success = now
                except Exception as exc:
                    row["error"] = str(exc)[:200]
                sync = now - last_sync >= 1
                for path in paths:
                    with open(path, "a") as fh:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        fh.flush()
                        if sync:
                            os.fsync(fh.fileno())
                if sync:
                    last_sync = now
                    atomic_json(
                        root / "observer-health.json",
                        dict(
                            utc=now,
                            last_success=last_success,
                            error=row.get("error"),
                            participants=len(row["who"]),
                        ),
                    )
                time.sleep(max(0, 0.5 - (time.time() - now)))
        finally:
            # Do not browser.close(): it would close the actual meeting.
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True)
    a = ap.parse_args()
    os.umask(0o077)
    collect(a.root)


if __name__ == "__main__":
    main()
