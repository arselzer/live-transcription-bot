"""Voice-based speaker diarization fused with DOM name observations.

Cluster voice turns by ECAPA embeddings so camera-off participants keep a
stable identity; DOM observations NAME clusters by majority vote instead of
labeling each utterance independently. Segmentation uses energy VAD on the
audio itself — never whisper timings (windows that clip to the next speaker
produce mixed embeddings and explode the cluster count).
Raw evidence is never modified.
"""

from live_transcript.core.config import get
import numpy as np

MODEL = get("paths", "diarization_model")
SR = 16000


def _load_wav16k(path):
    import subprocess, shutil

    ff = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    r = subprocess.run(
        [ff, "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True,
    )
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def vad_turns(
    audio, frame=0.03, silence_hold=0.6, min_turn=1.2, max_turn=8.0, rel=0.35, abs_floor=0.006
):
    """Energy VAD: voiced frames, merges gaps < silence_hold, splits long runs
    at the quietest frame so turn windows stay single-speaker."""
    flen = int(frame * SR)
    n = len(audio) // flen
    rms = np.sqrt((audio[: n * flen].reshape(n, flen) ** 2).mean(axis=1))
    thr = max(abs_floor, np.quantile(rms, 0.9) * rel)
    voiced = rms > thr
    turns, i = [], 0
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        s = i
        last_v = i
        while i < n and (voiced[i] or (i - last_v) * frame < silence_hold):
            if voiced[i]:
                last_v = i
            i += 1
        e = last_v + 1
        if (e - s) * frame >= min_turn:
            while (e - s) * frame > max_turn:
                seg = rms[s:e]
                q = s + int(np.argmin(seg))
                if (q - s) * frame < min_turn or (e - q) * frame < frame * 2:
                    q = s + int(max_turn * 0.5 / frame)  # force progress on flat runs
                if (q - s) * frame >= min_turn:
                    turns.append((s * frame, q * frame))
                s = q
            turns.append((s * frame, e * frame))
    return turns


def _embed_model():
    from speechbrain.inference.speaker import EncoderClassifier

    return EncoderClassifier.from_hparams(MODEL, savedir=MODEL, run_opts={"device": "cpu"})


def embed_turns(audio, turns, model=None):
    import torch

    model = model or _embed_model()
    rows, idx = [], []
    with torch.no_grad():
        for k, (a, b) in enumerate(turns):
            seg = audio[int(a * SR) : int(b * SR)]
            if len(seg) < int(0.8 * SR):
                continue
            emb = (
                model.encode_batch(torch.from_numpy(np.ascontiguousarray(seg)).unsqueeze(0))
                .squeeze()
                .numpy()
            )
            rows.append(emb / (np.linalg.norm(emb) + 1e-9))
            idx.append(k)
    return (np.stack(rows) if rows else np.zeros((0, 192))), idx


def cluster(embeddings, threshold=0.55):
    from sklearn.cluster import AgglomerativeClustering

    if len(embeddings) < 2:
        return list(range(len(embeddings)))
    k = AgglomerativeClustering(
        n_clusters=None, distance_threshold=threshold, metric="cosine", linkage="average"
    )
    return k.fit_predict(embeddings).tolist()


def name_turns(turn_labels, turns, dom_samples, pad=0.5):
    """Name each voice cluster by majority ACTIVE-speaker DOM vote during its spans.
    turn_labels: iterable of (label, (start, end)) pairs — one per turn.
    'tile' observations (always-visible participant tiles, incl. the bot itself)
    must not vote — they are presence, not speech."""
    from collections import Counter, defaultdict

    active = [d for d in dom_samples if d.get("role", "active") == "active"]
    spans = defaultdict(list)
    for lab, span in turn_labels:
        spans[lab].append(span)
    names, votes_by = {}, {}
    for k, sps in spans.items():
        votes = Counter()
        for d in active:
            t = d.get("t", 0)
            if any(a - pad <= t <= b + pad for a, b in sps):
                nm = d.get("name")
                if nm:
                    votes[nm] += 1
        names[k] = votes.most_common(1)[0][0] if votes else None
        votes_by[k] = dict(votes.most_common(3))
    return names, votes_by


def diarize_turns(wav_path, dom_samples, threshold=0.55, audio=None):
    """Returns (turns, labels, names, report). Pure function of audio + DOM log."""
    audio = _load_wav16k(wav_path) if audio is None else audio
    turns = vad_turns(audio)
    if len(turns) < 3:
        return turns, [], {}, {"voices": 0, "note": "not enough speech for diarization"}
    emb, idx = embed_turns(audio, turns)
    labs = cluster(emb, threshold)
    # merge singleton/dyad clusters into the nearest major voice centroid
    from collections import Counter as _C

    _sizes = _C(labs)
    keep = {c for c, n in _sizes.items() if n >= 3}
    if keep and len(keep) < len(_sizes):
        cents = {c: emb[[i for i, l in enumerate(labs) if l == c]].mean(0) for c in keep}
        labs = [
            c if c in keep else max(keep, key=lambda k: float(emb[i] @ cents[k]))
            for i, c in enumerate(labs)
        ]
    turn_labels = list(zip(labs, (turns[k] for k in idx)))
    names, votes = name_turns(turn_labels, turns, dom_samples)
    # map: per turn -> (label, name)
    lab_of = {k: l for k, l in zip(idx, labs)}
    per_turn = [(a, b, lab_of.get(k), names.get(lab_of.get(k))) for k, (a, b) in enumerate(turns)]
    report = {
        "voices": len(set(labs)),
        "cluster_sizes": {str(c): sum(1 for l in labs if l == c) for c in set(labs)},
        "cluster_names": {str(k): v for k, v in names.items()},
        "cluster_votes": {str(k): v for k, v in votes.items()},
    }
    return per_turn, labs, names, report


def diarize(wav_path, utterances, dom_samples, threshold=0.55):
    """Annotate whisper utterances by overlap with diarized turns. In place."""
    per_turn, _, _, report = diarize_turns(wav_path, dom_samples, threshold)
    for u in utterances:
        best, bo = None, 0.0
        us, ue = u["t"], u.get("end", u["t"] + 3)
        for a, b, lab, nm in per_turn:
            ov = min(ue, b) - max(us, a)
            if ov > bo:
                bo, best = ov, (lab, nm)
        if best and bo > 0.5:
            u["voice_cluster"], u["voice_name"] = best
        else:
            u["voice_cluster"] = u["voice_name"] = None
    return report
