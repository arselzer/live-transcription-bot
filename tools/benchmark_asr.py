#!/usr/bin/env python3
"""Local-only ASR comparison. Does not play audio or send it to the meeting.
Warm inference timings are compute timings, not end-to-end caption latency.
"""

import argparse
import gc
import json
from pathlib import Path
from live_transcript.core.config import get
import statistics
import time
import wave


def audio(path):
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        return (
            np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype("float32") / 32768
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(get("paths", "root")) / "benchmarks"))
    ap.add_argument("--model", action="append", default=[])
    a = ap.parse_args()
    root = Path(a.root)
    root.joinpath("results").mkdir(exist_ok=True)
    clips = [
        ("English", root / "audio/english-live-test.wav"),
        ("German", root / "audio/german-live-test.wav"),
    ]
    results = []
    import live_transcript.transcription.daemon as captiond

    for language, path in clips:
        samples = audio(path)
        times, output = [], None
        for _ in range(3):
            start = time.monotonic()
            output = captiond.stt_server(str(path), {"English": "en", "German": "de"}[language])
            times.append(time.monotonic() - start)
        row = dict(
            engine="whisper.cpp large-v3",
            language=language,
            audio_s=len(samples) / 16000,
            seconds=times,
            median_s=statistics.median(times),
            text=output,
        )
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    import torch
    from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration

    torch.set_num_threads(4)
    for model_path in a.model:
        try:
            start = time.monotonic()
            processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
            model = (
                Qwen3ASRForConditionalGeneration.from_pretrained(
                    model_path, dtype=torch.bfloat16, local_files_only=True
                )
                .to("cuda:0")
                .eval()
            )
            load_s = time.monotonic() - start
            for language, path in clips:
                samples = audio(path)
                conversation = [
                    {"role": "system", "content": [{"type": "text", "text": ""}]},
                    {"role": "user", "content": [{"type": "audio", "audio": samples}]},
                ]
                inputs = processor.apply_chat_template(
                    conversation,
                    tokenize=True,
                    add_generation_prompt=True,
                    return_dict=True,
                    return_tensors="pt",
                    sampling_rate=16000,
                )
                prefill = processor.tokenizer(
                    f"language {language}<asr_text>", add_special_tokens=False, return_tensors="pt"
                ).input_ids
                inputs["input_ids"] = torch.cat([inputs["input_ids"], prefill], dim=1)
                inputs["attention_mask"] = torch.ones_like(inputs["input_ids"])
                inputs = {k: v.to("cuda:0") for k, v in inputs.items()}
                inputs["input_features"] = inputs["input_features"].to(model.dtype)
                times, output = [], None
                for index in range(4):  # one warm-up followed by three measured runs
                    torch.cuda.synchronize()
                    start = time.monotonic()
                    with torch.inference_mode():
                        output = model.generate(**inputs, max_new_tokens=128, do_sample=False)
                    torch.cuda.synchronize()
                    elapsed = time.monotonic() - start
                    if index:
                        times.append(elapsed)
                text = processor.tokenizer.decode(
                    output[0, inputs["input_ids"].shape[1] :], skip_special_tokens=True
                )
                row = dict(
                    engine=Path(model_path).name + " / Transformers ROCm",
                    language=language,
                    audio_s=len(samples) / 16000,
                    load_s=load_s,
                    seconds=times,
                    median_s=statistics.median(times),
                    text=text,
                )
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
            del model, processor, inputs, output
            gc.collect()
            torch.cuda.empty_cache()
        except Exception as exc:
            row = dict(engine=model_path, error=f"{type(exc).__name__}: {exc}")
            results.append(row)
            print(json.dumps(row), flush=True)
    destination = root / "results/asr-comparison.json"
    destination.write_text(
        json.dumps(
            {
                "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "notes": "Warm model compute timings on two 10-second clips; not WER or streaming latency.",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("Saved:", destination, flush=True)


if __name__ == "__main__":
    main()
