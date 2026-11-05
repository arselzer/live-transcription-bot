#!/usr/bin/env python3
"""Proton shared-calendar -> Live Transcript auto-join — Hermes cron script job (no_agent).

Fetches the user's Proton Calendar "share with anyone" ICS link (scoped, revocable,
single calendar; no account mount) and launches the Live Transcript bot for a Zoom
meeting whose start time has arrived. Silent stdout = nothing to do (cron delivers
stdout to Telegram, so we print ONLY on an action or a real problem).

Policy: auto-launch with a T-2 min lead for recognized Zoom/Proton invitations
on THIS shared calendar. Exact systemd timers supplement Hermes reconciliation.
Dedupe by UID+DTSTART so an
edited/moved event can retrigger once. Never launches while a meeting is active.
Telegram heads-ups (user-approved 2026-11-04): the evening before at 20:00 Vienna,
and T-30 min before start. Informational only — no launch decision changes.
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
import fcntl
from pathlib import Path
import argparse
from live_transcript.capture.guard import atomic_json
from zoneinfo import ZoneInfo
from live_transcript.core.config import get

URL_FILE = get("calendar", "url_file")
STATE = str(Path(get("paths", "logs")) / "calendar-state.json")
MJSON = str(Path(get("paths", "logs")) / "current_meeting.json")
SCRIBE = str(Path(get("paths", "root")) / "scribe_run.sh")
LEAD = 120  # join 2 minutes before the official start
LATE_GRACE = 600  # still ok to join up to 10 min after start (boot/VPN hiccups)
REMINDER = 90  # if the event carries VALARM TRIGGER, honor it, floor 90s
TZ_FALLBACK = get("calendar", "timezone")
HEADSUP_EARLY = 1800  # Telegram heads-up 30 min before start
PREVIEW_HOUR = 20  # Telegram heads-up the evening before, 20:00 Vienna


def load_state():
    try:
        return json.loads(Path(STATE).read_text())
    except Exception:
        return {}


def save_state(st):
    Path(STATE).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(STATE, st)


def fetch_ics():
    url = Path(URL_FILE).read_text().strip()
    req = urllib.request.Request(url, headers={"User-Agent": "meetbot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(8 * 1024 * 1024 + 1).decode("utf-8", "replace")
        if (
            len(data) > 8 * 1024 * 1024
            or not data.lstrip().startswith("BEGIN:VCALENDAR")
            or "END:VCALENDAR" not in data
        ):
            raise ValueError("Calendar response is incomplete or is not an ICS feed")
        return data


def unfold(raw):
    return re.sub(r"\r?\n[ \t]", "", raw)


def parse_dt(value, tzmap):
    """ICS dt value WITHOUT the property params (caller strips ';TZID=...')."""
    m = re.match(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        m = re.match(r"(\d{8})(Z?)$", value)  # all-day -> midnight
        if not m:
            return None
        d = dt.datetime.strptime(m.group(1), "%Y%m%d")
        return d.replace(tzinfo=dt.timezone.utc)
    d = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3):
        return d.replace(tzinfo=dt.timezone.utc)
    return d  # caller attaches TZID


def events(ics):
    ics = unfold(ics)  # idempotent; DESCRIPTION is often soft-wrapped — unfolding
    # once here is what keeps wrapped URLs (incl. #pwd fragments) intact
    tzmap = {}
    for tzid, body in re.findall(
        r"BEGIN:VTIMEZONE\s*\nTZID:([^\r\n]+)\r?\n(.*?)END:VTIMEZONE", ics, re.S
    ):
        try:
            tzmap[tzid] = ZoneInfo(tzid.strip())
        except Exception:
            pass
    out = []
    for body in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics, re.S):

        def prop(name):
            m = re.search(rf"^{name}(?:;[^:\r\n]*)*:(.*)$", body, re.M)
            return m.group(1).strip() if m else None

        def prop_tz(name):
            m = re.search(rf"^{name};[^:\r\n]*TZID=([^:;\r\n]+):(.*?)(?:;|$)", body, re.M)
            return m.group(1).strip() if m else None

        raw_start = prop("DTSTART")
        if (prop("STATUS") or "").upper() == "CANCELLED":
            continue
        if not raw_start:
            continue
        start = parse_dt(raw_start, tzmap)
        if start is None:
            continue
        if start.tzinfo is None:
            tz = prop_tz("DTSTART") or TZ_FALLBACK
            try:
                start = start.replace(tzinfo=ZoneInfo(tz))
            except Exception:
                start = start.replace(tzinfo=ZoneInfo(TZ_FALLBACK))
        desc = "\n".join(prop(name) or "" for name in ("DESCRIPTION", "LOCATION", "URL"))
        desc = desc.replace("\\n", "\n").replace("\\,", ",")
        url = None
        # Proton Meet first: its #pwd- fragment is mandatory (decrypts the room)
        # and the generic zoom/tail regexes must not mangle it.
        m = re.search(r"https://meet\.proton\.me/join/[A-Za-z0-9_-]+#pwd-[A-Za-z0-9_-]+", desc)
        if not m:
            m = re.search(r"https://[A-Za-z0-9.-]*zoom\.us/j/\d+\?pwd=[^\s,]+", desc)
        if not m:
            m = re.search(r"https://[A-Za-z0-9.-]*zoom\.us/j/\d+", desc)
        url = m.group(0) if m else None
        pwd = re.search(r"Passcode:\s*([A-Za-z0-9]+)", desc)
        remind = None
        trg = re.search(r"TRIGGER[:;][^\r\n]*?[-+]PT(\d+)([MH])", body)
        if trg:
            remind = int(trg.group(1)) * (3600 if trg.group(2) == "H" else 60)
        end = parse_dt(prop("DTEND"), tzmap) if prop("DTEND") else None
        if end is not None and end.tzinfo is None:
            try:
                end = end.replace(
                    tzinfo=ZoneInfo(prop_tz("DTEND") or prop_tz("DTSTART") or TZ_FALLBACK)
                )
            except Exception:
                end = end.replace(tzinfo=ZoneInfo(TZ_FALLBACK))
        out.append(
            {
                "uid": prop("UID") or raw_start,
                "start": start,
                "end": end,
                "summary": prop("SUMMARY") or "(no title)",
                "url": url,
                "passcode": pwd.group(1) if pwd else None,
                "remind": remind,
            }
        )
    return out


def meeting_active():
    try:
        d = json.loads(Path(MJSON).read_text())
        return (
            d.get("status") in ("active", "control_lost")
            and time_since(d.get("started", 0)) < 9 * 3600
        )
    except Exception:
        return False


def storage_maintenance():
    # Defer launches without consuming the event's dedupe key during VM moves.
    sys.path.insert(0, get("paths", "root"))
    from live_transcript.storage.layout import maintenance_active

    return maintenance_active()


def time_since(epoch):
    return time.time() - epoch if epoch else 1e9


def cycle(dry_run=False):
    now = dt.datetime.now(dt.timezone.utc)
    try:
        ics = fetch_ics()
    except Exception:
        # one quiet retry, then complain only if it's been broken a long time
        try:
            ics = fetch_ics()
        except Exception as e2:
            print(f"[meetbot] calendar fetch failing: {type(e2).__name__}: {e2}")
            return
    st = load_state()
    did = False
    vienna = ZoneInfo(TZ_FALLBACK)
    if dry_run:
        for ev in events(ics):
            if (
                ev["url"]
                and now.timestamp() - 600 <= ev["start"].timestamp() <= now.timestamp() + 86400
            ):
                print(
                    json.dumps(
                        dict(
                            summary=ev["summary"],
                            start=ev["start"].astimezone(vienna).isoformat(),
                            provider=__import__("urllib.parse", fromlist=["urlsplit"])
                            .urlsplit(ev["url"])
                            .hostname,
                        )
                    )
                )
        return
    from live_transcript.integrations.calendar_dispatch import reconcile

    reconcile(events(ics), now.timestamp())
    for ev in sorted(events(ics), key=lambda e: e["start"]):
        if not ev["url"]:
            continue
        start_ts = ev["start"].timestamp()
        key = f"{ev['uid']}|{int(start_ts)}"
        t = ev["start"].astimezone(vienna)
        # --- informational heads-ups (independent of the launch dedupe key) ---
        for kind, when in (
            (
                "preview-evening",
                ev["start"]
                .astimezone(vienna)
                .replace(hour=PREVIEW_HOUR, minute=0, second=0, microsecond=0)
                - dt.timedelta(days=1),
            ),
            ("preview-30", ev["start"] - dt.timedelta(seconds=HEADSUP_EARLY)),
        ):
            rkey = f"{key}|{kind}"
            if rkey in st or when > now or start_ts <= now.timestamp():
                continue
            st[rkey] = {"notified": kind, "at": now.isoformat()}
            save_state(st)
            did = True
            tag = "tomorrow" if kind == "preview-evening" else "in ~30 min"
            from live_transcript.providers.proton import public_url

            print(
                f'[meetbot] {tag}: "{ev["summary"]}" at {t:%H:%M} Vienna — {public_url(ev["url"])}'
            )
        # Exact timers own launch decisions. Polling only reconciles their
        # schedule and sends the already-authorized informational reminders.
        continue
    if not did:
        return  # silent = healthy
    # prune state entries older than 30 days
    cut = (now - dt.timedelta(days=30)).isoformat()
    pruned = {k: v for k, v in st.items() if v.get("at", "") > cut}
    if len(pruned) != len(st):
        save_state(pruned)


def main(argv=None):
    os.umask(0o077)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    Path(STATE).parent.mkdir(parents=True, exist_ok=True)
    with open(STATE + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        cycle(a.dry_run)


if __name__ == "__main__":
    main()
