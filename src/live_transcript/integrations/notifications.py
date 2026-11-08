"""Durable notification handoff for optional clients such as Hermes.

The watchdog commits pending notifications with its deduplication state before
printing. Reading never acknowledges delivery. Clients acknowledge IDs only
after their own delivery succeeds; retrying an acknowledgement is safe.
"""

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import time

from live_transcript.core.config import get
from live_transcript.core.files import atomic_json

ACK_RECEIPT_LIMIT = 1000
LOCK_TIMEOUT_SECONDS = 5


def state_path():
    return Path(get("paths", "logs")) / "watchdog-state.json"


def load_state(path):
    try:
        with Path(path).open() as stream:
            data = json.loads(stream.read(16 * 1024 * 1024 + 1))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key, {}), dict)
        for key in ("issues", "rejoins", "notifications", "notification_acks")
    ):
        raise ValueError("Invalid watchdog state; retained without replacement")
    for row in data.get("issues", {}).values():
        if (
            not isinstance(row, dict)
            or type(row.get("at")) not in (int, float)
            or not math.isfinite(row["at"])
        ):
            raise ValueError("Invalid watchdog issue state; retained without replacement")
    if any(type(value) is not int or value < 0 for value in data.get("rejoins", {}).values()):
        raise ValueError("Invalid watchdog retry state; retained without replacement")
    if any(
        type(value) not in (int, float) or not math.isfinite(value)
        for value in data.get("notification_acks", {}).values()
    ):
        raise ValueError("Invalid delivery receipts; retained without replacement")
    for ident, item in data.get("notifications", {}).items():
        if (
            not re.fullmatch(r"[a-f0-9]{32}", ident)
            or not isinstance(item, dict)
            or item.get("id") != ident
            or any(not isinstance(item.get(key), str) for key in ("key", "message"))
            or any(
                type(item.get(key)) not in (int, float) or not math.isfinite(item[key])
                for key in ("created", "last_seen")
            )
        ):
            raise ValueError("Invalid notification state; retained without replacement")
    return data


def enqueue(state, messages, now=None):
    now = time.time() if now is None else now
    pending = state.setdefault("notifications", {})
    result = []
    for key, message in messages:
        # An unacknowledged repeat keeps its ID. It stays available even if the
        # stdout consumer or its external messaging provider fails.
        previous = next(
            (row for row in pending.values() if row["key"] == key and row["message"] == message),
            None,
        )
        if previous is None:
            ident = secrets.token_hex(16)
            previous = dict(id=ident, key=key, message=message, created=now, last_seen=now)
            pending[ident] = previous
        previous["last_seen"] = now
        result.append(dict(previous))
    return result


@contextmanager
def locked_state(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        os.fchmod(lock.fileno(), 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Watchdog is busy; no notifications acknowledged")
                time.sleep(0.05)
        yield load_state(path)


def pending(limit=100):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Notification limit must be between 1 and 1000")
    state = load_state(state_path())
    rows = sorted(
        state.get("notifications", {}).values(), key=lambda row: (row["created"], row["id"])
    )
    return dict(
        version=1,
        notifications=rows[:limit],
        pending_count=len(rows),
        delivery="unacknowledged",
        more=len(rows) > limit,
    )


def acknowledge(identifiers):
    if not identifiers or any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in identifiers):
        raise ValueError("Acknowledgement needs valid notification IDs")
    path = state_path()
    with locked_state(path) as state:
        queued = state.setdefault("notifications", {})
        receipts = state.setdefault("notification_acks", {})
        if any(ident not in queued and ident not in receipts for ident in identifiers):
            raise ValueError("Unknown notification ID; nothing acknowledged")
        for ident in identifiers:
            queued.pop(ident, None)
            receipts.setdefault(ident, time.time())
        # Only delivered receipts are bounded. Pending alerts are never evicted.
        state["notification_acks"] = dict(
            sorted(receipts.items(), key=lambda row: row[1])[-ACK_RECEIPT_LIMIT:]
        )
        atomic_json(path, state)
    return dict(version=1, acknowledged=identifiers, pending_count=len(queued))
