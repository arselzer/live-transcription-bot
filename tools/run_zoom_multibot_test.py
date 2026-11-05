#!/usr/bin/env python3
"""Authorized-room test: two named senders alternate, then overlap once."""

import argparse
import json
import wave
from pathlib import Path
import re
import shlex
import socket
import subprocess
import threading
import time

from live_transcript.transcription.store import CaptionStore
from live_transcript.core.config import get
from tools.benchmark_audio_conditions import transform
from live_transcript.capture.guard import atomic_json
from tools.run_tts_e2e import errors


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(Path(get("paths", "root")) / "test-assets/tts"))
    ap.add_argument(
        "--fault-gpu",
        action="store_true",
        help="explicitly stop/restart GPU service in this authorized room",
    )
    ap.add_argument("--audio-stress", action="store_true")
    a = ap.parse_args()
    current = json.loads((Path(get("paths", "logs")) / "current_meeting.json").read_text())
    if current.get("status") != "active":
        raise RuntimeError("Receiver must be in an authorized live test room")
    log = Path(current.get("control_log", f"/tmp/scribe_join.{current['pid']}.log")).read_text()
    session = Path(re.findall(r"RECORDING to (\S+)", log)[-1]).stem
    started = float(
        subprocess.check_output(["ssh", get("vm", "ssh"), "cat /tmp/cap_session_start"], text=True)
    )
    root = Path(a.root)
    out = root / time.strftime("multi-%Y%m%d-%H%M%S")
    out.mkdir()
    plan = json.loads((root / "plan.json").read_text())
    target = time.time() + 40
    events = []
    stop = threading.Event()

    def observe():
        with (
            socket.create_connection(
                (get("caption", "host"), get("caption", "port")), timeout=6
            ) as sock,
            open(out / "captions.jsonl", "w") as fh,
        ):
            sock.sendall(f"SUBCAP {session}\n".encode())
            for line in sock.makefile("rb"):
                if stop.is_set():
                    return
                item = dict(utc=time.time(), payload=json.loads(line))
                events.append(item)
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                fh.flush()

    threading.Thread(target=observe, daemon=True).start()
    processes = []
    faults = []

    def fault():
        if stop.wait(max(0, target - time.time() - 2)):
            return
        subprocess.run(
            ["systemctl", "--user", "stop", "whisper-stt.service"], check=True, timeout=40
        )
        faults.append(dict(utc=time.time(), event="GPU service stopped"))
        atomic_json(out / "faults.json", faults)
        stop.wait(45)
        subprocess.run(
            ["systemctl", "--user", "start", "whisper-stt.service"], check=True, timeout=40
        )
        faults.append(dict(utc=time.time(), event="GPU service restored"))
        atomic_json(out / "faults.json", faults)

    if a.fault_gpu:
        threading.Thread(target=fault, daemon=True).start()
    try:
        for language, ids, offset, port in [
            ("en", (1, 2, 3), 0, 9231),
            ("de", (4, 5, 6), 10, 9232),
        ]:
            directory = out / language
            directory.mkdir()
            phrases = [
                dict(p, start_offset=offset + i * 20)
                for i, p in enumerate(p for p in plan if p["id"] in ids)
            ]
            phrases.append(
                dict(phrases[0], id=7 if language == "en" else 8, start_offset=70, overlap=True)
            )
            if a.audio_stress:
                conditions = {
                    "en": ("quiet_20db", "loud_clipped", "noise_10db"),
                    "de": ("very_quiet_35db", "dropouts", "noise_10db"),
                }
                for index, phrase in enumerate(phrases[:3]):
                    phrase["condition"] = conditions[language][index]
            (directory / "plan.json").write_text(json.dumps(phrases, ensure_ascii=False))
            for phrase in phrases:
                target_file = directory / phrase["file"]
                if not target_file.exists():
                    if phrase.get("condition"):
                        with wave.open(str(root / phrase["file"])) as audio:
                            params = audio.getparams()
                            pcm = audio.readframes(audio.getnframes())
                        with wave.open(str(target_file), "wb") as audio:
                            audio.setparams(params)
                            audio.writeframes(transform(pcm, phrase["condition"]))
                    else:
                        target_file.write_bytes((root / phrase["file"]).read_bytes())
            remote = get("vm", "home") + "/" + out.name + "-" + language
            subprocess.run(["ssh", get("vm", "ssh"), "mkdir -p " + shlex.quote(remote)], check=True)
            subprocess.run(
                [
                    "scp",
                    "-q",
                    *[str(p) for p in directory.iterdir()],
                    get("vm", "ssh") + ":" + remote + "/",
                ],
                check=True,
            )
            name = "Audio Test · English" if language == "en" else "Audio Test · Deutsch"
            args = [
                "xvfb-run",
                "-a",
                "-s",
                "-screen 0 1280x800x24",
                "python3",
                "-u",
                get("vm", "home") + "/tts_test_bot.py",
                current["url"],
                "--directory",
                remote,
                "--name",
                name,
                "--port",
                str(port),
                "--pause",
                "0",
                "--start-at",
                str(target),
            ]
            command = (
                "export XDG_RUNTIME_DIR=/run/user/$(id -u) PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native; exec "
                + shlex.join(args)
            )
            fh = open(directory / "sender.log", "wb")
            proc = subprocess.Popen(
                ["ssh", get("vm", "ssh"), command], stdout=fh, stderr=subprocess.STDOUT
            )
            processes.append((language, name, remote, proc, fh))
        print(
            "Two Zoom TTS senders launched; alternating baseline followed by deliberate overlap",
            flush=True,
        )
        for language, name, remote, proc, fh in processes:
            code = proc.wait(timeout=180)
            fh.close()
            subprocess.run(
                [
                    "rsync",
                    "-rt",
                    "--include=failure.json",
                    "--include=admission.jpg",
                    "--exclude=*",
                    "-e",
                    "ssh -o ConnectTimeout=5",
                    get("vm", "ssh") + ":" + remote + "/",
                    str(out / language) + "/",
                ],
                capture_output=True,
                timeout=15,
            )
            if code:
                raise RuntimeError(
                    language + " sender failed; inspect " + str(out / language / "sender.log")
                )
            subprocess.run(
                [
                    "scp",
                    "-q",
                    get("vm", "ssh") + ":" + remote + "/spoken.jsonl",
                    str(out / language / "spoken.jsonl"),
                ],
                check=True,
            )
        time.sleep(5)
        store = CaptionStore(str(Path(get("paths", "state")) / "captions.sqlite3"))
        cards = list(store.transcript(session))
        store.close()
        results = []
        all_spoken = []
        for language, name, remote, proc, fh in processes:
            spoken = [
                json.loads(x) for x in (out / language / "spoken.jsonl").read_text().splitlines()
            ]
            all_spoken.extend(p for p in spoken if not p.get("overlap"))
            baseline = [p for p in spoken if not p.get("overlap")]
            selected = [
                row
                for row in cards
                if row[1] == name and target - started - 2 <= row[0] < target - started + 65
            ]
            expected = " ".join(p["text"] for p in baseline)
            actual = " ".join(row[2] for row in selected)
            results.append(
                dict(
                    language=language,
                    name=name,
                    expected=expected,
                    actual=actual,
                    cards=selected,
                    **errors(expected, actual),
                )
            )
        overlap = [row for row in cards if target - started + 65 <= row[0] <= target - started + 82]
        baseline = [row for row in cards if target - started - 2 <= row[0] < target - started + 65]
        expected = " ".join(p["text"] for p in sorted(all_spoken, key=lambda p: p["started"]))
        actual = " ".join(row[2] for row in baseline)
        report = dict(
            session=session,
            target=target,
            faults=faults,
            audio_stress=a.audio_stress,
            results=results,
            overlap_cards=overlap,
            baseline=dict(expected=expected, actual=actual, **errors(expected, actual)),
            notes="Known synthetic phrases through two real Zoom participants. Overlap is a stress test, excluded from baseline WER; speaker labels use Zoom active-speaker observations.",
        )
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("REPORT", out / "report.json", flush=True)
        print(json.dumps(results, ensure_ascii=False), flush=True)
    finally:
        stop.set()
        atomic_json(
            out / "run-state.json",
            dict(session=session, completed=(out / "report.json").exists(), faults=faults),
        )
        if a.fault_gpu:
            subprocess.run(
                ["systemctl", "--user", "start", "whisper-stt.service"], timeout=40, check=True
            )
        for _, _, _, proc, fh in processes:
            if proc.poll() is None:
                proc.terminate()
            fh.close()


if __name__ == "__main__":
    main()
