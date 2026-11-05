#!/usr/bin/env python3
"""Verify real VM ownership and SSH reconnection without opening any meeting."""

import json
from pathlib import Path
from live_transcript.core.config import get

guest_home = get("vm", "home")
import subprocess
import time
import uuid

VM = get("vm", "ssh")


def ssh(command, check=True, timeout=10):
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", VM, command],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def main():
    Path(get("paths", "logs")).mkdir(parents=True, exist_ok=True)
    ssh(f"python3 {guest_home}/meeting_session.py --assert-idle")
    token = "lifecycle-" + uuid.uuid4().hex[:12]
    unit = "live-transcript-meeting-" + token
    command = (
        "export XDG_RUNTIME_DIR=/run/user/$(id -u) PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native "
        f"LT_RUN_ID={token} LT_MEETING_PROGRAM={guest_home}/session_lifecycle_fixture.py; "
        f"exec python3 -u {guest_home}/meeting_session.py"
    )
    follower = subprocess.Popen(
        ["ssh", "-o", "ConnectTimeout=5", VM, command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready = ssh(f"cat {guest_home}/logs/session-" + token + ".log", check=False)
            if "SESSION FIXTURE READY" in ready.stdout:
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("fixture worker did not start")
        before = ssh("systemctl --user show " + unit + " -p MainPID --value").stdout.strip()
        denied = ssh(f"python3 {guest_home}/meeting_session.py --assert-idle", check=False)
        assert denied.returncode != 0, "active VM lease did not block preparation"
        mismatch = ssh(command + " different-invocation", check=False)
        assert mismatch.returncode != 0, "same token accepted a different invocation"
        follower.terminate()
        follower.wait(timeout=5)
        after = ssh("systemctl --user show " + unit + " -p MainPID --value").stdout.strip()
        assert before == after and after != "0", "meeting service stopped with SSH"
        resumed = ssh(command, timeout=30)
        assert "SESSION FIXTURE COMPLETE" in resumed.stdout
        remote = json.loads(
            ssh(f"python3 {guest_home}/meeting_session.py --status " + token).stdout
        )
        assert remote["phase"] == "finished", remote
        report = dict(
            token=token,
            vm_lease_blocks_reset=True,
            duplicate_invocation_rejected=True,
            ssh_disconnect_survived=True,
            reconnect_completed=True,
            notes="Supervised idle fixture only; no browser or meeting opened.",
        )
        out = Path(get("paths", "logs")) / "session-lifecycle-drill.json"
        out.write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        if follower.poll() is None:
            follower.terminate()
            follower.wait(timeout=5)
        follower.stdout.close()
        follower.stderr.close()
        ssh(f"python3 {guest_home}/meeting_session.py --stop " + token, check=False, timeout=135)


if __name__ == "__main__":
    main()
