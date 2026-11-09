#!/usr/bin/env python3
"""Live Transcript caption daemon (host side).
TCP :8099 on the libvirt bridge. One connection per message, first line = command:
  CHUNK <session> <speaker> [t=name;t=name]\n<wav bytes>  -> ingest audio, reply "OK\n"
  GETCAP <session>\n   -> last <=5 lines as "speaker\\ttext\\n"
  GETTODO <session>\n  -> latest one-line action-items ticker
  GETSUM <session>\n   -> two-sentence live summary (Qwen, on demand)
  GETGPU\n             -> json {"util":int,"watts":float,"temp":float,"stt_wps":float}
  RESET <session>\n    -> clear session, reply "OK\n"
STT: whisper.cpp HIP on GPU (HIP_VISIBLE_DEVICES=0), large-v3 + VAD.
Extras: GPU stats via rocm-smi (5 s cache); action-item ticker via Qwen :8078 every 150 s.
"""

import os, socket, subprocess, tempfile, threading, time, wave, io, re, json, urllib.request, sys, uuid
from difflib import SequenceMatcher
from live_transcript.core.config import get

PORT = int(os.environ.get("CAPTION_PORT", str(get("caption", "port"))))
BIND = os.environ.get("CAPTION_BIND", get("caption", "bind"))
RUNTIME = None
# COMMIT_ONCE: long-sequence architecture (2026-11-03 redesign). Audio is decoded
# ONCE: after each pass all but the last OVERLAP_S seconds are trimmed, and the
# overlap re-read is stripped against the committed text before appending.
# Prefix stability measured 100% on recorded meetings -> the merge ladder
# (_merge/_infix/_fuzzy) existed only to undo stateless-window re-decode damage.
# Env kill-switch: COMMIT_ONCE=0 restores the rolling-window path.
COMMIT_ONCE = os.environ.get("COMMIT_ONCE", str(int(get("caption", "commit_once")))) == "1"
OVERLAP_S = 2.5
CAP_FILE = os.environ.get("CAPTION_CAP_FILE", "/tmp/live-transcript-captions.tsv")
API = get("model", "url")
WINDOW = 8.0
RUN_EVERY = get("caption", "run_every")
SILENCE_FINALIZE = get("caption", "silence_finalize")
SILENCE_RMS = get("caption", "silence_rms")
# Whisper on silence = hallucinated words (classic); the
# ONLY reliable gate is energy — never decode silence.
TICK_EVERY = 150.0
LOCK = (
    threading.RLock()
)  # REENTRANT: CHUNK worker holds it across transcribe(), which re-acquires for stt_wps
STATE = {}  # session -> {"pcm","pcm_t0","dur","emitted","lines":[(t_abs,sp,txt)],"last_run","timeline":[(t,name)]}
SPEAKER = {}
GPU = {"util": 0, "watts": 0.0, "temp": 0.0, "stt_wps": 0.0, "t": 0.0}
TODOS = {}


def _rms(pcm: bytes) -> float:
    mv = memoryview(pcm).cast("h")
    step = max(1, len(mv) // 8000)
    acc = 0
    n = 0
    for i in range(0, len(mv), step):
        v = mv[i]
        acc += v * v
        n += 1
    return (acc / max(1, n)) ** 0.5


def wav_to_pcm(data: bytes) -> bytes:
    """Strict: only 16k mono int16 enters the window — anything else concatenated
    in there shifts/speeds the audio and whisper emits garbage."""
    try:
        w = wave.open(io.BytesIO(data))
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
            print(
                f"[cap] REJECT wav fmt {w.getnchannels()}ch/{w.getsampwidth()}B/{w.getframerate()}Hz",
                flush=True,
            )
            return b""
        d = w.readframes(w.getnframes())
        w.close()
        return d
    except Exception:
        return b""


def qwen(sysmsg, user, maxtok=200):
    if not get("model", "enabled"):
        raise RuntimeError("Meeting notes model is disabled")
    body = json.dumps(
        {
            "model": get("model", "name"),
            "temperature": 0.2,
            "max_tokens": maxtok,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}],
        }
    ).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"][
        "content"
    ].strip()


