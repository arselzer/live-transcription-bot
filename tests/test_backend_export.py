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
    def test_complete_application_export_keeps_vm_credentials_outside_the_image(self):
        import copy
        from live_transcript.core import config
        from live_transcript.runtime.containers import application_options, export_application
        from live_transcript.runtime.installation import MODELS

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            models.mkdir()
            for _, _, name, _ in MODELS.values():
                (models / name).write_text("fixture weights")
            key, known = root / "identity", root / "known_hosts"
            key.write_text("fixture private key never enters the image")
            known.write_text("fixture host trust")
            values = copy.deepcopy(config.DEFAULTS)
            values["vm"].update(
                ssh="transcript@192.0.2.20", identity_file=str(key), known_hosts=str(known)
            )
            parser = argparse.ArgumentParser()
            application_options(parser)
            args = parser.parse_args(
                [
                    str(root / "bundle"),
                    "--models",
                    str(models),
                    "--data",
                    str(root / "data"),
                    "--bind",
                    "192.0.2.1",
                ]
            )
            checksums = {name: checksum for _, _, name, checksum in MODELS.values()}
            with (
                patch.object(config, "CONFIG", values),
                patch(
                    "live_transcript.runtime.provision.sha256",
                    side_effect=lambda p: checksums[p.name],
                ),
            ):
                result = export_application(args)
            self.assertTrue(result["application"])
            self.assertFalse(result["vm_managed"])
            compose = json.loads((root / "bundle/compose.json").read_text())
            application = compose["services"]["app"]
            self.assertEqual(application["build"]["target"], "wheel")
            self.assertNotIn("privileged", application)
            self.assertNotIn("devices", application)
            self.assertTrue(
                any(
                    v["target"] == "/credentials/identity_file" and v["read_only"]
                    for v in application["volumes"]
                )
            )
            ignore = (root / "bundle/.dockerignore").read_text()
            self.assertNotIn("!application.toml", ignore)
            with zipfile.ZipFile(next((root / "bundle").glob("*.whl"))) as wheel:
                contents = b"".join(wheel.read(n) for n in wheel.namelist())
                self.assertNotIn(key.read_bytes(), contents)
                self.assertIn("live_transcript/runtime/launch.sh", wheel.namelist())
            generated = config.load(root / "bundle/application.toml")
            self.assertEqual(generated["runtime"]["supervisor"], "app")
            self.assertFalse(generated["vm"]["manage_libvirt"])

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
