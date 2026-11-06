#!/usr/bin/env python3
"""Verify real VM ownership and SSH reconnection without opening any meeting."""

import json
import shlex
from pathlib import Path
from live_transcript.core.config import get, ssh_options
from live_transcript.worker import guest_command
from tools.run_tts_e2e import deploy_test_bundle

guest_home = get("vm", "home")
import subprocess
import time
import uuid

VM = get("vm", "ssh")


def ssh(command, check=True, timeout=10):
    return subprocess.run(
        ["ssh", *ssh_options(), "-o", "ConnectTimeout=5", VM, command],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def main():
    deploy_test_bundle()
    Path(get("paths", "logs")).mkdir(parents=True, exist_ok=True)
    ssh(shlex.join(guest_command("session", "--assert-idle", test=True)))
    token = "lifecycle-" + uuid.uuid4().hex[:12]
    unit = "live-transcript-meeting-" + token
    command = (
        "export XDG_RUNTIME_DIR=/run/user/$(id -u) PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native "
        f"LT_RUN_ID={token} LT_TEST_ROLE=fixture-session; "
        "exec " + shlex.join(guest_command("session", test=True))
    )
    follower = subprocess.Popen(
        ["ssh", *ssh_options(), "-o", "ConnectTimeout=5", VM, command],
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
        denied = ssh(shlex.join(guest_command("session", "--assert-idle", test=True)), check=False)
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
            ssh(shlex.join(guest_command("session", "--status", test=True)) + " " + token).stdout
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
        ssh(
            shlex.join(guest_command("session", "--stop", test=True)) + " " + token,
            check=False,
            timeout=135,
        )


if __name__ == "__main__":
    main()