def gpu_poller():
    while True:
        try:
            r = subprocess.run(
                ["rocm-smi", "--showproductname", "-u", "-P", "-t", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            j = json.loads(r.stdout)
            d = j.get(get("asr", "metrics_device"))
            if not d:
                raise ValueError("configured GPU absent from metrics")
            util = float(d["GPU use (%)"])
            watts = float(d["Average Graphics Package Power (W)"])
            temp = float(d["Temperature (Sensor edge) (C)"])
            with LOCK:
                GPU.update({"util": int(util), "watts": watts, "temp": temp, "t": time.time()})
        except Exception:
            pass
        time.sleep(5)


def backend_poller():
    from live_transcript.transcription.service import readiness

    while True:
        results = readiness()
        with LOCK:
            GPU["backends"] = results
            GPU["backends_checked_at"] = time.time()
        time.sleep(10)


BAD_LANGS = re.compile(r"[\u0400-\u04FF\u0370-\u03FF\u4E00-\u9FFF\u3040-\u30FF\u0590-\u05FF]")
HALLUC = re.compile(
    r"(subtitles by|music by|copyright|\[music\]|\[applause\]|amara\.org|\[silence\])", re.I
)
# bilingual scorer: each candidate decode is credited only for words that belong
# to ITS language — the fair comparison auto-detect wishes it had.
DE_WORDS = set(
    """der die das den dem des ein eine einen einem einer und oder aber wenn dann als wie dass damit weil obwohl
ist sind war waren sein gewesen haben hat hatte hast du dich dir ich mich mir mein meine dein deine seine ihre unser euer
nicht nichts ja nein doch vielleicht auch noch schon nur sehr gut gute guten schlecht heute morgen gestern abend nacht zeit
jahr jahre tag uhr mann frau leute kinder kind geld arbeiten machen macht machte ging gehen kann können konnten muss musste
soll sollte will werden wird wurde wir ihr sie es er ihnen hier dort da oben unten bisschen eigentlich genau klar hallo
tschüss danke vielen dank bitte gerne leid spaß wahr wirklich eigentlich natürlich zusammen wieder weiter neue neuen alt
alte kleine große viel viele etwas nichts alles man demensprechend trotzdem übrigens quasi so halt grad schaun schau
anschauen reden redet reden sag sagt sagen weiss wissen denk denkt glaube komm kommst geh gehst machst tust bist bin hab
hast warst hast du wir uns euch ihnen dessen denen deren welchem jeder jede jedes alle allem allen hinter vor ohne über
unter zwischen bei mit nach von um an auf für""".split()
)
EN_WORDS = set(
    """the and is are was were be been being a an of in on at to for with from by that this these those it its
i you your yours we they he she me him her us my our their them what when where who whom which why how yes no not nothing
something everything please thanks thank do does did done have has had will would can could shall should may might must go
going get got make made like just now then there here good bad time times year years day days night nights man woman people
work working money very really okay hello bye sorry sure course kind things thing say says said think thinks know knows
want need needs new old big small much many more most other another same first last next right left end start""".split()
)


def lang_hits(text):
    toks = re.findall(r"[a-zäöüß]+", text.lower())
    return sum(t in DE_WORDS for t in toks), sum(t in EN_WORDS for t in toks)


from live_transcript.transcription.router import ASRRouter

ASR_ROUTER = ASRRouter()
DECODE_TIMEOUT = max(30, 8 * get("asr", "cpu_timeout") + 4 * get("asr", "gpu_timeout") + 15)


def transcribe(pcm: bytes, lang: str = "", prompt: str = ""):
    if len(pcm) < 16000:
        return []
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        with wave.open(f, "w") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(pcm)
        path = f.name
    try:
        started = time.monotonic()
        output = ASR_ROUTER.transcribe(stt_server, path, lang, prompt, len(pcm) / 32000)
        with LOCK:
            GPU["asr"] = dict(ASR_ROUTER.health)
            GPU["stt_err"] = ASR_ROUTER.health.get("error", "")
            if output is not None:
                words = sum(len(text.split()) for _, text in output)
                rate = words / max(0.5, time.monotonic() - started)
                GPU["stt_wps"] = round(0.7 * GPU["stt_wps"] + 0.3 * rate, 1)
        return output
    except Exception as exc:
        print("[cap] ASR unavailable; audio retained:", type(exc).__name__, flush=True)
        return None
    finally:
        os.unlink(path)


STT_URL = os.environ.get("CAPTION_STT_URL", get("caption", "stt_url"))


class ASRSegments(list):
    def __init__(self, segments=(), language_probs=None):
        super().__init__(segments)
        self.language_probs = language_probs or {}
        self.segment_ends = {}


def stt_server(path, lang="", prompt="", endpoint=None, timeout=12):
    """Return filtered segments and metadata; propagate backend failures."""
    import urllib.request
    import urllib.error
    from pathlib import Path

    # A known managed engine must match its current profile before it receives
    # speech. External servers remain supported, with explicitly unverified
    # provenance in readiness rather than inferred hardware claims.
    device = (
        "gpu"
        if endpoint is None
        else "cpu-small"
        if endpoint == get("asr", "small_cpu_url")
        else "cpu-large"
    )
    if (Path(get("paths", "state")) / f"asr-{device}.json").exists():
        from live_transcript.transcription.service import probe

        status = probe(endpoint or STT_URL, device, timeout=min(3, timeout))
        if not status["ready"]:
            raise RuntimeError(status["detail"])

    boundary = "----capt" + str(int(time.time() * 1000))
    with open(path, "rb") as fh:
        data = fh.read()
    fields = [
        ("response_format", "verbose_json"),
        ("translate", "false"),
        ("detect_language", "false"),
        ("no_language_probabilities", "false"),
    ]
    if lang:
        fields.append(("language", lang))  # pin: auto-detect on 9 s windows misfires
    if prompt:
        fields.append(("prompt", prompt))  # initial_prompt: names/jargon/recent context

    def post(include_probabilities, budget):
        body = b""
        for k, v in fields:
            if k == "no_language_probabilities" and not include_probabilities:
                v = "true"
            body += (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
            ).encode()
        body += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n'
            ).encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        req = urllib.request.Request(
            endpoint or STT_URL,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=budget) as resp:
            return json.loads(resp.read().decode())

    started = time.monotonic()
    metadata_fallback = False
    try:
        d = post(True, timeout)
    except urllib.error.HTTPError as exc:
        # whisper.cpp can finish VAD with no speech, then throw while building
        # optional language probabilities for an invalid detected language ID.
        # Retry the same audio without that metadata; never turn an HTTP error
        # into an assumed empty transcript or relax recognition thresholds.
        if exc.code != 500 or b"basic_string: construction from null" not in exc.read(4096):
            raise
        exc.close()
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise
        d = post(False, remaining)
        metadata_fallback = True
    if os.environ.get("CAPTION_DIAGNOSTIC_DIR"):
        import shutil

        root = Path(os.environ["CAPTION_DIAGNOSTIC_DIR"])
        root.mkdir(exist_ok=True, parents=True, mode=0o700)
        name = str(time.time_ns()) + "-" + lang
        (root / (name + ".json")).write_text(json.dumps(d, ensure_ascii=False))
        shutil.copyfile(path, root / (name + ".wav"))
    out = ASRSegments(language_probs=d.get("language_probabilities", {}))
    out.language_metadata_fallback = metadata_fallback
    pcm = None
    sample_rate = 0
    for seg in d.get("segments", []):
        txt = (seg.get("text") or "").strip()
        if len(txt) <= 2:
            continue
        # Whisper's own confidence: low avg_logprob / high no_speech_prob = guess.
        # Dropping these kills the mid-word-cut garbage short windows produce.
        confidence = float(seg.get("avg_logprob", 0) or 0)
        if confidence < -0.95:  # tightened: the wrong-language
            continue  # guess on ambiguous audio lands
            # around -1.0 and reached the board
        if float(seg.get("no_speech_prob", 0) or 0) > 0.50:
            # Whisper can attach a high no-speech score to a clearly spoken,
            # high-confidence first segment after leading silence. Do not erase
            # it solely on that score: require exceptionally strong text
            # confidence AND actual voiced samples within this segment.
            if confidence < -0.2:
                continue
            if pcm is None:
                with wave.open(io.BytesIO(data), "rb") as audio:
                    sample_rate = audio.getframerate()
                    pcm = (
                        audio.readframes(audio.getnframes())
                        if (audio.getnchannels(), audio.getsampwidth()) == (1, 2)
                        else b""
                    )
            begin = max(0, int(float(seg.get("start", 0)) * sample_rate)) * 2
            finish = max(begin, int(float(seg.get("end", 0)) * sample_rate) * 2)
            voiced = pcm[begin:finish]
            if len(voiced) < sample_rate * 0.3 * 2 or _rms(voiced) < max(330, SILENCE_RMS * 1.5):
                continue
        start = float(seg.get("start", 0))
        out.append((start, txt))
        out.segment_ends[(start, txt)] = float(seg.get("end", start))
    return out


def spk_at(timeline, t):
    best = ""
    for ts, nm in timeline:
        if ts <= t:
            best = nm
    return best


def spk_between(timeline, start, end):
    """Choose the speaker covering the recognized segment, tolerating DOM lag."""
    if end <= start:
        return spk_at(timeline, start)
    events = [(start, spk_at(timeline, start))]
    events.extend((t, name) for t, name in timeline if start < t < end)
    events.append((end, ""))
    totals = {}
    for (t, name), (next_t, _) in zip(events, events[1:]):
        if name:
            totals[name] = totals.get(name, 0) + next_t - t
    return max(totals, key=totals.get) if totals else ""


def segment_speaker(timeline, start, end):
    """Label settled text conservatively across an actual speaker transition."""
    if end <= start:
        return spk_at(timeline, start)
    events = [(start, spk_at(timeline, start))]
    events.extend((at, name) for at, name in timeline if start < at < end)
    events.append((end, ""))
    totals = {}
    for (at, name), (next_at, _) in zip(events, events[1:]):
        if name:
            totals[name] = totals.get(name, 0) + next_at - at
    if not totals:
        return ""
    winner = max(totals, key=totals.get)
    if totals[winner] >= 0.75 * sum(totals.values()):
        return winner
    return " / ".join(name for name, seconds in totals.items() if seconds >= 0.15)


def _norm(s):
    return re.sub(r"\s+", " ", s.lower().strip(" .,!?;:'\"")).strip()


def _toks(s):
    return [w.strip(".,!?;:\"'()") for w in _norm(s).split()]


def _merge(old, new):
    """Rolling-window STT re-reads the last seconds each pass: if `new` is the
    same utterance grown (word-suffix/prefix overlap), return the merged text;
    None means it is a genuinely different utterance."""
    ow, nw = _toks(old), _toks(new)
    if not ow or not nw:
        return None
    for k in range(min(len(ow), len(nw)), 0, -1):
        if ow[-k:] == nw[:k]:
            return (old.strip() + " " + " ".join(nw[k:])).strip()
    if _norm(new) and _norm(new) in _norm(old):  # regression (shorter re-read)
        return old.strip()
    return None


def _overlap_frac(old, new):
    """Fraction of `new` covered by a >=2-word suffix/prefix overlap with old.
    ASR jitter ("der Typ" -> "der Tinker") breaks _merge's exact equality; this
    catches near-identical rolling-window re-reads so they never become cards."""
    ow, nw = _toks(old), _toks(new)
    if not ow or not nw:
        return 0.0
    for k in range(min(len(ow), len(nw)), 1, -1):
        if ow[-k:] == nw[:k]:
            return k / len(nw)
    return 0.0


def _agree(a, b):
    """Count of leading words both texts agree on (punctuation-insensitive)."""
    aw, bw = a.split(), b.split()
    n = 0
    while n < min(len(aw), len(bw)) and aw[n].strip(".,!?;:\"'()") == bw[n].strip(".,!?;:\"'()"):
        n += 1
    return n


def _finalize(st):
    """Publish the unconfirmed tail of the growing card and close it out."""
    cand = st.pop("cand", None)
    if cand and st["lines"] and st["lines"][-1][1] == cand[0]:
        if cand[1] != st["lines"][-1][2]:
            st["lines"][-1] = (st["lines"][-1][0], cand[0], cand[1])
            return True
    return False


def _infix_merge(prev, txt):
    """Re-read that DROPPED leading words (rolling window slid past them while
    the utterance kept going). Suffix-overlap _merge can't align these, so the
    card fractured into duplicates. Find where txt starts inside prev; if the
    match runs to prev's end, append txt's new tail."""
    pw = [w.lower().strip(".,!?;:") for w in prev.split()]
    raw = txt.split()
    tw = [w.lower().strip(".,!?;:") for w in raw]
    n = len(pw)
    if not pw or not tw:
        return None
    for off in range(0, n):
        m = 0
        while m < len(tw) and off + m < n and tw[m] == pw[off + m]:
            m += 1
        if m >= 3 and off + m == n:  # matched to prev's end = new tail
            return prev + " " + " ".join(raw[m:]) if len(tw) > m else None
    return None


def _fuzzy_merge(prev, txt):
    """Slightly-disagreeing re-reads (same words, one-two decode differences on
    shifted windows) pass every exact-match gate and surface as partially
    duplicated cards. SequenceMatcher alignment: >=0.55 token ratio = re-read.
    New tail after the last matching block gets appended; otherwise keep the
    richer of the two versions."""
    pw = prev.split()
    tw = txt.split()
    pt = [w.lower().strip(".,!?;:") for w in pw]
    tt = [w.lower().strip(".,!?;:") for w in tw]
    sm = SequenceMatcher(None, pt, tt)
    if sm.ratio() < 0.55:
        return None
    blocks = sm.get_matching_blocks()
    last = blocks[-2] if len(blocks) >= 2 else None  # [-1] is the sentinel
    if last and len(tw) - (last.b + last.size) >= 3:  # real new tail
        return prev + " " + " ".join(tw[last.b + last.size :])
    return prev if len(pw) >= len(tw) else txt  # same content: keep richer


def _strip_lead_replay(lines, txt):
    """Card opening with the previous card's closing words = re-read/echo, not
    new speech. Strips up to 12 leading tokens that replay the last card's tail."""
    if not (lines and lines[-1][2] and txt):
        return txt
    prev = _toks(lines[-1][2])
    cur = _toks(txt)
    raw = txt.split()
    for k in range(min(12, len(prev), len(cur)), 1, -1):
        if [w.lower().strip(".,!?;:") for w in prev[-k:]] == [
            w.lower().strip(".,!?;:") for w in cur[:k]
        ]:
            return " ".join(raw[k:]) if len(raw) >= k else txt
    return txt


def _strip_anchor(anchor, txt):
    """COMMIT_ONCE: `anchor` is the last committed words whose audio was
    deliberately re-decoded for continuity (the overlap). Return ONLY the words
    the decode produced AFTER the anchor — the new commit."""
    aw = [w.lower().strip(".,!?;:") for w in anchor.split()]
    raw = txt.split()
    tw = [w.lower().strip(".,!?;:") for w in raw]
    if not tw:
        return ""
    if not aw:
        return txt
    sm = SequenceMatcher(None, aw, tw)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 2]
    if blocks:
        b = blocks[-1]
        return " ".join(raw[b.b + b.size :])  # everything after the anchor re-read
    if sm.ratio() > 0.6:
        return ""  # pure prompt echo, no new speech
    return txt  # genuinely new (anchor decoded differently)


