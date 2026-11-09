import io
import json
import os
import socket
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from live_transcript.transcription.router import ASRRouter
from live_transcript.transcription.service import command
from live_transcript.transcription import service
import live_transcript.core.config as runtime_config


class BackendReadinessTests(unittest.TestCase):
    def test_readiness_follows_the_caption_endpoint_override(self):
        with patch.dict(os.environ, CAPTION_STT_URL="http://127.0.0.1:19992/inference"):
            self.assertEqual(service.endpoints()[0][1], "http://127.0.0.1:19992/inference")

    def test_occupied_endpoint_refuses_model_loading(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            argv = [
                "engine",
                "-m",
                str(self.model),
                "--host",
                "127.0.0.1",
                "--port",
                str(listener.getsockname()[1]),
            ]
            with (
                patch.object(service, "command", return_value=argv),
                patch.object(service, "supervise") as load,
                patch.object(sys, "argv", ["asr", "--device", "gpu"]),
                patch.dict(os.environ),
            ):
                with self.assertRaises(OSError):
                    service.main()
            load.assert_not_called()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.model = self.root / "model.bin"
        self.model.write_bytes(b"model")
        self.url = "http://127.0.0.1:19991/inference"
        original = service.get
        overrides = {
            ("paths", "state"): str(self.root),
            ("paths", "model"): str(self.model),
            ("caption", "stt_url"): self.url,
        }
        self.settings = patch.object(
            service, "get", side_effect=lambda s, k: overrides.get((s, k), original(s, k))
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.argv = command("gpu")
        self.record = dict(
            pid=os.getpid(),
            ticks="123",
            endpoint=self.url,
            model=service.identity(self.model),
            device="gpu",
        )

    def response(self, status="ok"):
        result = io.BytesIO(json.dumps(dict(status=status)).encode())
        result.status = 200
        return result

    def check(self, record=None, listening=True):
        (self.root / "asr-gpu.json").write_text(json.dumps(record or self.record))
        with (
            patch.object(service.urllib.request, "urlopen", return_value=self.response()),
            patch.object(service, "process_ticks", return_value="123"),
            patch.object(service, "owns_listener", return_value=listening),
            patch.object(
                Path,
                "read_bytes",
                return_value=b"\0".join(os.fsencode(a) for a in self.argv) + b"\0",
            ),
        ):
            return service.probe(self.url, "gpu")

    def test_listener_health_alone_does_not_claim_a_gpu(self):
        with patch.object(service.urllib.request, "urlopen", return_value=self.response()):
            result = service.probe(self.url, "gpu")
        self.assertTrue(result["ready"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["device"], "unknown")

    def test_managed_gpu_requires_current_model_process_and_listener(self):
        self.assertTrue(self.check()["verified"])
        for field, value in (
            ("ticks", "stale"),
            ("device", "cpu"),
            ("exited", True),
            ("model", {}),
        ):
            with self.subTest(field=field):
                self.assertFalse(self.check(dict(self.record, **{field: value}))["ready"])
        self.assertFalse(self.check(listening=False)["ready"])
        self.model.write_bytes(b"different model")
        self.assertFalse(self.check()["ready"])

    def test_invalid_health_and_invalid_receipts_never_look_ready(self):
        with patch.object(
            service.urllib.request, "urlopen", return_value=self.response("loading model")
        ):
            self.assertFalse(service.probe(self.url, "gpu")["ready"])
        (self.root / "asr-gpu.json").write_text("null")
        with patch.object(service.urllib.request, "urlopen", return_value=self.response()):
            self.assertFalse(service.probe(self.url, "gpu")["ready"])


class RouterTests(unittest.TestCase):
    def test_sustained_slow_cpu_primary_temporarily_uses_small_standby(self):
        original = runtime_config.get
        calls = []

        def setting(section, key):
            if (section, key) == ("asr", "primary_device"):
                return "cpu"
            if (section, key) == ("asr", "cpu_url"):
                return original("caption", "stt_url")
            return original(section, key)

        def request(*args, **kwargs):
            calls.append(kwargs)
            return [(0, "retained")]

        with (
            patch("live_transcript.transcription.router.get", side_effect=setting),
            patch(
                "live_transcript.transcription.router.time.monotonic",
                side_effect=[0, 0, 2, 3, 3, 5, 5, 6, 6, 8],
            ),
        ):
            router = ASRRouter()
            for _ in range(3):
                router.transcribe(request, "wav", "de", "", 1)
        self.assertNotIn("endpoint", calls[0])
        self.assertNotIn("endpoint", calls[1])
        self.assertEqual(calls[2]["endpoint"], original("asr", "small_cpu_url"))
        self.assertEqual(router.health["engine"], "cpu-small")
        self.assertTrue(router.health["degraded"])

    def test_cpu_primary_uses_cpu_timeout_and_truthful_health(self):
        original = runtime_config.get

        def setting(section, key):
            return "cpu" if (section, key) == ("asr", "primary_device") else original(section, key)

        calls = []
        with patch("live_transcript.transcription.router.get", side_effect=setting):
            router = ASRRouter()
            router.transcribe(
                lambda *a, **kw: calls.append(kw) or [(0, "accurate")], "wav", "de", "", 3
            )
        self.assertEqual(calls[0]["timeout"], original("asr", "cpu_timeout"))
        self.assertEqual(router.health["engine"], "cpu-primary")
        self.assertFalse(router.health["degraded"])

    def test_cpu_primary_failure_never_requeues_the_same_endpoint_as_standby(self):
        original = runtime_config.get

        def setting(section, key):
            if (section, key) == ("asr", "primary_device"):
                return "cpu"
            if (section, key) == ("asr", "cpu_url"):
                return original("caption", "stt_url")
            return original(section, key)

        calls = []

        def request(*a, **kw):
            calls.append(kw)
            if "endpoint" not in kw:
                raise ConnectionError("primary down")
            return [(0, "fallback")]

        with patch("live_transcript.transcription.router.get", side_effect=setting):
            router = ASRRouter()
            router.transcribe(request, "wav", "de", "", 3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["endpoint"], original("asr", "small_cpu_url"))

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
