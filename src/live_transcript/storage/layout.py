#!/usr/bin/env python3
"""Move recordings to the data volume, verifying copies before switching paths."""

import hashlib
import os
from pathlib import Path
import shutil
import json
import time
import sys
from live_transcript.core.config import get


def maintenance_active(path=None):
    path = path or str(Path(get("paths", "logs")) / "vm-storage-maintenance.json")
    try:
        value = json.loads(Path(path).read_text())
        ticks = Path(f"/proc/{value['pid']}/stat").read_text().rsplit(")", 1)[1].split()[19]
        return (
            value.get("active", False)
            and value["deadline"] > time.time()
            and ticks == value["start_ticks"]
        )
    except (OSError, ValueError, KeyError):
        return False


def prepare_link(source, destination, policy_path=None):
    """Validate the local symlink without touching archive storage on the join path."""
    source, destination = Path(source), Path(destination)
    if os.path.abspath(source) == os.path.abspath(destination) and not source.is_symlink():
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(source).free < get("storage", "host_free_reserve_bytes"):
            raise RuntimeError("Recording reserve reached")
        return
    if source.is_symlink():
        target = os.path.abspath(os.path.join(source.parent, os.readlink(source)))
        if target != os.path.abspath(destination):
            raise ValueError("recordings link points at an unexpected archive destination")
    elif os.path.lexists(source):
        raise ValueError("Existing local recordings require an explicit verified migration")
    else:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.symlink_to(destination, target_is_directory=True)
    if policy_path is None:
        if shutil.disk_usage(get("paths", "root")).free < get("storage", "host_free_reserve_bytes"):
            raise RuntimeError(
                "Local recording reserve reached; free space before starting another meeting"
            )
    elif Path(policy_path).exists():
        policy_path = Path(policy_path)
        policy = json.loads(policy_path.read_text())
        if shutil.disk_usage("/").free < policy.get("host_free_reserve_bytes", 0):
            raise RuntimeError(
                "Local recording reserve reached; free local disk space before starting another meeting"
            )


def ensure_archive_volume(destination):
    """Background-only check; never write beneath an unmounted archive path."""
    configured_mount = get("paths", "archive_mount")
    if configured_mount:

        def configured_mounted():
            return any(
                line.split()[4] == configured_mount
                for line in Path("/proc/self/mountinfo").read_text().splitlines()
            )

        if not configured_mounted():
            os.stat(configured_mount + "/.")
        if not configured_mounted():
            raise RuntimeError(
                "Configured archive volume is not mounted; original evidence retained"
            )
        return


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.digest()


def migrate(source, destination):
    source, destination = Path(source), Path(destination)
    if source.is_symlink():
        if source.resolve() != destination.resolve():
            raise ValueError("recordings already point to a different destination")
        return destination
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe = destination / (".write-check-" + str(os.getpid()))
    probe.write_text("ok")
    probe.unlink()
    if source.exists():
        for path in source.rglob("*"):
            target = destination / path.relative_to(source)
            if path.is_dir():
                target.mkdir(exist_ok=True, mode=0o700)
            elif path.is_file():
                if not target.exists():
                    tmp = target.with_name(target.name + ".copying")
                    shutil.copy2(path, tmp)
                    if digest(path) != digest(tmp):
                        raise IOError(f"copy verification failed: {path.name}")
                    os.replace(tmp, target)
                elif digest(path) != digest(target):
                    raise ValueError(f"conflicting destination file: {path.name}")
        previous = source.with_name(source.name + ".verified-local")
        if previous.exists():
            raise ValueError("previous migration backup needs inspection")
        source.rename(previous)
        try:
            source.symlink_to(destination, target_is_directory=True)
        except Exception:
            previous.rename(source)
            raise
        # Only verified duplicate copies are removed; all recordings remain at destination.
        shutil.rmtree(previous)
    else:
        source.symlink_to(destination, target_is_directory=True)
    return destination


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare-link"]:
        prepare_link(get("paths", "recordings"), get("paths", "archive_destination"))
        raise SystemExit(0)
    ensure_archive_volume(get("paths", "archive_destination"))
    if get("paths", "archive_mount") and not os.path.ismount(get("paths", "archive_mount")):
        raise SystemExit("Recording data volume is not mounted; refusing to fill the host disk")
    print(migrate(get("paths", "recordings"), get("paths", "archive_destination")))
