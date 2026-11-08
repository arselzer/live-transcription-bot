"""Read-only recording capacity estimates; never delete evidence or stop a call."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

from live_transcript.core.config import get, ssh_options

MIB = 1024**2


def memory_available():
    try:
        values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return int(values["MemAvailable"].split()[0]) * 1024
    except (OSError, ValueError, KeyError):
        return None


def existing_parent(path):
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def vm_host_status(root, spec):
    """Written in a separate shared status mount, never exposing disk/admin keys."""
    root = Path(root)
    disk = root / "system.qcow2"
    try:
        allocated = disk.stat().st_blocks * 512
    except OSError:
        allocated = None
    usage = shutil.disk_usage(root)
    return dict(
        checked_at=time.time(),
        owner=spec["id"],
        free_bytes=usage.free,
        total_bytes=usage.total,
        filesystem=str(root.stat().st_dev),
        disk_allocated_bytes=allocated,
        memory_available_bytes=memory_available(),
    )


def guest_free_bytes():
    # The selected guest path comes from a validated local profile, never HTTP.
    import shlex

    code = (
        "import shutil,json;print(json.dumps({'free':shutil.disk_usage("
        + repr(get("vm", "home"))
        + ").free}))"
    )
    response = subprocess.run(
        [
            "ssh",
            *ssh_options(),
            "-o",
            "ConnectTimeout=4",
            get("vm", "ssh"),
            shlex.join([get("vm", "python"), "-c", code]),
        ],
        capture_output=True,
        timeout=8,
        check=True,
    )
    if len(response.stdout) > 4096:
        raise ValueError("Invalid guest capacity response")
    value = json.loads(response.stdout)["free"]
    if type(value) is not int or value < 0:
        raise ValueError("Invalid guest capacity")
    return value


def report(minutes=0, *, guest_free=None):
    if type(minutes) is not int or not 0 <= minutes <= 1440:
        raise ValueError("Invalid capacity duration")
    root = existing_parent(get("paths", "root"))
    free = shutil.disk_usage(root).free
    reserve = get("storage", "host_free_reserve_bytes")
    rate = get("capacity", "recording_bytes_per_minute")
    copies = get("capacity", "host_copy_factor")
    vm_host = None
    try:
        data = json.loads((Path(get("paths", "state")) / "vm-status/capacity.json").read_text())
        if (
            0 <= time.time() - data["checked_at"] < 90
            and type(data["free_bytes"]) is int
            and data["free_bytes"] >= 0
            and isinstance(data.get("filesystem"), str)
        ):
            vm_host = data
    except (OSError, ValueError, TypeError, KeyError):
        pass
    reasons, warnings = [], []
    memory = memory_available()
    if memory is None:
        warnings.append("Host memory availability is unknown")
    elif memory < get("capacity", "min_memory_available_bytes"):
        reasons.append("Host memory headroom is below the configured reserve")
    same_disk = vm_host is not None and vm_host["filesystem"] == str(root.stat().st_dev)
    host_rate = rate * (copies + int(same_disk))
    remaining = max(0, free - reserve) / host_rate
    if free < reserve or minutes and remaining < minutes:
        reasons.append("Application storage has insufficient recording headroom")
    if vm_host:
        vm_minutes = max(0, vm_host["free_bytes"] - reserve) / (host_rate if same_disk else rate)
        remaining = min(remaining, vm_minutes)
        if vm_host["free_bytes"] < reserve or minutes and vm_minutes < minutes:
            reasons.append("VM backing storage has insufficient recording headroom")
    elif (Path(get("paths", "state")) / "vm-status").exists():
        warnings.append("VM backing storage status is unavailable or stale")
    if guest_free is not None:
        if type(guest_free) is not int or guest_free < 0:
            raise ValueError("Invalid guest capacity")
        guest_minutes = max(0, guest_free - get("capacity", "guest_free_reserve_bytes")) / rate
        remaining = min(remaining, guest_minutes)
        if guest_minutes < minutes or guest_free < get("capacity", "guest_free_reserve_bytes"):
            reasons.append("Guest storage has insufficient recording headroom")
    else:
        warnings.append("Guest storage availability is unknown")
    return dict(
        status="blocked" if reasons else "warning" if warnings else "ready",
        checked_at=time.time(),
        required_minutes=minutes,
        estimated_minutes_remaining=round(remaining, 1),
        recording_bytes_per_minute=rate,
        host_free_bytes=free,
        vm_host_free_bytes=vm_host["free_bytes"] if vm_host else None,
        vm_disk_allocated_bytes=vm_host.get("disk_allocated_bytes") if vm_host else None,
        guest_free_bytes=guest_free,
        memory_available_bytes=memory,
        reasons=reasons,
        warnings=warnings,
    )


def preflight(minutes):
    try:
        guest = guest_free_bytes()
    except (OSError, ValueError, subprocess.SubprocessError):
        guest = None
    result = report(minutes, guest_free=guest)
    if result["reasons"]:
        raise RuntimeError("; ".join(result["reasons"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-min", type=int, default=0)
    parser.add_argument("--require", action="store_true")
    args = parser.parse_args()
    result = preflight(args.max_min) if args.require else report(args.max_min)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
