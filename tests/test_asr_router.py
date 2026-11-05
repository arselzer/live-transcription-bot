import unittest
from unittest.mock import patch
from live_transcript.transcription.router import ASRRouter
from live_transcript.transcription.service import command
import live_transcript.core.config as runtime_config


class RouterTests(unittest.TestCase):
    def test_healthy_gpu_never_downgrades_even_with_backlog(self):
        router = ASRRouter()
        router.note_backlog(100)
        calls = []

        def request(*a, **kw):
            calls.append(kw)
            return [(0, "accurate")]

        self.assertEqual(router.transcribe(request, "wav", "de", "", 3), [(0, "accurate")])
        self.assertEqual(router.health["engine"], "gpu-large")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("endpoint", calls[0])

    def test_gpu_failure_uses_full_cpu_first_then_backlog_small(self):
        router = ASRRouter()
        calls = []

        def request(*a, **kw):
            calls.append(kw)
            if "endpoint" not in kw:
                raise ConnectionError("GPU lost")
            return [(0, "preserved")]

        router.transcribe(request, "wav", "de", "", 3)
        self.assertEqual(router.health["engine"], "cpu-large")
        router.note_backlog(25)
        router.transcribe(request, "wav", "de", "", 3)
        self.assertEqual(router.health["engine"], "cpu-small")
        self.assertEqual(calls[-1]["endpoint"], runtime_config.get("asr", "small_cpu_url"))

    def test_sustained_slow_cpu_switches_without_backlog(self):
        router = ASRRouter()
        router.primary_retry_at = float("inf")

        def request(*a, **kw):
            return [(0, "text")]

        # One-second audio with a synthetic two-second inference measurement.
        with patch(
            "live_transcript.transcription.router.time.monotonic",
            side_effect=[0, 0, 2, 3, 3, 5, 6, 6, 8],
        ):
            for _ in range(3):
                router.transcribe(request, "wav", "de", "", 1)
        self.assertEqual(router.health["engine"], "cpu-small")

    def test_small_model_unavailable_falls_back_to_full_cpu(self):
        router = ASRRouter()
        router.primary_retry_at = float("inf")
        router.note_backlog(100)

        def request(*a, **kw):
            if kw.get("endpoint") == runtime_config.get("asr", "small_cpu_url"):
                raise ConnectionError("small unavailable")
            return [(0, "text")]

        self.assertTrue(router.transcribe(request, "wav", "de", "", 3))
        self.assertEqual(router.health["engine"], "cpu-large")

    def test_both_cpu_engines_down_return_none_for_durable_retry(self):
        router = ASRRouter()

        def request(*a, **kw):
            raise ConnectionError("all unavailable")

        self.assertIsNone(router.transcribe(request, "wav", "de", "", 3))
        self.assertEqual(router.health["engine"], "unavailable")

    def test_gpu_success_resets_degraded_state_and_small_model_latch(self):
        router = ASRRouter()
        router.cpu_small = True
        router.cpu_slow_samples = 10
        self.assertTrue(
            router.transcribe(lambda *a, **kw: [(0, "full quality")], "wav", "de", "", 3)
        )
        self.assertFalse(router.cpu_small)
        self.assertEqual(router.cpu_slow_samples, 0)
        self.assertFalse(router.health["degraded"])

    def test_concurrent_decode_cannot_build_unbounded_server_queue(self):
        router = ASRRouter()
        router.lock.acquire()
        try:
            self.assertIsNone(
                router.transcribe(
                    lambda *a, **kw: self.fail("must not call engine"), "wav", "de", "", 3
                )
            )
        finally:
            router.lock.release()

    def test_cpu_can_be_disabled_and_adaptation_can_be_disabled(self):
        original = runtime_config.get
        for disabled in ("cpu_fallback", "adaptive_model"):
            router = ASRRouter()
            router.note_backlog(100)
            calls = []

            def setting(section, key):
                return False if section == "asr" and key == disabled else original(section, key)

            def request(*a, **kw):
                calls.append(kw)
                if "endpoint" not in kw:
                    raise ConnectionError()
                return [(0, "text")]

            with patch("live_transcript.transcription.router.get", side_effect=setting):
                router.transcribe(request, "wav", "de", "", 3)
            if disabled == "cpu_fallback":
                self.assertEqual(len(calls), 1)
            else:
                self.assertEqual(router.health["engine"], "cpu-large")

    def test_cpu_service_command_cannot_offload_to_gpu(self):
        for device in ("cpu-large", "cpu-small"):
            self.assertIn("--no-gpu", command(device))
        self.assertNotIn("--no-gpu", command("gpu"))


if __name__ == "__main__":
    unittest.main()
