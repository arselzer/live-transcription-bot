"""Portable backend generation never adopts services, profiles or credentials."""

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from live_transcript.runtime.containers import export_options, export_backend, backend_configuration


class BackendExportTests(unittest.TestCase):
    def test_native_and_container_decoder_cannot_claim_the_same_journal(self):
        from live_transcript.transcription.store import journal_owner

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            with journal_owner(path):
                with self.assertRaisesRegex(RuntimeError, "Another caption service"):
                    with journal_owner(path):
                        self.fail("second decoder acquired journal ownership")
                self.assertFalse(path.exists())
            with journal_owner(path):
                self.assertEqual(Path(str(path) + ".owner.lock").stat().st_mode & 0o777, 0o600)

    def test_backend_keeps_speech_private_and_ingest_independent_of_speech_readiness(self):
        result = backend_configuration("/models", "/state", "192.0.2.1", 18099, "test", 1000, 1000)
        services = result["services"]
        self.assertNotIn("ports", services["primary"])
        self.assertNotIn("ports", services["cpu_small"])
        self.assertNotIn("depends_on", services["captions"])
        self.assertEqual(services["captions"]["ports"], ["192.0.2.1:18099:8099"])
        for service in services.values():
            self.assertNotIn("privileged", service)
            self.assertNotIn("devices", service)
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
        for bad in ("0.0.0.0", "224.0.0.1", "::1", "example.invalid"):
            with self.assertRaises(ValueError):
                backend_configuration("/models", "/state", bad, 18099, "test", 1000, 1000)

    def test_export_produces_only_code_and_backend_config_and_preserves_existing_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            models.mkdir()
            for name in ("ggml-large-v3.bin", "ggml-small.bin", "ggml-silero-v6.2.0.bin"):
                (models / name).write_text("weight fixture")
            parser = argparse.ArgumentParser()
            export_options(parser)
            args = parser.parse_args(
                [str(root / "export"), "--models", str(models), "--state", str(root / "state")]
            )
            # Weight verification is separately exercised by download tests; no
            # model/network/GPU access in an offline packaging regression.
            from live_transcript.runtime.installation import MODELS

            checksums = iter(value[3] for value in MODELS.values())
            with patch(
                "live_transcript.runtime.provision.sha256", side_effect=lambda p: next(checksums)
            ):
                result = export_backend(args)
            self.assertFalse(result["started"])
            text = (root / "export/backend.toml").read_text()
            self.assertIn("enabled = false", text)
            self.assertNotIn("identity", text)
            self.assertNotIn("ssh", text)
            with zipfile.ZipFile(root / "export/backend.pyz") as bundle:
                self.assertTrue(all(name.endswith(".py") for name in bundle.namelist()))
            content = json.loads((root / "export/compose.json").read_text())
            self.assertEqual(content["services"]["captions"]["volumes"][0]["target"], "/state")
            with self.assertRaisesRegex(ValueError, "empty directory"):
                export_backend(args)

    def test_missing_weights_fail_before_creating_bundle_or_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parser = argparse.ArgumentParser()
            export_options(parser)
            args = parser.parse_args(
                [
                    str(root / "export"),
                    "--models",
                    str(root / "models"),
                    "--state",
                    str(root / "state"),
                ]
            )
            with self.assertRaisesRegex(ValueError, "unverified model"):
                export_backend(args)
            self.assertFalse((root / "export").exists())
            self.assertFalse((root / "state").exists())
