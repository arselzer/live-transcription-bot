#!/usr/bin/env python3
"""Actual CPU/GPU inference with injected primary failure, no meeting playback."""

import json
from pathlib import Path
from live_transcript.core.config import get
import time
import urllib.error
import live_transcript.transcription.daemon as captiond
from live_transcript.transcription.router import ASRRouter
from tools.run_tts_e2e import errors


def main():
    Path(get("paths", "logs")).mkdir(parents=True, exist_ok=True)
    root = Path(get("paths", "root")) / "benchmarks" / "tts-test"
    phrases = json.loads((root / "plan.json").read_text())
    router = ASRRouter()
    rows = []
    unavailable = True

    def request(path, language, prompt, **kwargs):
        if "endpoint" not in kwargs and unavailable:
            raise urllib.error.URLError("injected GPU outage")
        return captiond.stt_server(path, language, prompt, **kwargs)

    for number in (1, 4, 1):
        phrase = next(p for p in phrases if p["id"] == number)
        if len(rows) == 1:
            router.note_backlog(25)
        if len(rows) == 2:
            unavailable = False
            router.primary_retry_at = 0
        started = time.monotonic()
        result = router.transcribe(
            request,
            str(root / (f"{number:02d}-" + phrase["language"] + "-16k.wav")),
            phrase["language"],
            "",
            phrase["duration"],
        )
        actual = " ".join(text for _, text in result) if result is not None else ""
        row = dict(
            stage=("cpu-large", "adaptive-cpu-small", "gpu-recovery")[len(rows)],
            expected=phrase["text"],
            actual=actual,
            seconds=time.monotonic() - started,
            health=dict(router.health),
            **errors(phrase["text"], actual),
        )
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        (Path(get("paths", "logs")) / "asr-failover.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2)
        )
    assert [r["health"]["engine"] for r in rows] == ["cpu-large", "cpu-small", "gpu-large"]
    assert all(r["edits"] == 0 for r in rows), "inspect actual recognition errors"


if __name__ == "__main__":
    main()
