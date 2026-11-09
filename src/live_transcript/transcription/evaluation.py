"""Reference-backed lab scores; overlap and uncertain timing stay explicit."""

from collections import Counter
import hashlib
import json
import math
import re
import statistics


def words(text):
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def errors(reference, hypothesis):
    """Case/punctuation normalized WER with substitution/deletion/insertion counts."""
    a, b = words(reference), words(hypothesis)
    if (len(a) + 1) * (len(b) + 1) > 4_000_000:
        raise ValueError("Reference comparison exceeds its work budget")
    directions = [bytearray(len(b) + 1) for _ in range(len(a) + 1)]
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        new = [i]
        for j, y in enumerate(b, 1):
            choices = (row[j - 1] + (x != y), row[j] + 1, new[-1] + 1)
            choice = min(range(3), key=choices.__getitem__)
            directions[i][j] = choice
            new.append(choices[choice])
        row = new
    i, j, substitutions, deletions, insertions = len(a), len(b), 0, 0, 0
    while i or j:
        choice = directions[i][j] if i and j else 1 if i else 2
        if choice == 0:
            substitutions += a[i - 1] != b[j - 1]
            i, j = i - 1, j - 1
        elif choice == 1:
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1
    return dict(
        reference_words=len(a),
        hypothesis_words=len(b),
        edits=row[-1],
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
        word_error_rate=row[-1] / len(a) if a else None,
    )


def validate_reference(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise ValueError("Reference requires 1–200 phrases")
    seen = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", row["id"])
            or row["id"] in seen
            or not isinstance(row.get("text"), str)
            or not 1 <= len(row["text"]) <= 1000
            or not words(row["text"])
            or row.get("language") not in ("en", "de")
            or not isinstance(row.get("speaker"), str)
            or not row["speaker"]
            or len(row["speaker"]) > 64
        ):
            raise ValueError("Invalid reference phrase")
        for key in ("start", "end"):
            if type(row.get(key)) not in (int, float) or not math.isfinite(row[key]):
                raise ValueError("Invalid reference timing")
        if not 0 <= row["start"] < row["end"] <= 1800:
            raise ValueError("Reference timing exceeds the lab budget")
        seen.add(row["id"])
    return sorted(rows, key=lambda r: (r["start"], r["id"]))


def signature(reference):
    stable = [
        {
            k: r.get(k)
            for k in (
                "id",
                "text",
                "language",
                "speaker",
                "condition",
                "audio_sha256",
                "offset",
                "duration",
            )
        }
        for r in validate_reference(reference)
    ]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def normalize_lines(rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("Transcript exceeds the lab budget")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid transcript line")
        start, end = row.get("t"), row.get("end", row.get("t"))
        if (
            type(start) not in (int, float)
            or type(end) not in (int, float)
            or not math.isfinite(start)
            or not math.isfinite(end)
            or not 0 <= start <= end
            or not isinstance(row.get("text"), str)
            or len(row["text"]) > 10000
        ):
            raise ValueError("Invalid transcript line")
        result.append(
            dict(t=start, end=end, text=row["text"], speaker=str(row.get("speaker") or "")[:100])
        )
    return sorted(result, key=lambda r: r["t"])


def percentile(values, fraction):
    values = sorted(values)
    return (
        values[min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))]
        if values
        else None
    )


