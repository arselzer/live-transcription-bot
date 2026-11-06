#!/usr/bin/env python3
"""Generate quality-checked full-model transcripts, notes and captioned video.

Recognition and retries retain original media and prior outputs on failure.
Usage: live-transcript process RECORDING.wav
"""

import json, os, sys, urllib.request
from datetime import datetime
from types import SimpleNamespace
from live_transcript.core.config import get
from live_transcript.transcription.quality import TranscriptQualityError
from live_transcript.runtime.processing import publish as publish_status, atomic_text


def _process(argv=None):
    arguments = sys.argv[1:] if argv is None else list(argv)
    if len(arguments) != 1:
        raise SystemExit("Usage: live-transcript process RECORDING.wav")
    WAV = arguments[0]
    from pathlib import Path
    import fcntl

    # Serialize manual and scheduled processing of the same recording.
    with open(Path(WAV).with_suffix(".postprocess.lock"), "a") as _processing_lock:
        fcntl.flock(_processing_lock, fcntl.LOCK_EX)
        publish_status(WAV, dict(status="processing"))
        STT_MODEL = get("paths", "cpu_model")
        API = get("model", "url")

        # 1) STT — whisper.cpp HIP on the GPU. Falls back to CPU faster-whisper.
        import subprocess, wave as _wave

        utterances = None
        stt_failed = False
        primary_failed = False
        batch_info = None
        try:
            from live_transcript.transcription.batch import transcribe_wav

            env = dict(os.environ, HIP_VISIBLE_DEVICES=get("asr", "gpu_device"))
            utterances, batch_info = transcribe_wav(
                WAV, env=env, progress=lambda state: publish_status(WAV, state)
            )
        except TranscriptQualityError as exc:
            from live_transcript.capture.guard import atomic_json
            import time

            candidate = Path(WAV).with_suffix(".rejected-" + str(time.time_ns()) + ".json")
            atomic_json(
                candidate, dict(quality=exc.report, utterances=getattr(exc, "utterances", []))
            )
            publish_status(
                WAV,
                dict(
                    status="quality_failed",
                    audio_retained=WAV,
                    candidate=str(candidate),
                    quality=exc.report,
                ),
            )
            raise SystemExit(
                "Full-model recognition remains repetitive after bounded retries; prior outputs retained"
            )
        except (FileNotFoundError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            primary_failed = True
            print(f"GPU STT failed ({e}) — CPU faster-whisper fallback", flush=True)
            try:
                from faster_whisper import WhisperModel
                import numpy as np

                with _wave.open(WAV) as w:
                    if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
                        raise ValueError(
                            "CPU recognition requires canonical 16 kHz mono PCM16 audio"
                        )
                    _pcm = (
                        np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(
                            np.float32
                        )
                        / 32768.0
                    )
                model = WhisperModel(
                    STT_MODEL,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=get("asr", "cpu_threads"),
                )
                segments, info = model.transcribe(
                    _pcm, vad_filter=True, language=None, condition_on_previous_text=False
                )
                utterances = [
                    {"t": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()}
                    for s in segments
                    if s.text.strip()
                ]
            except Exception as e2:
                print(
                    f"CPU STT fallback failed too ({e2}) — raw recording and prior outputs retained",
                    flush=True,
                )
                info = SimpleNamespace(language="und")
                utterances = []
                stt_failed = True
        if batch_info is not None:
            languages = batch_info["languages"]
            info = SimpleNamespace(
                language=languages[0] if len(languages) == 1 else ("mixed" if languages else "auto")
            )
            from live_transcript.capture.guard import atomic_json

            atomic_json(
                __import__("pathlib").Path(WAV).with_suffix(".transcription.json"),
                dict(utterances=utterances, **batch_info),
            )
            print(
                f"STT via whisper.cpp ({batch_info['chunks']} chunks, {len(batch_info['retries'])} repaired): "
                f"{len(utterances)} utterances, lang={info.language}",
                flush=True,
            )
        if stt_failed:
            from live_transcript.capture.guard import atomic_json

            publish_status(WAV, dict(status="stt_failed", audio_retained=WAV))
            # Never replace a previously successful transcript/notes with an empty stub.
            raise SystemExit(
                "Speech recognition unavailable; original recording and prior outputs retained"
            )
        if not utterances:
            previous = WAV.rsplit(".", 1)[0] + ".txt"
            if primary_failed:
                # A failed primary engine must never be reported as a silent meeting:
                # the fallback's empty result is unproven, so treat it as an outage.
                publish_status(
                    WAV, dict(status="stt_failed", audio_retained=WAV, fallback_empty=True)
                )
                raise SystemExit(
                    "Primary recognition failed and fallback returned no segments; prior outputs retained"
                )
            if os.path.exists(previous) and os.path.getsize(previous):
                from live_transcript.capture.guard import atomic_json

                publish_status(WAV, dict(status="stt_empty_prior_retained", audio_retained=WAV))
                raise SystemExit(
                    "Recognition returned no segments; previous transcript retained for review"
                )
        # A zero process exit does not establish useful recognition. Preserve every
        # candidate and previous output if a looping/timestamp failure survives retries.
        from live_transcript.transcription.quality import assess
        from live_transcript.capture.guard import atomic_json
        from pathlib import Path

        quality = assess(utterances)
        atomic_json(Path(WAV).with_suffix(".transcript-quality.json"), quality)
        if quality["status"] != "pass":
            import time

            rejected = Path(WAV).with_suffix(".rejected-" + str(time.time_ns()) + ".json")
            atomic_json(rejected, dict(utterances=utterances, quality=quality))
            publish_status(
                WAV,
                dict(
                    status="quality_failed",
                    audio_retained=WAV,
                    candidate=str(rejected),
                    quality=quality,
                ),
            )
            raise SystemExit(
                "Recognition failed quality checks; original audio and previous outputs retained"
            )

        transcript = "\n".join(
            f"[{int(u['t'] // 60):02d}:{int(u['t'] % 60):02d}] {u['text']}" for u in utterances
        )

        # 1b) speaker attribution: merge Zoom active-speaker timeline (speakers.jsonl) into utterances
        from live_transcript.transcription.speakers import load as load_speaker_timeline

        speaker_at, speaker_samples = load_speaker_timeline(
            WAV.rsplit(".", 1)[0] + ".speakers.jsonl", WAV.rsplit(".", 1)[0] + ".voice.jsonl"
        )
        if speaker_samples:
            lines = []
            for u in utterances:
                name = speaker_at(u["t"])
                lines.append(
                    f"[{int(u['t'] // 60):02d}:{int(u['t'] % 60):02d}] {name + ': ' if name else ''}{u['text']}"
                )
            transcript = "\n".join(lines)
            print(f"speaker attribution: {speaker_samples} recorded observations", flush=True)

        if get("postprocess", "diarization_enabled"):
            # 1c) voice diarization: cluster by actual voice, name clusters via DOM active votes.
            # Identity from voice survives camera-off stretches; DOM point-in-time is fallback.
            try:
                import live_transcript.transcription.diarize as _dz

                _dom = []
                _sp = WAV.rsplit(".", 1)[0] + ".speakers.jsonl"
                if os.path.exists(_sp):
                    for _l in Path(_sp).read_text().splitlines():
                        _d = json.loads(_l)
                        for _w in _d.get("who", []):
                            if _w.get("name"):
                                _dom.append(
                                    {
                                        "t": _d["t"],
                                        "name": _w["name"],
                                        "role": _w.get("role", "tile"),
                                    }
                                )
                _rep = _dz.diarize(WAV, utterances, _dom)
                from live_transcript.capture.guard import atomic_json as _aj

                _aj(Path(WAV).with_suffix(".diarization.json"), _rep)
                if _rep.get("voices"):
                    lines = []
                    for u in utterances:
                        name = u.get("voice_name") or (
                            speaker_at(u["t"]) if speaker_samples else None
                        )
                        lines.append(
                            f"[{int(u['t'] // 60):02d}:{int(u['t'] % 60):02d}] {name + ': ' if name else ''}{u['text']}"
                        )
                    transcript = "\n".join(lines)
                    print(
                        f"voice diarization: {_rep['voices']} voices, "
                        f"{sum(1 for u in utterances if u.get('voice_name'))}/{len(utterances)} named by voice",
                        flush=True,
                    )
            except Exception as _de:
                print("voice diarization skipped:", _de, flush=True)
        txt_path = WAV.rsplit(".", 1)[0] + ".txt"
        atomic_text(txt_path, transcript)
        print(
            f"STT done: {len(utterances)} utterances, lang={info.language} -> {txt_path}",
            flush=True,
        )

        # 2) Summary via local Qwen
        def qwen(sysmsg, user, maxtok=2500):
            body = json.dumps(
                {
                    "model": get("model", "name"),
                    "temperature": 0.2,
                    "max_tokens": maxtok,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [
                        {"role": "system", "content": sysmsg},
                        {"role": "user", "content": user},
                    ],
                }
            ).encode()
            req = urllib.request.Request(
                API, data=body, headers={"Content-Type": "application/json"}
            )
            return json.load(urllib.request.urlopen(req, timeout=600))["choices"][0]["message"][
                "content"
            ]

        SYS = (
            "You are a meeting-notes assistant. From the meeting transcript (any language), produce "
            "meeting notes ALWAYS IN ENGLISH, in Markdown: ## Key Points (max 7), ## Decisions, "
            "## Action Items (- [ ] task — owner — deadline, if identifiable), ## Open Questions, "
            "## Participants (name hypotheses from the text, with uncertainty). No speculation, "
            "short and concrete. List only explicitly agreed actions; do not infer owners or deadlines. "
            "Include original transcript timestamps for decisions and action items. Output language: English, always."
        )
        meta = {}
        try:
            value = json.loads(Path(WAV + ".json").read_text())
            if isinstance(value, dict):
                meta = value
        except (OSError, ValueError):
            pass
        from live_transcript.notes.summary import public_metadata

        meta = public_metadata(meta)
        summary_status = dict(status="complete")
        try:
            from live_transcript.notes.summary import summarize

            summary = (
                summarize(transcript, meta, qwen, SYS)
                if transcript.strip()
                else "No speech was recognized. The original recording is retained for review."
            )
        except Exception as e:
            print(f"summary generation failed ({e}) — embedding raw transcript instead", flush=True)
            summary_status = dict(status="failed", error=type(e).__name__)
            summary = "> Summary generation failed. The full transcript is retained below."
        md_path = WAV.rsplit(".", 1)[0] + ".md"
        document = (
            f"---\ndate: {datetime.now().date()}\nsource: {os.path.basename(WAV)}\n"
            f"meeting: {json.dumps(meta.get('meeting', '?'), ensure_ascii=False)}\nstt_lang: {info.language}\n---\n\n{summary}\n"
            f"\n<details><summary>Full transcript</summary>\n\n{transcript}\n\n</details>\n"
        )
        notes_path = WAV.rsplit(".", 1)[0] + ".notes.txt"
        if os.path.exists(notes_path):
            document += (
                "\n## Pinned notes (in-meeting !note)\n\n" + Path(notes_path).read_text() + "\n"
            )
        chat_path = WAV.rsplit(".", 1)[0] + ".chat.txt"
        if os.path.exists(chat_path):
            document += "\n## Meeting chat\n\n" + Path(chat_path).read_text() + "\n"
        atomic_text(md_path, document)
        print(f"SUMMARY -> {md_path}", flush=True)

        # Derived captions and videos publish atomically; errors retain old media.
        from live_transcript.presentation.artifacts import subtitles, render_video

        srt_path = WAV.rsplit(".", 1)[0] + ".srt"
        atomic_text(srt_path, subtitles(utterances, speaker_at))
        try:
            video_status = render_video(WAV, srt_path)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            video_status = dict(status="failed", error=type(exc).__name__)
            print(
                "Video generation failed; previous media retained:", type(exc).__name__, flush=True
            )

        # Publication is complete only after recognition quality and artifact generation.
        import hashlib

        artifacts = {}
        for path in (Path(txt_path), Path(md_path), Path(srt_path)):
            if path.is_file():
                artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        publish_status(
            WAV,
            dict(
                status="complete_with_warnings"
                if "failed" in (summary_status["status"], video_status["status"])
                else "complete",
                quality=quality,
                audio_retained=WAV,
                artifacts=artifacts,
                summary=summary_status,
                video=video_status,
            ),
        )


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        return _process(arguments)
    except Exception as exc:
        if len(arguments) == 1:
            try:
                publish_status(
                    arguments[0], dict(status="processing_failed", error=type(exc).__name__)
                )
            except OSError:
                pass
        raise


def run_logged(argv=None):
    from contextlib import redirect_stdout, redirect_stderr
    from pathlib import Path
    from live_transcript.core.files import atomic_text

    arguments = sys.argv[1:] if argv is None else list(argv)
    if len(arguments) != 1:
        raise SystemExit("Usage: live-transcript process RECORDING.wav")
    wav = Path(arguments[0])
    logs = Path(get("paths", "logs"))
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        logs / ("post-" + wav.stem + ".log"),
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "a") as log:
        os.fchmod(log.fileno(), 0o600)
        with redirect_stdout(log), redirect_stderr(log):
            main(arguments)
            vault = get("paths", "vault")
            if vault:
                try:
                    if not Path(vault).is_dir():
                        raise OSError("Configured notes vault is unavailable")
                    destination = Path(vault) / "meetings"
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    atomic_text(
                        destination / wav.with_suffix(".md").name,
                        wav.with_suffix(".md").read_text(),
                    )
                except OSError as exc:
                    # The recording's finished artifacts are authoritative. An
                    # optional notes mirror must not turn success into a retry.
                    print("Notes copy failed:", type(exc).__name__, flush=True)
                    state = json.loads(wav.with_suffix(".postprocess-status.json").read_text())
                    state.update(
                        status="complete_with_warnings",
                        vault=dict(status="failed", error=type(exc).__name__),
                    )
                    publish_status(str(wav), state)
    print("Processing finished:", wav.with_suffix(".md"))


if __name__ == "__main__":
    run_logged()