def after_stable(stable, hypothesis):
    """Align against published words, never against the still-revisable tail."""
    old, new = _toks(stable)[-8:], _toks(hypothesis)
    raw = hypothesis.split()
    for count in range(min(len(old), len(new)), 0, -1):
        if count == 1 and len(old[-1]) < 4:
            continue
        for pos in range(min(len(new) - count + 1, 10)):
            if old[-count:] == new[pos : pos + count]:
                return " ".join(raw[pos + count :])
    return hypothesis


def trim_lines(st, limit=3000):
    excess = len(st["lines"]) - limit
    if excess > 0:
        del st["lines"][:excess]
        if "utterance_mark" in st:
            st["utterance_mark"] = max(0, st["utterance_mark"] - excess)


def commit_apply(st, sp, t_abs, txt):
    """Freeze published text; replace the provisional tail on every reread.

    The former append-only candidate made temporary guesses permanent even
    before they were visible ("Wilson" could never become "will send").
    """
    lines = st["lines"]
    cand = st.get("cand")
    if cand and cand[0] != sp:
        _finalize(st)
        cand = None
    if not _norm(txt):
        return False
    if not cand:
        lines.append((t_abs, sp, ""))
        trim_lines(st)
        stable = ""
        candidate = txt.strip()
    else:
        stable = lines[-1][2]
        tail = after_stable(stable, txt) if stable else txt.strip()
        if not tail:
            return False
        candidate = (stable + " " + tail).strip()
        voice_at = st.get("input_voice_at", st.get("last_voice_at", 0))
        if len(candidate.split()) < len(cand[1].split()) and voice_at <= st.get(
            "hypothesis_voice_at", -1
        ):
            return False  # a quiet reread must not erase the richer provisional tail
        if len(stable) > 240:
            # Transfer the provisional tail to the next card; do not publish it
            # on both sides of a card boundary.
            lines.append((t_abs, sp, ""))
            trim_lines(st)
            stable, candidate = "", tail
    st["cand"] = (sp, candidate)
    st["hypothesis_voice_at"] = st.get("input_voice_at", st.get("last_voice_at", 0))
    words = candidate.split()
    margin = int(os.environ.get("CAPTION_LIVE_MARGIN", "3"))
    pub = " ".join(words[:-margin]) if len(words) > margin else ""
    if pub and len(pub) >= len(stable) and pub != stable:
        lines[-1] = (lines[-1][0], sp, pub)
        return True
    return False