def score(reference, transcript, *, caption_events=(), timing_tolerance=2.0):
    reference, transcript = validate_reference(reference), normalize_lines(transcript)
    expected = " ".join(r["text"] for r in reference)
    actual = " ".join(r["text"] for r in transcript)
    overlap = any(
        a["start"] < b["end"] and b["start"] < a["end"]
        for i, a in enumerate(reference)
        for b in reference[i + 1 :]
    )
    wanted, heard = Counter(words(expected)), Counter(words(actual))
    matched = sum((wanted & heard).values())
    totals = dict(
        expected_tokens=sum(wanted.values()),
        recognized_tokens=sum(heard.values()),
        token_recall=matched / sum(wanted.values()),
        token_precision=matched / sum(heard.values()) if heard else 0,
        unmatched_tokens=sum((heard - wanted).values()),
    )
    alignment = errors(expected, actual) if not overlap else None
    phrases, latency = [], []
    for phrase in reference:
        target = set(words(phrase["text"]))
        # Timestamp matching is approximate: some ASR segments span phrases.
        window = [
            r
            for r in transcript
            if r["t"] <= phrase["end"] + timing_tolerance
            and r["end"] >= phrase["start"] - timing_tolerance
        ]
        candidate = " ".join(r["text"] for r in window)
        coverage = len(target & set(words(candidate))) / len(target)
        first_caption = None
        for event in caption_events:
            at = event.get("received_t")
            if type(at) not in (int, float) or not math.isfinite(at) or at < phrase["start"]:
                continue
            for r in event.get("lines", []):
                if (
                    type(r.get("t")) in (int, float)
                    and abs(r["t"] - phrase["start"]) <= max(4, phrase["end"] - phrase["start"] + 2)
                    and len(target & set(words(str(r.get("text", ""))))) >= min(3, len(target))
                ):
                    first_caption = max(0, at - phrase["start"])
                    break
            if first_caption is not None:
                break
        if first_caption is not None:
            latency.append(first_caption)
        known = [
            r["speaker"]
            for r in window
            if r["speaker"] and len(target & set(words(r["text"]))) >= min(2, len(target))
        ]
        phrases.append(
            dict(
                id=phrase["id"],
                language=phrase["language"],
                condition=phrase.get("condition", "clean"),
                speaker=phrase["speaker"],
                token_coverage=coverage,
                possibly_missed=coverage < 0.35,
                first_caption_seconds=first_caption,
                speaker_observation="ambiguous-overlap"
                if overlap
                else "unknown"
                if not known
                else "correct"
                if all(s == phrase["speaker"] for s in known)
                else "mismatch",
            )
        )
    outside = [
        r
        for r in transcript
        if not any(
            r["t"] <= p["end"] + timing_tolerance and r["end"] >= p["start"] - timing_tolerance
            for p in reference
        )
    ]
    return dict(
        schema=1,
        reference_signature=signature(reference),
        overlap=overlap,
        wer=alignment,
        tokens=totals,
        phrases=phrases,
        possibly_missed_phrases=sum(p["possibly_missed"] for p in phrases),
        unreferenced_passages=outside,
        caption_latency=dict(
            count=len(latency),
            median=statistics.median(latency) if latency else None,
            p95=percentile(latency, 0.95),
        ),
        limitations=[
            "Synthetic speech; not a human-meeting accuracy claim.",
            "Phrase coverage, speaker windows and first-caption matching are timing heuristics.",
            "Overlap uses unordered token recall/precision; no ordered WER is reported.",
            "Unmatched words are not automatically hallucinations; retained audio needs review.",
        ],
    )


def compare(current, baseline, *, wer_tolerance=0.02, recall_tolerance=0.02):
    for value in (current, baseline):
        if (
            not isinstance(value, dict)
            or type(value.get("schema")) is not int
            or value.get("schema") != 1
            or type(value.get("overlap")) is not bool
            or not isinstance(value.get("reference_signature"), str)
            or not isinstance(value.get("tokens"), dict)
            or type(value.get("possibly_missed_phrases")) is not int
            or not 0 <= value["possibly_missed_phrases"] <= 200
        ):
            raise ValueError("Invalid regression score")
        recall = value["tokens"].get("token_recall")
        if type(recall) not in (int, float) or not math.isfinite(recall) or not 0 <= recall <= 1:
            raise ValueError("Invalid regression recall")
        if not value["overlap"]:
            rate = (
                value.get("wer", {}).get("word_error_rate")
                if isinstance(value.get("wer"), dict)
                else None
            )
            if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
                raise ValueError("Invalid regression WER")
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
        for v in (wer_tolerance, recall_tolerance)
    ):
        raise ValueError("Invalid regression tolerance")
    if (
        current.get("schema") != 1
        or baseline.get("schema") != 1
        or current.get("reference_signature") != baseline.get("reference_signature")
        or current.get("overlap") != baseline.get("overlap")
    ):
        raise ValueError("Baseline uses a different reference or report schema")
    regressions = []
    if current["wer"] is not None and baseline["wer"] is not None:
        if current["wer"]["word_error_rate"] > baseline["wer"]["word_error_rate"] + wer_tolerance:
            regressions.append("word_error_rate")
    if current["tokens"]["token_recall"] < baseline["tokens"]["token_recall"] - recall_tolerance:
        regressions.append("token_recall")
    if current["possibly_missed_phrases"] > baseline["possibly_missed_phrases"]:
        regressions.append("possibly_missed_phrases")
    return dict(
        ok=not regressions,
        regressions=regressions,
        policy="Accuracy gates; latency is reported separately and cannot excuse accuracy loss.",
    )


