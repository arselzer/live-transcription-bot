"""Collapse short-run repetition bleed in transcript utterances.

Whisper can emit the same phrase several times in a row at identical or
near-identical timestamps (observed in production 2026-11-06: 17 duplicate
cues inside a run that passed the long-collapse quality gate). Consecutive
near-identical phrases are recognition artifacts, not real speech: keep the
first occurrence and drop the rest. Only DIRECT neighbours are collapsed, so
legitimate later repetition elsewhere in the meeting survives.
"""

import re

NORMALIZE = re.compile(r"[^a-z0-9äöüß]+")


def normalize(text):
    return NORMALIZE.sub(" ", text.casefold()).strip()


def similar(a, b):
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def collapse(utterances, threshold=0.85):
    """Return (kept, dropped) for a time-ordered utterance list."""
    kept, dropped = [], []
    for utterance in utterances:
        if kept:
            a, b = normalize(kept[-1]["text"]), normalize(utterance["text"])
            if a and b and similar(a, b) >= threshold:
                dropped.append(utterance)
                continue
        kept.append(utterance)
    return kept, dropped