def grow_or_append(st, sp, t_abs, txt):
    """Local-agreement-2 streaming captions (whisper_streaming policy): keep a
    private candidate `cand` per pass; PUBLISH only the words two consecutive
    passes agree on, so mid-sentence AST revisions never flicker on screen."""
    lines = st["lines"]
    cand = st.get("cand")
    if cand and cand[0] == sp:
        merged = _merge(cand[1], txt) or _infix_merge(cand[1], txt) or _fuzzy_merge(cand[1], txt)
        if merged is None and _overlap_frac(cand[1], txt) >= 0.5:
            return False  # re-read noise while growing: keep candidate
        if merged is not None:
            if len(merged) > 240:  # monologue guard: roll to a new card
                _finalize(st)
            else:
                st["cand"] = (sp, merged)
                # Relaxed LA: show live edge minus the last 2 words (those are
                # mid-utterance and get revised). Full LA-2 cost a whole pass of
                # latency; decodes are stable now (context prompt + single-flight).
                words = merged.split()
                pub = " ".join(words[:-2]) if len(words) > 4 else ""
                if pub and pub != (lines[-1][2] if lines else ""):
                    lines[-1] = (lines[-1][0], sp, pub)
                    return True
                return False
        _finalize(st)  # different utterance: publish tail
    elif cand:
        _finalize(st)  # speaker switch
    txt = _strip_lead_replay(lines, txt)
    n = _norm(txt)
    if not n or any(_norm(p) == n for _, _, p in lines[-6:]):
        return True  # finalize above may have changed a card
    # Rolling window re-reads the tail of a just-closed utterance. Near-identical
    # (ASR jitter defeats exact dedup) -> fold into the last card, never a new one.
    if lines and (lines[-1][1] == sp or not lines[-1][1]):
        m = (
            _merge(lines[-1][2], txt)
            or _infix_merge(lines[-1][2], txt)
            or _fuzzy_merge(lines[-1][2], txt)
        )
        if m is not None and (
            _overlap_frac(lines[-1][2], txt) >= 0.4 or len(_toks(txt)) > len(_toks(lines[-1][2]))
        ):
            if m != lines[-1][2]:
                _fw = m.split()
                lines[-1] = (
                    lines[-1][0],
                    sp or lines[-1][1],
                    " ".join(_fw[:-2]) if len(_fw) > 4 else m,
                )
                st["cand"] = (sp or lines[-1][1], m)
                return True
            return False
    lines.append((t_abs, sp, ""))  # speaking-now card, fills in next pass
    trim_lines(st)
    st["cand"] = (sp, txt)
    return True


