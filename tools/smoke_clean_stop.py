#!/usr/bin/env python3
"""Exercise real service shutdown and verify recorder flush without a meeting."""

import json
from pathlib import Path
from live_transcript.core.config import get

guest_home = get("vm", "home")
import subprocess
import time
import uuid


def ssh(command, timeout=15):
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", get("vm", "ssh"), command],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout


def main():
    Path(get("paths", "logs")).mkdir(parents=True, exist_ok=True)
    token = "clean-stop-" + uuid.uuid4().hex[:10]
    ssh(f"python3 {guest_home}/meeting_session.py --assert-idle")
    command = (
        "export XDG_RUNTIME_DIR=/run/user/$(id -u) PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native "
        f"LT_RUN_ID={token} LT_MEETING_PROGRAM={guest_home}/stop_recording_fixture.py; "
        f"exec python3 -u {guest_home}/meeting_session.py"
    )
    log = Path(get("paths", "logs")) / "clean-stop-fixture.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            ["ssh", get("vm", "ssh"), command], stdout=fh, stderr=subprocess.STDOUT
        )
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if "STOP FIXTURE READY" in log.read_text():
                break
            if proc.poll() is not None:
                raise RuntimeError(log.read_text())
            time.sleep(0.2)
        else:
            raise TimeoutError("stop fixture failed to initialize")
        time.sleep(6)
        ssh(f"python3 {guest_home}/meeting_session.py --stop " + token, timeout=155)
        code = proc.wait(timeout=10)
        text = log.read_text()
        assert code == 0, text
        line = next(line for line in text.splitlines() if line.startswith("STOP FIXTURE COMPLETE "))
        report = json.loads(line.removeprefix("STOP FIXTURE COMPLETE "))
        report.update(
            owner_first_shutdown_verified=True,
            no_duplicate_signal_error="Received > 3 system signals" not in text,
        )
        (Path(get("paths", "logs")) / "clean-stop-report.json").write_text(
            json.dumps(report, indent=2)
        )
        print(json.dumps(report, indent=2))
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    main()
