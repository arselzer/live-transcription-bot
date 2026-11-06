#!/usr/bin/env python3
"""Continuously mirror raw evidence to host storage; never removes VM sources.

Started independently of the SSH session by the host orchestrator. Mutable
headers are recopied using checksum comparison, not unsafe append-only copying.
The last open segment may be incomplete; finalized segments remain independent.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import hashlib
import shutil
from live_transcript.capture.guard import atomic_json, verify_inventory
from live_transcript.core.config import get, ssh_transport


def finalized(root):
    """The completion marker is written only after media and inventory flush."""
    try:
        root = Path(root)
        marker = json.loads((root / "finalized.json").read_text())
        digest = hashlib.sha256((root / "integrity.json").read_bytes()).hexdigest()
        return marker.get("inventory_sha256") == digest
    except (OSError, ValueError, AttributeError):
        return False


def archive_from_log(path):
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None, False
    explicit = re.findall(r"^ARCHIVE to (\S+)", text, re.M)
    recordings = re.findall(r"^RECORDING to (\S+)", text, re.M)
    root = (
        explicit[-1]
        if explicit
        else str(Path(recordings[-1]).with_suffix(".archive"))
        if recordings
        else None
    )
    return root, bool(re.search(r"^DONE ", text, re.M))


def mirror(vm, remote, destination, evidence_only=False):
    from live_transcript.storage.layout import ensure_archive_volume

    ensure_archive_volume(destination)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rsync",
        "-rt",
        "--checksum",
        "--partial",
        "--protect-args",
        "--exclude=*.tmp",
        "--exclude=guard.lock",
    ]
    if evidence_only:
        cmd += [
            "--include=*.flac",
            "--include=*.jpg",
            "--include=*.json",
            "--include=*.jsonl",
            "--include=*.txt",
            "--include=*.wav",
            "--include=audio-*.csv",
            "--exclude=*",
        ]
    cmd += [
        "-e",
        ssh_transport(),
        vm + ":" + remote.rstrip("/") + "/",
        str(destination) + "/",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return result.returncode, result.stderr[-500:]


def capacity_available(used, free, max_bytes, min_free):
    return used <= max_bytes - 1024**3 and free >= min_free + 1024**3


def safety_copy(vm, remote, primary, directory, max_bytes=None, min_free=None):
    """A second filesystem protects critical evidence if archive storage fails.

    No automatic deletion. Keep a 1 GB transfer margin and a root-disk reserve.
    Video segments stay on primary; audio, snapshots and metadata get both copies.
    """
    max_bytes = get("storage", "safety_max_bytes") if max_bytes is None else max_bytes
    min_free = get("storage", "host_free_reserve_bytes") if min_free is None else min_free
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    out = directory / Path(remote).name
    out.mkdir(exist_ok=True, mode=0o700)
    result = dict(utc=time.time(), status="pending", directory=str(out), screen_video_copied=False)
    # Never stat the configured archive mount from the local safety-copy worker: a
    # storage outage must not block the independent copy on local storage.
    mounted_archive = (
        str(primary).startswith(get("paths", "recordings").rstrip("/") + "/")
        and bool(get("paths", "archive_mount"))
    ) or (
        bool(get("paths", "archive_mount"))
        and str(primary).startswith(get("paths", "archive_mount").rstrip("/") + "/")
    )
    primary_path = Path(primary) if not mounted_archive else None
    same = (
        primary_path is not None
        and directory.stat().st_dev
        == (primary_path if primary_path.exists() else primary_path.parent).stat().st_dev
    )
    if same:
        result.update(
            status="same_filesystem", error="secondary copy must use a different filesystem"
        )
    else:
        used = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        if not capacity_available(used, shutil.disk_usage(directory).free, max_bytes, min_free):
            result.update(
                status="capacity_limit",
                error="local safety reserve/quota reached; existing copies retained",
            )
        else:
            try:
                code, error = mirror(vm, remote, out, evidence_only=True)
                result.update(status="ok" if code == 0 else "retrying", code=code, error=error)
                if code == 0:
                    wav = str(Path(remote).with_suffix(".wav"))
                    target = directory / (Path(remote).stem + ".monitor.wav")
                    transfer = subprocess.run(
                        [
                            "rsync",
                            "-rt",
                            "--checksum",
                            "--partial",
                            "--protect-args",
                            "-e",
                            ssh_transport(),
                            vm + ":" + wav,
                            str(target),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if transfer.returncode not in (0, 23):
                        result.update(
                            status="retrying", error="secondary monitor WAV transfer failed"
                        )
                    if finalized(out):
                        result["verified_critical_files"] = verify_inventory(
                            out, evidence_only=True
                        )
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                result.update(status="retrying", error=type(exc).__name__)
    atomic_json(out / "backup-status.json", result)
    return result


def run(vm, log, destination, max_seconds=9 * 3600, secondary=None, lane="archive"):
    secondary = secondary or get("paths", "safety_copies")
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        remote, done = archive_from_log(log)
        if not remote:
            try:
                text = Path(log).read_text(errors="replace")
                if "DRY:" in text or Path(str(log) + ".finished.json").exists():
                    return
            except OSError:
                pass
        if remote:
            out = Path(destination) / Path(remote).name
            if lane == "local":
                try:
                    backup = safety_copy(vm, remote, out, secondary)
                    local = Path(secondary) / Path(remote).name
                    if backup["status"] == "ok" and finalized(local):
                        return
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    print("Local archive retry:", type(exc).__name__, flush=True)
                time.sleep(10)
                continue
            try:
                code, error = mirror(vm, remote, out)
            except (OSError, subprocess.TimeoutExpired) as exc:
                code, error = 30, type(exc).__name__
            try:
                try:
                    backup = json.loads(
                        (Path(secondary) / Path(remote).name / "backup-status.json").read_text()
                    )
                except (OSError, ValueError):
                    backup = dict(status="pending")
                atomic_json(
                    out / "mirror-status.json",
                    dict(utc=time.time(), code=code, error=error, safety=backup),
                )
                if finalized(out) and code == 0:
                    return
            except (OSError, subprocess.TimeoutExpired) as exc:
                print("Archive mirror retry:", exc, flush=True)
        time.sleep(10)


def main():
    os.umask(0o077)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vm", required=True)
    ap.add_argument("--join-log", required=True)
    ap.add_argument("--destination", default=get("paths", "recordings"))
    ap.add_argument("--max-seconds", type=float, default=9 * 3600)
    ap.add_argument("--secondary-root", default=get("paths", "safety_copies"))
    ap.add_argument("--lane", choices=("archive", "local", "nas"), default="archive")
    a = ap.parse_args()
    run(a.vm, a.join_log, a.destination, a.max_seconds, a.secondary_root, a.lane)


if __name__ == "__main__":
    main()
