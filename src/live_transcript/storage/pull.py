#!/usr/bin/env python3
"""Resumable recording retrieval; source audio is never removed from the VM."""

import argparse
from pathlib import Path
from live_transcript.core.config import get
import subprocess
import time
import json
from live_transcript.storage.mirror import mirror, finalized
from live_transcript.capture.guard import verify_inventory


def verify_archive(root, evidence_only=False):
    return verify_inventory(root, evidence_only)


def pull(vm, remote, destination, required=False):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            result = subprocess.run(
                [
                    "rsync",
                    "-a",
                    "--checksum",
                    "--partial",
                    "--protect-args",
                    "-e",
                    "ssh -o ConnectTimeout=6 -o ServerAliveInterval=15 -o ServerAliveCountMax=4",
                    vm + ":" + remote,
                    str(destination),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            if attempt == 2 and required:
                raise RuntimeError(
                    "Recording transfer timed out; source remains on the VM: " + remote
                )
            continue
        if result.returncode == 0:
            return True
        if not required and result.returncode == 23:
            return False  # optional sidecar absent
        time.sleep(attempt + 1)
    if required:
        raise RuntimeError("Recording transfer failed; audio remains on the VM: " + remote)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vm")
    ap.add_argument("wav")
    a = ap.parse_args()
    out = Path(get("paths", "recordings")) / Path(a.wav).name
    # Preserve the independent live result before final ASR can fail. This
    # convenience export must never prevent retrieval of original media.
    try:
        from live_transcript.storage.live_evidence import export

        print("Live caption evidence:", export(out))
    except Exception as exc:
        print("Live caption evidence export unavailable; journal retained:", type(exc).__name__)
    remote_archive = str(Path(a.wav).with_suffix(".archive"))
    archive = out.with_suffix(".archive")
    for attempt in range(3):
        try:
            code, error = mirror(a.vm, remote_archive, archive)
        except subprocess.TimeoutExpired:
            code, error = 30, "transfer timed out"
        if code == 0 or (code == 23 and not (archive / "config.json").exists()):
            break
        time.sleep(attempt + 1)
    if code == 0 and (archive / "integrity.json").exists():
        print("Verified archival evidence:", verify_archive(archive), "files")
    elif code != 0 and (code != 23 or (archive / "config.json").exists()):
        raise RuntimeError("Archive retrieval failed; originals remain on VM: " + error)
    has_archive = code == 0 and any(archive.glob("audio-*.flac"))
    if has_archive and not finalized(archive):
        # Older completed archives have no marker; their checksum inventory
        # and explicit stopped heartbeat are still sufficient for compatibility.
        health = json.loads((archive / "health.json").read_text())
        config = json.loads((archive / "config.json").read_text())
        if not (
            config.get("archive_version", 1) < 2
            and health.get("stopped")
            and (archive / "integrity.json").exists()
        ):
            raise RuntimeError(
                "Archive still active or unsealed; copied evidence is retained, retry after finalization"
            )
    primary = out.with_suffix(".monitor.wav") if has_archive else out
    pull(a.vm, a.wav, primary, required=not has_archive)
    if has_archive:
        from live_transcript.storage.recover import recover

        print("Recovered archival audio:", recover(archive, out, replace=out.exists()))
    for suffix in (".json", ".transport.jsonl", ".ffmpeg.log"):
        pull(a.vm, a.wav + suffix, str(out) + suffix)
    stem = out.with_suffix("")
    for name, suffix in (
        ("speakers.jsonl", ".speakers.jsonl"),
        ("notes.txt", ".notes.txt"),
        ("chat.txt", ".chat.txt"),
        ("voice.jsonl", ".voice.jsonl"),
    ):
        scoped = str(stem) + suffix
        if suffix == ".speakers.jsonl" and (archive / "speakers.jsonl").exists():
            import shutil

            shutil.copy2(archive / "speakers.jsonl", scoped)
        elif suffix == ".voice.jsonl" and (archive / "voice.jsonl").exists():
            import shutil

            shutil.copy2(archive / "voice.jsonl", scoped)
        elif (archive / "config.json").exists():
            # Never associate a previous meeting's shared scratch file with
            # this archive. New sessions keep voice evidence in their archive.
            # New archives never read another run's shared scratch sidecars.
            archived = archive / name
            if archived.exists():
                import shutil

                shutil.copy2(archived, scoped)
        else:
            pull(a.vm, str(Path(a.wav).parent / name), scoped)
    pull(a.vm, a.wav[:-4] + "-screen.mp4", str(stem) + "-screen.mp4")
    print(out)


if __name__ == "__main__":
    main()
