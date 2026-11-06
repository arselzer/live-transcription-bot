"""Structured provider denials, private evidence and test-join limits."""

import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from live_transcript.capture.guard import atomic_json
from live_transcript.core.config import get

REASONS = {
    "google_access_denied": re.compile(
        r"you (?:can.t|cannot) join this (?:video call|meeting)", re.I
    ),
    "google_sign_in_required": re.compile(r"(?:sign in|log in) to join", re.I),
    "google_admission_timeout": re.compile(r"no confirmed admission", re.I),
    "zoom_automation_blocked": re.compile(
        r"automated bots (?:are not|aren[\x27\u2019]t) allowed|we detected you may be a bot", re.I
    ),
    "host_denied": re.compile(r"host.*denied|(?:entry|admission|request).*denied", re.I),
    "meeting_locked": re.compile(r"meeting is locked", re.I),
    "captcha_required": re.compile(r"not a robot|your browser may be automated", re.I),
}


class AdmissionFailure(SystemExit):
    def __init__(self, reason):
        super().__init__(4)
        self.reason = reason


def block_reason(text):
    # A terminal host/security decision takes precedence over generic bot text.
    for reason in ("host_denied", "meeting_locked", "captcha_required", "zoom_automation_blocked"):
        pattern = REASONS[reason]
        if pattern.search(text):
            return reason
    return None


def inspect_zoom(page, joined=False):
    if joined:
        return None  # participant chat cannot become an admission failure
    for frame in page.frames:
        host = urlsplit(frame.url).hostname or ""
        if host != "zoom.us" and not host.endswith(".zoom.us"):
            continue
        try:
            reason = block_reason(frame.locator("body").inner_text(timeout=700))
            if reason:
                return reason
        except Exception:
            continue
    return None


def save_failure(page, directory, reason, display_name, provider="zoom", automatic_retry=False):
    if reason not in (*REASONS, "admission_error"):
        raise ValueError("unknown admission failure")
    root = Path(directory)
    if (root / "failure.json").exists():
        root = root / ("attempt-" + str(time.time_ns()))
    parsed = urlsplit(page.url)
    evidence = dict(
        version=1,
        utc=time.time(),
        reason=reason,
        provider=provider,
        display_name=display_name,
        origin=parsed.hostname,
        automatic_retry=bool(automatic_retry),
        cause="not established" if reason == "zoom_automation_blocked" else reason,
    )
    # A scoped image survives future launches clearing legacy stage screenshots.
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        page.screenshot(path=str(root / "admission.jpg"), type="jpeg", timeout=1500)
        evidence["screenshot"] = "admission.jpg"
    except Exception:
        evidence["screenshot_unavailable"] = True
    try:
        atomic_json(root / "failure.json", evidence)
    except OSError as exc:
        evidence["diagnostic_storage_error"] = type(exc).__name__
    print("ADMISSION_FAILED " + json.dumps(evidence), flush=True)
    if "diagnostic_storage_error" not in evidence:
        print("ADMISSION_DIAGNOSTICS to " + str(root), flush=True)
    return evidence


def claim_test_join(profile, directory=None, now=None):
    """Throttle test churn across rooms; this never throttles production launches."""
    now = time.time() if now is None else now
    root = Path(directory or Path(get("vm", "home")) / "state")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "test-join-budget.json"
    key = hashlib.sha256(profile.encode()).hexdigest()
    with (root / "test-join-budget.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rows = json.loads(path.read_text())
        except FileNotFoundError:
            rows = []
        # A malformed budget is an operational error, not a reason to join freely.
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and type(r.get("utc")) in (int, float) for r in rows
        ):
            raise ValueError("test admission budget needs inspection")
        rows = [r for r in rows if now - r["utc"] < get("admission", "test_window_seconds")]
        recent = [r["utc"] for r in rows if r.get("profile") == key]
        if recent and now - max(recent) < get("admission", "test_min_interval_seconds"):
            raise RuntimeError(
                "test profile cooldown active; use offline replay instead of repeated joins"
            )
        if len(rows) >= get("admission", "test_max_attempts"):
            raise RuntimeError(
                "test join budget exhausted; use offline replay until the window expires"
            )
        rows.append(dict(utc=now, profile=key))
        atomic_json(path, rows)