def write_caps(session):
    if session not in STATE:
        return  # GC'd mid-pass: writing here blanks the board
    st = STATE[session]
    try:
        tmp = f"{CAP_FILE}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w") as fh:
            for _, sp, tx in st["lines"][-12:]:
                fh.write(f"{sp}\t{tx}\n")
        os.replace(tmp, CAP_FILE)  # atomic: readers never see a half-written board
    except Exception:
        pass


TTS_DIR = get("paths", "tts")
TTS_VOICE = {"de": "de_DE-thorsten_medium", "en": "en_US-lessac_medium"}


def tts_wav(text, lang="de"):
    """Piper TTS -> wav bytes (cached per text+lang). Empty bytes if unavailable."""
    text = (text or "").strip()
    if not text or not os.path.exists(f"{TTS_DIR}/venv/bin/piper"):
        return b""
    import hashlib

    key = hashlib.md5(f"{lang}|{text}".encode()).hexdigest()[:16]
    out = f"/tmp/tts_{key}.wav"
    try:
        if not os.path.exists(out):
            model = f"{TTS_DIR}/voices/{TTS_VOICE.get(lang, 'de')}.onnx"
            tmp = out + "." + uuid.uuid4().hex + ".wav"
            r = subprocess.run(
                [f"{TTS_DIR}/venv/bin/piper", "-m", model, "-f", tmp],
                input=(text + "\n").encode(),
                capture_output=True,
                timeout=90,
            )
            if r.returncode != 0:
                print(f"[cap] tts err: {r.stderr.decode(errors='replace')[:200]}", flush=True)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return b""
            with wave.open(tmp) as audio:
                if audio.getnframes() == 0:
                    raise ValueError("synthesizer returned empty audio")
            os.replace(tmp, out)
        return open(out, "rb").read()
    except Exception as e:
        print(f"[cap] tts fail {e}", flush=True)
        return b""


