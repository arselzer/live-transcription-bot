"""Durable ingest, bounded buffering, one decoder per session, pushed captions."""

import json
import math
import re
import threading
import time


def language_choice(probabilities, current):
    """Route acoustic language evidence strictly between German and English."""
    en, de = float(probabilities.get("en", 0)), float(probabilities.get("de", 0))
    choice, best, other = ("en", en, de) if en >= de else ("de", de, en)
    return choice if best >= 0.30 and best >= other * 1.6 and best - other >= 0.15 else current


def text_languages(app, lines):
    languages = set()
    for _, _, text in lines:
        de, en = app.lang_hits(text)
        if max(de, en) >= 2 and max(de, en) >= min(de, en) * 2:
            languages.add("de" if de > en else "en")
    return languages


def recognition_context():
    from live_transcript.transcription.batch import glossary_text

    # UI display names and provisional captions are not evidence of words spoken.
    # Priming from them can make Whisper repeat labels instead of recognizing audio.
    return glossary_text()


def speech_spans(pcm, rms, threshold, gap):
    """Partition at observed silence, keeping every sample and at most four spans."""
    frame = 640  # 20 ms of PCM16 at 16 kHz
    quiet = None
    cuts = []
    voiced = False
    for offset in range(0, len(pcm), frame):
        if rms(pcm[offset : offset + frame]) < threshold:
            if quiet is None:
                quiet = offset
        else:
            if voiced and quiet is not None and offset - quiet >= gap * 32000:
                cuts.append((offset - quiet, ((offset + quiet) // 4) * 2))
            quiet = None
            voiced = True
    # Prefer the clearest boundaries if the utterance contains many hesitations.
    points = [0, *sorted(at for _, at in sorted(cuts, reverse=True)[:3]), len(pcm)]
    return list(zip(points, points[1:]))


class CaptionRuntime:
    def __init__(self, app, store):
        self.app, self.store = app, store
        self.changed = threading.Condition(app.LOCK)
        self.stopped = threading.Event()
        for session in store.sessions():
            st = store.restore(session) or self.new_state()
            st.update(busy=False, generation=0)
            app.STATE[session] = st

    @staticmethod
    def new_state():
        return {
            "pcm": bytearray(),
            "pcm_t0": 0.0,
            "dur": 0.0,
            "emitted": [],
            "lines": [],
            "timeline": [],
            "last_run": 0.0,
            "created": time.time(),
            "decoded_until": 0.0,
            "ingest_id": 0,
            "revision": 0,
            "generation": 0,
        }

    def accept(self, session, meta, pcm):
        if not pcm or len(pcm) % 2 or len(pcm) > 12 * 32000:
            raise ValueError("empty or unaligned PCM")
        for key in ("audio_t0", "captured_at"):
            if key in meta and (not math.isfinite(float(meta[key])) or float(meta[key]) < 0):
                raise ValueError("invalid audio timestamp")
        for timestamp, name in meta.get("timeline", []):
            if not math.isfinite(float(timestamp)) or float(timestamp) < 0 or len(str(name)) > 128:
                raise ValueError("invalid speaker event")
        added = self.store.accept(session, meta, pcm)
        from live_transcript.transcription.audio_quality import measure

        quality = measure(pcm)
        with self.changed:
            if session not in self.app.STATE:
                self.app.STATE[session] = self.store.restore(session) or self.new_state()
            st = self.app.STATE[session]
            st["last_chunk"] = time.time()
            st["audio_quality"] = quality
            st["duplicates"] = st.get("duplicates", 0) + int(not added)
            self.changed.notify_all()
        return added

    def fill(self, session, st):
        rows = self.store.pending(session, st.get("ingest_id", 0))
        with self.app.LOCK:
            if self.app.STATE.get(session) is not st:
                return
            for ident, meta, pcm in rows:
                if st.get("force_finalize"):
                    break  # settle the previous audio-time utterance before loading the next
                if len(st["pcm"]) + len(pcm) > 30 * 32000:
                    break  # leave excess audio durably queued, never trim it away
                end = st["pcm_t0"] + len(st["pcm"]) / 32000
                start = float(meta.get("audio_t0", end))
                if not st["pcm"]:
                    st["pcm_t0"] = start
                    st["decoded_until"] = start
                elif start - end > 0.2:
                    if st.get("utterance_pcm"):
                        st["force_finalize"] = True
                        break  # settle pre-gap speech before accepting another clock span
                    if end - st.get("decoded_until", 0) > 0.05:
                        break  # process the previous capture before crossing a gap
                    st["pcm"].clear()
                    st["pcm_t0"] = start
                    st["decoded_until"] = start
                    st["preroll"] = bytearray()
                st["pcm"].extend(pcm)
                voiced = self.app._rms(pcm) >= self.app.SILENCE_RMS
                if voiced and not st.get("utterance_pcm"):
                    lead = bytes(st.get("preroll", b""))
                    st["utterance_pcm"] = bytearray(lead)
                    st["utterance_t0"] = max(0, start - len(lead) / 32000)
                    st["utterance_mark"] = len(st["lines"])
                if st.get("utterance_pcm") or voiced:
                    st.setdefault("utterance_pcm", bytearray()).extend(pcm)
                st["preroll"] = bytearray(pcm[-8000:])
                if voiced:
                    st["last_voice_at"] = time.time()
                    st["last_voice_audio_end"] = start + len(pcm) / 32000
                st["ingest_id"] = ident
                st["dur"] = len(st["pcm"]) / 32000
                if meta.get("captured_at"):
                    st["captured_at"] = float(meta["captured_at"])
                events = {(float(t), str(n)) for t, n in st["timeline"]}
                events.update((float(t), str(n)) for t, n in meta.get("timeline", []))
                timeline = []
                for t, name in sorted(events):
                    if not timeline or timeline[-1][1] != name:
                        timeline.append((t, name))
                st["timeline"] = timeline[-10000:]
                # Backlogged audio must retain its original silence boundaries.
                # Wall time alone merges turns/languages while CPU decode is slow.
                if (
                    not voiced
                    and st.get("utterance_pcm")
                    and start + len(pcm) / 32000 - st.get("last_voice_audio_end", start)
                    >= self.app.SILENCE_FINALIZE
                ):
                    st["force_finalize"] = True
                    break

    def publish(self, session, st):
        """Caller owns app.LOCK. Checkpoint before notifying viewers."""
        st["revision"] = st.get("revision", 0) + 1
        st["published_at"] = time.time()
        self.store.checkpoint(session, st)
        self.app.write_caps(session)
        self.changed.notify_all()

    @staticmethod
    def record_backend(st, segments, audio_t0):
        backend = getattr(segments, "backend", None)
        if backend:
            st["asr"] = backend
            events = st.setdefault("asr_events", [])
            if not events or events[-1]["engine"] != backend["engine"]:
                events.append(
                    dict(
                        utc=time.time(),
                        audio_t0=audio_t0,
                        engine=backend["engine"],
                        degraded=backend["degraded"],
                    )
                )

    def snapshot(self, session):
        with self.app.LOCK:
            st = self.app.STATE.get(session, {})
            return {
                "revision": st.get("revision", 0),
                "lines": list(st.get("lines", [])[-12:]),
                "published_at": st.get("published_at", 0),
                "decode_ms": st.get("decode_ms", 0),
                "queued_audio_s": self.queued_seconds(session, st),
                "board": st.get("board", {}),
                "asr": st.get("asr", {}),
                "asr_events": st.get("asr_events", [])[-32:],
                "draft": bool(st.get("cand")),
            }

    def queued_seconds(self, session, st):
        end = st.get("pcm_t0", 0) + len(st.get("pcm", b"")) / 32000
        return round(
            max(0, end - st.get("decoded_until", 0))
            + self.store.backlog_seconds(session, st.get("ingest_id", 0)),
            2,
        )

    def subscribe(self, conn, session):
        revision = None
        while not self.stopped.is_set():
            with self.changed:
                current = self.app.STATE.get(session, {}).get("revision", 0)
                if current == revision:
                    self.changed.wait(timeout=5)  # heartbeat detects broken sockets
                data = self.snapshot(session)
            conn.sendall(json.dumps(data, ensure_ascii=False).encode() + b"\n")
            revision = data["revision"]

    def pump_once(self):
        with self.app.LOCK:
            sessions = list(self.app.STATE.items())
        for session, st in sessions:
            self.fill(session, st)
            with self.changed:
                now = time.time()
                if st.get("busy"):
                    if now - st.get("busy_since", now) <= getattr(self.app, "DECODE_TIMEOUT", 30):
                        continue
                    st["generation"] = st.get("generation", 0) + 1
                    st["busy"] = False  # late completion is rejected by generation
                final_due = st.get("utterance_pcm") and (
                    st.get("force_finalize")
                    or now - st.get("last_voice_at", now) >= self.app.SILENCE_FINALIZE
                    or len(st["utterance_pcm"]) >= 28 * 32000
                )
                if final_due and now - st.get("last_run", 0) >= self.app.RUN_EVERY:
                    st["busy"], st["busy_since"], st["last_run"] = True, now, now
                    token = st.get("generation", 0) + 1
                    st["generation"] = token
                    snap = bytes(st["utterance_pcm"][: 28 * 32000])
                    args = (
                        session,
                        st,
                        snap,
                        st["utterance_t0"],
                        st.get("utterance_mark", len(st["lines"])),
                        token,
                    )
                    threading.Thread(target=self.finalize_speech, args=args, daemon=True).start()
                    continue
                end = st["pcm_t0"] + len(st["pcm"]) / 32000
                if end - st.get("decoded_until", 0) < 0.05:
                    if (
                        st.get("cand")
                        and now - st.get("last_chunk", now) > self.app.SILENCE_FINALIZE
                        and self.app._finalize(st)
                    ):
                        self.publish(session, st)
                    continue
                if now - st.get("last_run", 0) < self.app.RUN_EVERY:
                    continue
                if len(st["pcm"]) < 48000 and now - st.get("last_chunk", now) < 2:
                    continue
                snap = bytes(st["pcm"][: 12 * 32000])
                st["busy"], st["busy_since"], st["last_run"] = True, now, now
                token = st.get("generation", 0) + 1
                st["generation"] = token
                args = (session, st, snap, st["pcm_t0"], token)
            threading.Thread(target=self.decode, args=args, daemon=True).start()

    def decode(self, session, st, snap, t0, token):
        app = self.app
        if hasattr(app, "ASR_ROUTER"):
            app.ASR_ROUTER.note_backlog(self.queued_seconds(session, st))
        started = time.monotonic()
        silent = app._rms(snap) < app.SILENCE_RMS
        try:
            with app.LOCK:
                timeline = list(st["timeline"])
                pick = st.get("pick", "de")
                input_voice_at = st.get("last_voice_at", 0)
                prompt = recognition_context()
            segs = [] if silent else app.transcribe(snap, pick, prompt)
            if segs is None:
                raise RuntimeError("speech backend unavailable; audio retained for retry")
            txt = " ".join(text for _, text in segs)
            d, e = app.lang_hits(txt)
            acoustic_pick = language_choice(getattr(segs, "language_probs", {}), pick)
            lexical_switch = (e > d) if pick == "de" else (d > e)
            if not silent and (acoustic_pick != pick or lexical_switch):
                other = acoustic_pick if acoustic_pick != pick else ("en" if pick == "de" else "de")
                # A previous-language transcript seed can prime an unwanted translation.
                alternative = app.transcribe(snap, other, prompt)
                if alternative is not None:
                    txt2 = " ".join(text for _, text in alternative)
                    d2, e2 = app.lang_hits(txt2)
                    if alternative and (
                        acoustic_pick != pick
                        or (e2 if other == "en" else d2) > (d if pick == "de" else e)
                    ):
                        segs, pick = alternative, other

            def usable(text):
                if app.HALLUC.search(text) or app.BAD_LANGS.search(text):
                    return False
                words = re.findall(r"\w+", text.lower())
                return not (len(words) >= 8 and 1 - len(set(words)) / len(words) > 0.5)

            segment_ends = getattr(segs, "segment_ends", {})
            asr_result = segs
            segs = [(t, text) for t, text in segs if usable(text)]
            with self.changed:
                if self.app.STATE.get(session) is not st or st.get("generation") != token:
                    return  # reset, timeout, or newer decoder owns the session
                st["pick"] = pick
                if segs:
                    history = st.setdefault("language_history", [])
                    history.append((t0, t0 + len(snap) / 32000, pick))
                    st["language_history"] = history[-64:]
                self.record_backend(st, asr_result, t0)
                if not silent:
                    st["decode_ms"] = round((time.monotonic() - started) * 1000, 1)
                st["decode_error"] = ""
                changed = False
                if segs:
                    st["last_speech"] = time.time()
                    # Assign every segment at its MEETING timestamp, not window offset.
                    groups = []
                    for relative, text in segs:
                        absolute = t0 + float(relative)
                        end = min(
                            t0 + len(snap) / 32000,
                            t0 + segment_ends.get((relative, text), relative),
                        )
                        speaker = app.spk_between(timeline, absolute, end)
                        if groups and groups[-1][0] == speaker:
                            groups[-1][2] += " " + text
                        else:
                            groups.append([speaker, absolute, text])
                    apply = app.commit_apply if app.COMMIT_ONCE else app.grow_or_append
                    st["input_voice_at"] = input_voice_at
                    for speaker, absolute, text in groups:
                        changed = apply(st, speaker, absolute, text) or changed
                    print(
                        f"[pass] sess={session} pick={pick} segs={len(segs)} decode_ms={st['decode_ms']}",
                        flush=True,
                    )
                elif (
                    not st.get("utterance_pcm")
                    and time.time() - st.get("last_speech", 0) > app.SILENCE_FINALIZE
                ):
                    changed = app._finalize(st)
                consumed_until = t0 + len(snap) / 32000
                # Retain overlap and any audio that arrived while inference ran.
                has_stable_text = bool(st.get("cand") and st["lines"] and st["lines"][-1][2])
                trim = (
                    max(
                        0,
                        min(
                            len(st["pcm"]),
                            int((consumed_until - app.OVERLAP_S - st["pcm_t0"]) * 32000),
                        ),
                    )
                    & ~1
                    if silent or not st.get("cand") or has_stable_text
                    else 0
                )
                if trim:
                    del st["pcm"][:trim]
                    st["pcm_t0"] += trim / 32000
                st["decoded_until"] = consumed_until
                st["dur"] = len(st["pcm"]) / 32000
                if changed:
                    st["text_revision"] = st.get("text_revision", 0) + 1
                    self.publish(session, st)
                else:
                    self.store.checkpoint(session, st)
        except Exception as exc:
            with app.LOCK:
                if st.get("generation") == token:
                    st["decode_error"] = str(exc)[:160]
            print(f"[decode] session={session}: {exc}", flush=True)
        finally:
            with self.changed:
                if st.get("generation") == token:
                    st["busy"] = False
                self.changed.notify_all()

    def finalize_speech(self, session, st, snap, t0, mark, token):
        """One full-utterance pass settles the draft; finalized history stays fixed."""
        app = self.app
        if hasattr(app, "ASR_ROUTER"):
            app.ASR_ROUTER.note_backlog(self.queued_seconds(session, st))
        try:
            pick = st.get("pick", "de")
            prompt = recognition_context()
            with app.LOCK:
                prior = list(st["lines"][mark:])
                languages = text_languages(app, prior)
                languages.update(
                    lang
                    for start, end, lang in st.get("language_history", [])
                    if end > t0 and start < t0 + len(snap) / 32000
                )
            spans = [(0, len(snap))]
            if len(languages) > 1:
                from live_transcript.core.config import get

                spans = speech_spans(
                    snap, app._rms, app.SILENCE_RMS, get("caption", "mixed_language_gap")
                )
            segs = app.ASRSegments()
            for begin, stop in spans:
                part = app.transcribe(snap[begin:stop], pick, prompt)
                if part is None:
                    raise RuntimeError("final pass unavailable; utterance retained")
                route = language_choice(getattr(part, "language_probs", {}), pick)
                if route != pick:
                    other = app.transcribe(snap[begin:stop], route, prompt)
                    if other is None:
                        raise RuntimeError("language correction unavailable; utterance retained")
                    if other:
                        part, pick = other, route
                for start, text in part:
                    relative = begin / 32000 + start
                    segs.append((relative, text))
                    segs.segment_ends[(relative, text)] = begin / 32000 + getattr(
                        part, "segment_ends", {}
                    ).get((start, text), start)
                if getattr(part, "backend", None):
                    segs.backend = part.backend
            with self.changed:
                if app.STATE.get(session) is not st or st.get("generation") != token:
                    return
                self.record_backend(st, segs, t0)
                if not segs:
                    app._finalize(st)
                else:
                    ends = getattr(segs, "segment_ends", {})
                    final = []
                    for start, text in segs:
                        if app.HALLUC.search(text) or app.BAD_LANGS.search(text):
                            continue
                        end = t0 + ends.get((start, text), start)
                        speaker = app.segment_speaker(st["timeline"], t0 + start, end)
                        final.append((t0 + start, speaker, text.strip()))
                    if final:
                        mark = min(mark, len(st["lines"]))
                        previous = list(st["lines"][mark:])
                        if previous:
                            old_words = app._toks(" ".join(row[2] for row in previous))
                            new_words = app._toks(" ".join(row[2] for row in final))
                            # Confidence filtering can retain only the last
                            # segment of a full pass. It must not erase a known
                            # leading clause when the result is just its suffix.
                            loses_language = bool(
                                text_languages(app, previous) - text_languages(app, final)
                            )
                            if loses_language and len(languages) > 1:
                                events = st.setdefault("reconciliation_events", [])
                                events.append(
                                    dict(
                                        utc=time.time(),
                                        audio_t0=t0,
                                        reason="language_coverage",
                                        candidate=final,
                                    )
                                )
                                st["reconciliation_events"] = events[-32:]
                                final = previous
                            elif (
                                new_words
                                and len(new_words) * 2 < len(old_words)
                                and old_words[-len(new_words) :] == new_words
                                and final[0][0] > previous[0][0] + 1
                            ):
                                final = previous
                        st["reconcile_from"] = (
                            st["lines"][mark][0] if mark < len(st["lines"]) else t0
                        )
                        st["lines"][mark:] = final
                    else:
                        app._finalize(st)
                st["cand"] = None
                st.pop("force_finalize", None)
                st["pick"] = pick
                st["language_history"] = [
                    row for row in st.get("language_history", []) if row[1] > t0 + len(snap) / 32000
                ]
                remainder = bytes(st.get("utterance_pcm", b""))[len(snap) :]
                end = t0 + len(snap) / 32000
                if remainder and app._rms(remainder) >= app.SILENCE_RMS:
                    st["utterance_pcm"] = bytearray(remainder)
                    st["utterance_t0"] = end
                    st["utterance_mark"] = len(st["lines"])
                else:
                    st["utterance_pcm"] = bytearray()
                trim = max(0, min(len(st["pcm"]), int((end - 0.25 - st["pcm_t0"]) * 32000))) & ~1
                if trim:
                    del st["pcm"][:trim]
                    st["pcm_t0"] += trim / 32000
                st["decoded_until"] = max(st.get("decoded_until", 0), end)
                st["text_revision"] = st.get("text_revision", 0) + 1
                st["decode_error"] = ""
                self.publish(session, st)
                print(f"[final] session={session} language={pick} segments={len(segs)}", flush=True)
        except Exception as exc:
            with app.LOCK:
                if app.STATE.get(session) is st and st.get("generation") == token:
                    st["decode_error"] = str(exc)
            print(f"[final] {session}: {exc}", flush=True)
        finally:
            with self.changed:
                if st.get("generation") == token:
                    st["busy"] = False
                self.changed.notify_all()

    def start(self):
        def loop():
            while not self.stopped.wait(0.15):
                try:
                    self.pump_once()
                except Exception as exc:
                    print(f"[runtime] {exc}", flush=True)

        threading.Thread(target=loop, daemon=True).start()
