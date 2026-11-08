"""Bounded live GPU/CPU failover; smaller models only in degraded operation."""

import threading
import time
from live_transcript.core.config import get


class ASRRouter:
    def __init__(self):
        self.lock = threading.Lock()
        self.primary_retry_at = 0
        self.backlog = 0
        self.cpu_slow_samples = 0
        self.cpu_small = False
        self.primary_engine = (
            "cpu-primary" if get("asr", "primary_device") == "cpu" else "gpu-large"
        )
        self.health = dict(engine=self.primary_engine, degraded=False, switches=0)

    def note_backlog(self, seconds):
        self.backlog = max(0, float(seconds))

    def selected(self, engine, error="", rtf=None):
        if self.health.get("engine") != engine:
            self.health["switches"] += 1
        self.health.update(
            engine=engine,
            degraded=engine != self.primary_engine,
            error=error,
            utc=time.time(),
            backlog_seconds=round(self.backlog, 2),
        )
        if rtf is not None:
            self.health["real_time_factor"] = round(rtf, 3)

    def transcribe(self, request, path, language, prompt, seconds):
        # Shared model servers cannot process arbitrarily many abandoned retries.
        # One outstanding inference across sessions preserves bounded resource use.
        if not self.lock.acquire(timeout=0.1):
            return None
        primary_error = ""
        try:
            if time.monotonic() >= self.primary_retry_at:
                try:
                    started = time.monotonic()
                    cpu_primary = self.primary_engine == "cpu-primary"
                    result = request(
                        path,
                        language,
                        prompt,
                        timeout=get("asr", "cpu_timeout" if cpu_primary else "gpu_timeout"),
                    )
                    if result is None:
                        raise RuntimeError("primary backend returned no result")
                    rtf = (time.monotonic() - started) / max(0.1, seconds)
                    if cpu_primary and rtf > get("asr", "slow_rtf"):
                        self.cpu_slow_samples += 1
                    else:
                        self.cpu_slow_samples = 0
                    behind = self.backlog > get(
                        "asr", "backlog_seconds"
                    ) or self.cpu_slow_samples >= get("asr", "slow_samples")
                    self.primary_retry_at = (
                        time.monotonic() + get("asr", "gpu_retry_seconds")
                        if cpu_primary and behind and get("asr", "adaptive_model")
                        else 0
                    )
                    self.cpu_small = bool(self.primary_retry_at)
                    self.selected(self.primary_engine, rtf=rtf)
                    if hasattr(result, "__dict__"):
                        result.backend = dict(self.health)
                    return result
                except Exception as exc:
                    primary_error = type(exc).__name__ + ": " + str(exc)[:120]
                    self.primary_retry_at = time.monotonic() + get("asr", "gpu_retry_seconds")
            if not get("asr", "cpu_fallback"):
                self.selected("unavailable", primary_error or "GPU circuit open; audio retained")
                return None
            if get("asr", "adaptive_model") and (
                self.backlog > get("asr", "backlog_seconds")
                or self.cpu_slow_samples >= get("asr", "slow_samples")
            ):
                self.cpu_small = True
            engines = (
                [("cpu-small", get("asr", "small_cpu_url")), ("cpu-large", get("asr", "cpu_url"))]
                if self.cpu_small
                else [
                    ("cpu-large", get("asr", "cpu_url")),
                    ("cpu-small", get("asr", "small_cpu_url")),
                ]
            )
            if not get("asr", "adaptive_model"):
                engines = [("cpu-large", get("asr", "cpu_url"))]
            for engine, endpoint in engines:
                if self.primary_engine == "cpu-primary" and endpoint == get("caption", "stt_url"):
                    continue  # do not enqueue the same failed inference a second time
                try:
                    started = time.monotonic()
                    result = request(
                        path, language, prompt, endpoint=endpoint, timeout=get("asr", "cpu_timeout")
                    )
                    if result is None:
                        raise RuntimeError("CPU backend returned no result")
                    rtf = (time.monotonic() - started) / max(0.1, seconds)
                    if engine == "cpu-large":
                        self.cpu_slow_samples = (
                            self.cpu_slow_samples + 1 if rtf > get("asr", "slow_rtf") else 0
                        )
                    self.selected(
                        engine,
                        primary_error
                        or (
                            "Primary CPU recognition is unavailable or behind; using CPU fallback"
                            if self.primary_engine == "cpu-primary"
                            else "GPU unavailable; retained audio is being decoded on CPU"
                        ),
                        rtf,
                    )
                    if hasattr(result, "__dict__"):
                        result.backend = dict(self.health)
                    return result
                except Exception as exc:
                    primary_error = engine + ": " + type(exc).__name__ + ": " + str(exc)[:100]
            self.selected("unavailable", primary_error)
            return None
        finally:
            self.lock.release()