def session_transcript(st, n=40):
    return "\n".join(f"{sp + ': ' if sp else ''}{tx}" for _, sp, tx in st.get("lines", [])[-n:])


def ticker():
    """Every TICK_EVERY: distill current action items into a one-line strip; GC idle sessions."""
    while True:
        time.sleep(TICK_EVERY)
        with LOCK:  # GC pops MUST be locked: GETGPU iterates STATE; and a pop
            for sess, st in list(STATE.items()):  # mid-pass truncates CAP_FILE to empty
                if st.get("busy"):
                    continue
                if time.time() - max(st.get("created", 0), st.get("last_chunk", 0)) > 3 * 3600:
                    STATE.pop(sess, None)
                    SPEAKER.pop(sess, None)
                    TODOS.pop(sess, None)
        with LOCK:  # no more chunks + open candidate -> finalize so the live-edge
            for session, st in list(STATE.items()):  # tail-3 isn't stranded invisible
                if (
                    st.get("cand")
                    and not st.get("busy")
                    and time.time() - st.get("last_chunk", 0) > SILENCE_FINALIZE + 2
                ):
                    if _finalize(st):
                        if RUNTIME:
                            RUNTIME.publish(session, st)
                        else:
                            write_caps(session)
        for session, st in list(STATE.items()):
            if not st.get("lines") or time.time() - st.get("last_run", 0) > 300:
                continue
            # Reuse the evidence-checked board, whose inputs exclude the TTS
            # test participant. A second free-form extraction invented tasks
            # from example sentences and left them in the footer indefinitely.
            board = st.get("board", {})
            fresh = time.time() - board.get("updated_at", 0) < 600
            TODOS[session] = (
                "; ".join(board.get("bullets", []))[:140]
                if fresh and board.get("kind") == "action"
                else ""
            )