def markdown(report):
    out = [
        "# Transcription lab report",
        "",
        "| Scenario | Output | WER | Token recall | Possibly missed | Matched captions | Caption p95 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, case in report.get("scenarios", {}).items():
        for mode in ("live", "final"):
            value = case.get(mode)
            if not value:
                continue
            wer = value["wer"]["word_error_rate"] if value["wer"] else None
            latency = value["caption_latency"]["p95"]
            rate = format(wer, ".1%") if wer is not None else "n/a (overlap)"
            delay = format(latency, ".2f") + "s" if latency is not None else "n/a"
            coverage = (
                f"{value['caption_latency']['count']}/{len(value['phrases'])}"
                if mode == "live"
                else "n/a"
            )
            out.append(
                f"| {name} | {mode} | {rate} | {value['tokens']['token_recall']:.1%} | {value['possibly_missed_phrases']} | {coverage} | {delay} |"
            )
    for name, case in report.get("scenarios", {}).items():
        if case.get("recording"):
            out += [
                "",
                f"{name}: [recorded audio]({name}/{case['recording']}) · [reference]({name}/reference.json) · [live transcript]({name}/live-transcript.json)",
            ]
    for name, case in report.get("scenarios", {}).items():
        if not case.get("ok"):
            out += [
                "",
                f"Run {name}: {case.get('detail') or case.get('error') or 'accuracy regression; inspect JSON'}",
            ]
    out += [
        "",
        "Original recordings, exact sender WAVs, reference timings and raw reports remain beside this report.",
        "Overlap is scored separately. Timing and speaker observations are approximate; WER is case/punctuation normalized.",
    ]
    return "\n".join(out) + "\n"


def main():
    """Independent full-model final pass for a retained lab WAV."""
    import argparse
    import os
    from pathlib import Path
    import time
    from live_transcript.core.config import get
    from live_transcript.core.files import atomic_json
    from live_transcript.transcription.batch import transcribe_wav
    from live_transcript.transcription.speakers import load

    import signal

    def interrupted(*_):
        if os.getpgrp() == os.getpid():
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(os.getpgrp(), signal.SIGTERM)
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("wav")
    parser.add_argument("output")
    parser.add_argument("--speakers")
    args = parser.parse_args()
    wav = Path(args.wav)
    started = time.monotonic()
    lines, info = transcribe_wav(
        wav, env=dict(os.environ, HIP_VISIBLE_DEVICES=get("asr", "gpu_device"))
    )
    speaker_at, observations = load(
        Path(args.speakers) if args.speakers else wav.with_suffix(".archive") / "speakers.jsonl",
        wav.with_suffix(".voice.jsonl"),
    )
    for line in lines:
        line["speaker"] = speaker_at(line["t"]) or ""
    atomic_json(
        Path(args.output),
        dict(
            lines=lines,
            recognition_seconds=time.monotonic() - started,
            metadata=info,
            speaker_observations=observations,
        ),
    )


if __name__ == "__main__":
    main()
