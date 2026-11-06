#!/usr/bin/env python3
"""Run the known bilingual TTS script against an already joined authorized Zoom or Proton bot.
Only use for an explicitly authorized test meeting. Audio Test joins temporarily.
Writes observed captions, word-error results and a round-trip voice check.
"""

from live_transcript.worker import guest_command, command as worker_command

import json
from pathlib import Path
from live_transcript.core.config import get, ssh_options

guest_home = get("vm", "home")
import re
import shlex
import socket
import subprocess
import threading
import time

ROOT = Path(get("paths", "root")) / "benchmarks" / "tts-test"


def words(text):
    return re.findall(r"[\wäöüß]+", text.casefold())


def errors(reference, hypothesis):
    a, b = words(reference), words(hypothesis)
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        new = [i]
        for j, y in enumerate(b, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (x != y)))
        row = new
    return dict(reference_words=len(a), edits=row[-1], word_error_rate=row[-1] / max(1, len(a)))


def deploy_test_bundle():
    from live_transcript.runtime.deployment import build_bundle
    import tempfile

    with tempfile.TemporaryDirectory(prefix="meeting-test-tools-") as directory:
        bundle = Path(directory) / "live-transcript-test.pyz"
        build_bundle(bundle, with_test_tools=True)
        target = get("vm", "ssh") + ":" + get("vm", "home") + "/live-transcript-test.pyz"
        subprocess.run(["scp", *ssh_options(), "-q", str(bundle), target], check=True, timeout=30)


def main():
    deploy_test_bundle()
    from live_transcript.transcription.store import CaptionStore

    meeting = json.loads((Path(get("paths", "logs")) / "current_meeting.json").read_text())
    if meeting.get("status") != "active":
        raise SystemExit("Join Live Transcript into an authorized test meeting first")
    join_log = Path(
        meeting.get(
            "control_log", str(Path(get("paths", "logs")) / ("join-" + meeting["run_id"] + ".log"))
        )
    ).read_text()
    session = Path(re.findall(r"RECORDING to (\S+)", join_log)[-1]).stem
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = ROOT / ("run-" + stamp)
    out.mkdir(parents=True)
    subprocess.run(
        [
            "ssh",
            *ssh_options(),
            get("vm", "ssh"),
            f"if [ -f {guest_home}/tts-test/spoken.jsonl ]; then "
            f"mv {guest_home}/tts-test/spoken.jsonl {guest_home}/tts-test/spoken.previous.jsonl; fi",
        ],
        check=True,
    )
    session_start = float(
        subprocess.check_output(
            ["ssh", *ssh_options(), get("vm", "ssh"), "cat /tmp/cap_session_start"], text=True
        )
    )
    stopped = threading.Event()
    events = []

    def observe():
        with open(out / "captions.jsonl", "w", buffering=1) as f:
            while not stopped.is_set():
                try:
                    with socket.create_connection(
                        (get("caption", "host"), get("caption", "port")), timeout=6
                    ) as sock:
                        sock.sendall(f"SUBCAP {session}\n".encode())
                        for raw in sock.makefile("rb"):
                            if stopped.is_set():
                                break
                            event = dict(received_at=time.time(), payload=json.loads(raw))
                            events.append(event)
                            f.write(json.dumps(event, ensure_ascii=False) + "\n")
                except Exception:
                    stopped.wait(0.3)

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    remote = (
        "export XDG_RUNTIME_DIR=/run/user/$(id -u) PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native; exec "
        + shlex.join(
            [
                "xvfb-run",
                "-a",
                "-s",
                "-screen 0 1280x800x24",
                *guest_command("test-sender", test=True),
                meeting["url"],
            ]
        )
    )
    voice_id = None
    voice_reply = None
    with open(out / "sender.log", "w", buffering=1) as log:
        proc = subprocess.Popen(
            ["ssh", *ssh_options(), get("vm", "ssh"), remote],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for line in proc.stdout:
                log.write(line)
                print(line.rstrip(), flush=True)
                if "TTS TEST COMPLETE" in line:
                    reply = subprocess.check_output(
                        worker_command(
                            "tts",
                            "--lang",
                            "en",
                            "The test is complete. We will keep the transcript in the original language, show summaries during pauses, and use a compact status line.",
                        ),
                        text=True,
                    )
                    voice_id = reply.strip().split()[-1]
                    print(reply.strip(), flush=True)
                if line.startswith("VOICE_REPLY "):
                    voice_reply = json.loads(line.split(" ", 1)[1])
            if proc.wait(timeout=10):
                raise RuntimeError("TTS participant failed; inspect sender.log")
        finally:
            stopped.set()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
    subprocess.run(
        [
            "scp",
            *ssh_options(),
            "-q",
            get("vm", "ssh") + ":" + guest_home + "/tts-test/spoken.jsonl",
            str(out / "spoken.jsonl"),
        ],
        check=True,
    )
    spoken = [json.loads(line) for line in (out / "spoken.jsonl").read_text().splitlines()]
    store = CaptionStore(str(Path(get("paths", "state")) / "captions.sqlite3"))
    transcript = [
        list(row)
        for row in store.transcript(session)
        if spoken[0]["started"] - session_start - 2
        <= row[0]
        <= spoken[-1]["ended"] - session_start + 3
    ]
    store.close()
    expected = " ".join(item["text"] for item in spoken)
    actual = " ".join(row[2] for row in transcript)
    latency = []
    stopwords = {
        "this",
        "is",
        "the",
        "and",
        "der",
        "die",
        "das",
        "ist",
        "und",
        "den",
        "will",
        "before",
    }
    for phrase in spoken:
        target = set(words(phrase["text"])) - stopwords
        for event in events:
            if event["received_at"] < phrase["started"]:
                continue
            if any(
                t >= phrase["started"] - session_start - 2
                and len(target.intersection(words(text))) >= 2
                for t, speaker, text in event["payload"]["lines"]
            ):
                latency.append(round(event["received_at"] - phrase["started"], 3))
                break
    voice = None
    if voice_id:
        raw = subprocess.check_output(
            [
                "ssh",
                *ssh_options(),
                get("vm", "ssh"),
                f"cat {guest_home}/voice_requests/done/" + voice_id + ".json",
            ],
            text=True,
        )
        voice = json.loads(raw)
    report = dict(
        session=session,
        expected=expected,
        actual=actual,
        cards=transcript,
        **errors(expected, actual),
        first_caption_seconds=latency,
        voice_command=voice,
        voice_received=voice_reply,
        notes="Six synthetic phrases in a quiet authorized two-bot room; not a human-speech benchmark.",
        provider="proton" if "meet.proton.me" in meeting["url"] else "zoom",
    )
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("REPORT", out / "report.json", flush=True)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "reference_words",
                    "edits",
                    "word_error_rate",
                    "first_caption_seconds",
                    "voice_received",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
