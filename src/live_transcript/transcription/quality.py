"""Deterministic checks for recognizer failure, not a claim of transcription accuracy.

Repeated natural acknowledgements are common. Flag sustained low-diversity runs
only when longer repeated phrases dominate a window. Never delete suspected text:
retain candidates and original audio for review or independent re-decoding.
"""

from collections import Counter
import math
import re


def normalize(text):
    return " ".join(re.findall(r"\w+", str(text).casefold()))


def assess(
    utterances,
    duration=None,
    window=24,
    dominant_fraction=0.85,
    min_long_words=5,
    min_run_segments=12,
):
    if window < 12 or not 0.5 < dominant_fraction <= 1:
        raise ValueError("invalid transcript quality configuration")
    rows = []
    invalid = []
    for index, row in enumerate(utterances):
        try:
            start = float(row["t"])
            text = normalize(row["text"])
            if (
                not math.isfinite(start)
                or start < 0
                or (duration is not None and start > duration + 2)
            ):
                raise ValueError("invalid timestamp")
            if "end" in row:
                end = float(row["end"])
                if (
                    not math.isfinite(end)
                    or end < start
                    or (duration is not None and end > duration + 2)
                ):
                    raise ValueError("invalid segment end")
            if rows and start < rows[-1]["t"] - 0.2:
                raise ValueError("timestamps go backwards")
            rows.append(dict(index=index, t=start, text=text))
        except (KeyError, ValueError, TypeError):
            invalid.append(index)
    spans = []
    # Consecutive single-phrase looping can be shorter than a full window.
    begin = 0
    for index in range(1, len(rows) + 1):
        if index < len(rows) and rows[index]["text"] == rows[begin]["text"]:
            continue
        if index - begin >= min_run_segments and len(rows[begin]["text"].split()) >= min_long_words:
            spans.append(
                dict(
                    first=rows[begin]["index"],
                    last=rows[index - 1]["index"],
                    start=rows[begin]["t"],
                    end=rows[index - 1]["t"],
                    kind="repeated_phrase",
                )
            )
        begin = index
    # Also detect alternating loops which an adjacent-duplicate check misses.
    for index in range(max(0, len(rows) - window + 1)):
        part = rows[index : index + window]
        counts = Counter(r["text"] for r in part if r["text"])
        dominant = counts.most_common(3)
        repeated = sum(count for _, count in dominant)
        has_long = any(
            len(text.split()) >= min_long_words and count >= 5 for text, count in dominant
        )
        if has_long and repeated >= window * dominant_fraction:
            spans.append(
                dict(
                    first=part[0]["index"],
                    last=part[-1]["index"],
                    start=part[0]["t"],
                    end=part[-1]["t"],
                    kind="low_diversity_loop",
                )
            )
    merged = []
    for span in sorted(spans, key=lambda s: s["first"]):
        if merged and span["first"] <= merged[-1]["last"] + 1:
            merged[-1]["last"] = max(merged[-1]["last"], span["last"])
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
        else:
            merged.append(dict(span))
    suspect = set()
    for span in merged:
        suspect.update(range(span["first"], span["last"] + 1))
    issues = []
    if invalid:
        issues.append("invalid_segment_timestamps")
    if merged:
        issues.append("sustained_repetition")
    return dict(
        version=1,
        status="needs_review" if issues else "pass",
        issues=issues,
        segments=len(utterances),
        suspect_segments=len(suspect),
        suspect_fraction=round(len(suspect) / max(1, len(utterances)), 4),
        suspect_spans=merged,
        invalid_segments=invalid[:20],
        notes="Heuristic failure detection. Passing does not establish word accuracy; suspected text is not deleted.",
    )


class TranscriptQualityError(RuntimeError):
    def __init__(self, report):
        super().__init__("Transcript failed quality checks: " + ", ".join(report["issues"]))
        self.report = report


def require_acceptable(utterances, duration=None):
    report = assess(utterances, duration)
    if report["status"] != "pass":
        error = TranscriptQualityError(report)
        error.utterances = [dict(row) for row in utterances]
        raise error
    return report