def worker(conn):
    try:
        conn.settimeout(30)  # a stalled client must not park a thread forever
        f = conn.makefile("rb")
        hdr_line = f.readline(65536)
        if len(hdr_line) >= 65536:  # garbage/no-newline flood: refuse, don't buffer
            conn.sendall(b"ERR\n")
            return
        hdr = hdr_line.decode(errors="replace").rstrip("\n").split(" ", 3)
        cmd = hdr[0] if hdr else ""
        session = hdr[1] if len(hdr) > 1 else "m"
        if cmd == "STREAM2":
            conn.sendall(b"OK\n")
            while True:
                packet_line = f.readline(65536)
                if not packet_line:
                    break
                if len(packet_line) >= 65536:
                    raise ValueError("metadata too large")
                meta = json.loads(packet_line)
                size = int(meta.pop("size"))
                if size < 44 or size > 1024 * 1024:
                    raise ValueError("invalid audio packet size")
                body = f.read(size)
                if len(body) != size:
                    raise ValueError("truncated audio packet")
                pcm = wav_to_pcm(body)
                RUNTIME.accept(session, meta, pcm)
                conn.sendall(b"OK\n")
        elif cmd == "SUBCAP":
            RUNTIME.subscribe(conn, session)
        elif cmd == "CHUNK":
            # Compatibility with a running v1 streamer during deployment.
            body = f.read(32 * 1024 * 1024 + 1)
            if len(body) > 32 * 1024 * 1024:
                raise ValueError("audio packet too large")
            timeline = []
            if len(hdr) > 3:
                for item in hdr[3].split(";"):
                    try:
                        ts, name = item.split("=", 1)
                        timeline.append((float(ts), name.replace("_", " ")))
                    except ValueError:
                        pass
            RUNTIME.accept(
                session,
                {"stream": uuid.uuid4().hex, "seq": 0, "timeline": timeline},
                wav_to_pcm(body),
            )
            conn.sendall(b"OK\n")
        elif cmd == "GETCAP":
            st = STATE.get(session, {"lines": []})
            conn.sendall("".join(f"{sp}\t{tx}\n" for _, sp, tx in st["lines"][-12:]).encode())
        elif cmd == "GETTODO":
            conn.sendall((TODOS.get(session, "") + "\n").encode())
        elif cmd == "GETSUM":
            st = STATE.get(session, {"lines": []})
            try:
                out = qwen(
                    "Summarize this live meeting so far in two sentences max, English.",
                    session_transcript(st),
                )
            except Exception:
                out = "summary unavailable"
            conn.sendall((out[:400] + "\n").encode())
        elif cmd == "GETGPU":
            with LOCK:
                selected = (
                    [STATE[session]] if len(hdr) > 1 and session in STATE else list(STATE.values())
                )
                ages = [time.time() - s["last_chunk"] for s in selected if s.get("last_chunk")]
                payload = dict(GPU)
                if os.environ.get("CAPTION_OWNER_TOKEN"):
                    payload["server_nonce"] = os.environ["CAPTION_OWNER_TOKEN"]
                payload["chunk_age"] = round(min(ages), 1) if ages else -1
                payload["n_sessions"] = len(STATE)
                payload["stt_err"] = GPU.get("stt_err", "")
                if len(selected) == 1:
                    active = selected[0]
                    payload.update(
                        decode_ms=active.get("decode_ms", 0),
                        audio_quality=active.get("audio_quality", {}),
                        decode_error=active.get("decode_error", ""),
                        decode_busy=bool(active.get("busy")),
                        finalize_pending=bool(active.get("utterance_pcm")),
                        revision=active.get("revision", 0),
                        duplicates=active.get("duplicates", 0),
                        last_speech_age=round(
                            time.time() - active.get("last_voice_at", active.get("last_speech", 0)),
                            1,
                        ),
                        queued_audio_s=RUNTIME.queued_seconds(
                            session if session in STATE else next(iter(STATE)), active
                        ),
                    )
                conn.sendall((json.dumps(payload) + "\n").encode())
        elif cmd == "PLANUI":
            from live_transcript.providers.rescue import plan_recovery

            line = f.readline(8193)
            if len(line) > 8192 or not line.endswith(b"\n"):
                raise ValueError("invalid recovery snapshot size")
            conn.sendall(json.dumps(plan_recovery(json.loads(line))).encode() + b"\n")
        elif cmd == "GETANS":
            q = " ".join(hdr[2:]) if len(hdr) > 2 else ""
            st = STATE.get(session, {"lines": []})
            try:
                ans = qwen(
                    "You are a live meeting assistant answering a question spoken into the meeting. "
                    "Answer in at most 45 words, plain text (it will be read aloud, no markdown), "
                    "in the language of the question. If the transcript does not contain the answer, say so briefly.",
                    f"Transcript so far:\n{session_transcript(st, 60)}\n\nQuestion: {q}",
                    maxtok=160,
                )
            except Exception:
                ans = ""
            conn.sendall((ans[:500] + "\n").encode())
        elif cmd == "SAY":
            lang = hdr[1] if len(hdr) > 1 and hdr[1] in ("de", "en") else "de"
            text = " ".join(hdr[2:]) if len(hdr) > 2 else ""
            conn.sendall(tts_wav(text, lang))
        elif cmd == "RESET":
            with RUNTIME.changed:
                STATE.pop(session, None)
                SPEAKER.pop(session, None)
                TODOS.pop(session, None)
                RUNTIME.store.reset(session)
                RUNTIME.changed.notify_all()
            try:
                os.unlink(CAP_FILE)
            except Exception:
                pass
            conn.sendall(b"OK\n")
        else:
            conn.sendall(b"ERR\n")
    except (BrokenPipeError, ConnectionResetError, socket.timeout):
        pass
    except Exception as e:
        import traceback

        print(f"[cap] conn err {e}", flush=True)
        traceback.print_exc()
        try:
            conn.sendall(b"ERR\n")
        except Exception:
            pass
    finally:
        conn.close()


