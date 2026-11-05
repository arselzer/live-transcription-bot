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
        self.health = dict(engine="gpu-large", degraded=False, switches=0)

    def note_backlog(self, seconds):
        self.backlog = max(0, float(seconds))

    def selected(self, engine, error="", rtf=None):
        if self.health.get("engine") != engine:
            self.health["switches"] += 1
        self.health.update(
            engine=engine,
            degraded=engine != "gpu-large",
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
                    result = request(path, language, prompt, timeout=get("asr", "gpu_timeout"))
                    if result is None:
                        raise RuntimeError("primary backend returned no result")
                    self.primary_retry_at = 0
                    self.cpu_slow_samples, self.cpu_small = 0, False
                    self.selected("gpu-large", rtf=(time.monotonic() - started) / max(0.1, seconds))
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
                        primary_error or "GPU unavailable; retained audio is being decoded on CPU",
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