def serve(database):
    global RUNTIME
    from live_transcript.transcription.store import CaptionStore
    from live_transcript.transcription.runtime import CaptionRuntime

    os.umask(0o077)
    RUNTIME = CaptionRuntime(
        sys.modules[__name__],
        CaptionStore(database),
    )
    RUNTIME.start()
    from live_transcript.notes.board import start_board

    if get("model", "enabled"):
        start_board(sys.modules[__name__], RUNTIME)
    threading.Thread(target=gpu_poller, daemon=True).start()
    threading.Thread(target=backend_poller, daemon=True).start()
    if get("model", "enabled"):
        threading.Thread(target=ticker, daemon=True).start()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    while (
        True
    ):  # virbr0 may not exist yet at boot (libvirtd race) — retry, don't crash-loop the unit
        try:
            srv.bind((BIND, PORT))
            break
        except OSError as e:
            print(f"[cap] bind failed ({e}) — retry in 5s", flush=True)
            srv.close()
            time.sleep(5)
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.listen(16)
    print(f"[cap] listening :{PORT}", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=worker, args=(c,), daemon=True).start()


if __name__ == "__main__":
    from live_transcript.transcription.store import journal_owner

    database = os.environ.get("CAPTION_DB", os.path.join(get("paths", "state"), "captions.sqlite3"))
    with journal_owner(database):
        serve(database)
