"""Installation safety and real subprocess boundaries, without host provisioning."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from live_transcript.runtime.installation import (
    Installation,
    run,
    setup_options,
    specification,
    service_unit,
)
from live_transcript.runtime.provision import (
    BrowserVM,
    assert_domain_owner,
    cloud_config,
    domain_xml,
    download,
)


class InstallationTests(unittest.TestCase):
    def test_active_host_session_blocks_setup_before_provisioning(self):
        import fcntl
        from contextlib import nullcontext

        instance = self.installation()
        instance.claim()
        logs = instance.root / "data/logs"
        logs.mkdir(parents=True)
        with (logs / "run.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch(
                    "live_transcript.runtime.installation.sudo_session", return_value=nullcontext()
                ),
                patch.object(instance, "prerequisites") as prepare,
            ):
                with self.assertRaisesRegex(RuntimeError, "meeting owns"):
                    instance.apply()
            prepare.assert_not_called()

    def test_corrupt_or_unknown_installation_state_fails_before_any_commands(self):
        instance = self.installation()
        instance.state_path.write_text('{"version":99}')
        instance.runner = MagicMock()
        with self.assertRaisesRegex(ValueError, "unsupported installation state"):
            instance.claim()
        instance.runner.run.assert_not_called()
        self.assertEqual(instance.state_path.read_text(), '{"version":99}')

    def test_edited_units_are_preserved_and_install_only_does_not_enable_services(self):
        self.args.recording_only = False
        instance = self.installation()
        instance.no_start = True
        instance.claim()
        config = instance.profile_config("192.0.2.20", "192.0.2.1")
        instance.runner = MagicMock()
        with patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root / "config-home")):
            instance.units(config)
            calls = [call.args[0] for call in instance.runner.run.call_args_list]
            self.assertFalse(any("enable" in call or "start" in call for call in calls))
            path = self.root / "config-home/systemd/user" / config["services"]["captions"]
            path.write_text(path.read_text() + "# operator change\n")
            with self.assertRaisesRegex(ValueError, "was edited"):
                instance.units(config)
            self.assertIn("operator change", path.read_text())

    def test_service_health_checks_guest_caption_protocol_without_inference(self):
        import ast
        import shlex

        self.args.recording_only = False
        instance = self.installation()
        instance.claim()
        config = instance.profile_config("192.0.2.20", "192.0.2.1")
        vm = MagicMock()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"ok"}'
        with patch(
            "live_transcript.runtime.installation.urllib.request.urlopen", return_value=response
        ):
            instance.health(vm, "192.0.2.20", config)
        code = shlex.split(vm.ssh.call_args.args[1])[-1]
        tree = ast.parse(code)
        sent = [
            node.args[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sendall"
        ]
        self.assertEqual(ast.literal_eval(sent[0]), b"GETGPU\n")
        self.assertTrue(instance.state["services_verified"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="transcript setup ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        parser = argparse.ArgumentParser()
        setup_options(parser)
        self.args = parser.parse_args(
            ["--workspace", str(self.root / "workspace"), "--recording-only"]
        )
        self.profile = self.root / "profile.toml"

    def installation(self):
        profile, root, ident, options = specification(self.args, self.profile)
        root.mkdir(exist_ok=True)
        return Installation(profile, root, ident, options)

    def test_plan_creates_nothing_and_ignores_unrelated_operator_config(self):
        self.args.plan = True
        with patch.dict(os.environ, LIVE_TRANSCRIPT_CONFIG=str(self.root / "absent.toml")):
            result = run(self.args, self.profile)
        self.assertTrue(result["plan_only"])
        self.assertFalse(self.profile.exists())
        self.assertFalse(self.args.workspace.exists())

    def test_cli_setup_plan_works_before_the_requested_profile_exists(self):
        env = dict(os.environ, LIVE_TRANSCRIPT_CONFIG=str(self.root / "unrelated-absent.toml"))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "live_transcript",
                "--config",
                str(self.profile),
                "setup",
                "--plan",
                "--workspace",
                str(self.root / "data"),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["plan_only"])
        self.assertFalse(self.profile.exists())

    def test_existing_private_profile_cannot_be_adopted(self):
        self.profile.write_text("existing private settings\n")
        instance = self.installation()
        with self.assertRaisesRegex(ValueError, "existing profile"):
            instance.claim()
        self.assertEqual(self.profile.read_text(), "existing private settings\n")
        self.assertFalse(instance.state_path.exists())

    def test_rerun_preserves_identity_ports_and_rejects_changed_plan(self):
        first = self.installation()
        first.claim()
        expected = dict(first.state)
        second = self.installation()
        second.claim()
        self.assertEqual(second.state, expected)
        second.options = dict(second.options, disk_gib=128)
        with self.assertRaisesRegex(ValueError, "different setup plan"):
            second.claim()

    def test_modified_managed_profile_is_never_overwritten(self):
        instance = self.installation()
        instance.claim()
        instance.profile_config("192.0.2.20", "192.0.2.1")
        self.profile.write_text(self.profile.read_text() + "# user customization\n")
        with self.assertRaisesRegex(ValueError, "profile was changed"):
            self.installation().claim()
        self.assertIn("user customization", self.profile.read_text())

    def test_generated_recording_profile_is_valid_and_uses_local_storage(self):
        from live_transcript.core.config import load

        instance = self.installation()
        instance.claim()
        instance.profile_config("192.0.2.20", "192.0.2.1")
        config = load(self.profile)
        self.assertEqual(config["asr"]["primary_device"], "cpu")
        self.assertEqual(config["paths"]["recordings"], config["paths"]["archive_destination"])
        self.assertEqual(config["paths"]["archive_mount"], "")
        self.assertTrue(all(not value for value in config["services"].values()))
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o600)
        self.assertIn("ssh-key", config["vm"]["identity_file"])

    def test_domain_collision_never_mutates_an_unowned_vm(self):
        instance = self.installation()
        instance.claim()
        runner = MagicMock()
        runner.virsh.return_value = SimpleNamespace(
            returncode=0, stdout=b"<domain><metadata/></domain>"
        )
        vm = BrowserVM(runner, instance.state, instance.root, instance.options)
        with self.assertRaisesRegex(ValueError, "another installation"):
            vm.create()
        runner.run.assert_not_called()
        self.assertEqual(runner.virsh.call_count, 1)

    def test_setup_limits_reject_destructive_or_invalid_inputs(self):
        for key, value in (
            ("vm_name", "../existing"),
            ("network", "--delete"),
            ("memory_mib", 1),
            ("disk_gib", 1),
            ("vcpus", 0),
            ("build_jobs", 64),
        ):
            with self.subTest(key=key):
                changed = argparse.Namespace(**vars(self.args))
                setattr(changed, key, value)
                with self.assertRaises(ValueError):
                    specification(changed, self.profile)

    def test_service_values_escape_spaces_percent_and_never_call_a_shell(self):
        unit = service_unit(
            Path("/tmp/profile with 20%.toml"), "fixture", ["asr", "--device", "gpu"]
        )
        self.assertIn("20%%.toml", unit)
        self.assertIn('"--config"', unit)
        self.assertNotIn("bash -c", unit)
        with self.assertRaises(ValueError):
            service_unit(Path("bad\nprofile"), "fixture", [])


class DownloadTests(unittest.TestCase):
    def test_download_rejects_partial_symlinks_without_touching_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "retain.txt"
            outside.write_text("original")
            path = root / "model.bin"
            path.with_name("model.bin.download").symlink_to(outside)
            with self.assertRaises(ValueError):
                download("https://example.invalid/model", path, "0" * 64)
            self.assertEqual(outside.read_text(), "original")

    def test_compose_contains_only_restricted_cpu_speech_services(self):
        from live_transcript.runtime.containers import configuration

        config = configuration("/models with spaces", [19091, 19092, 19093], "fixture")
        self.assertEqual(set(config["services"]), {"primary", "cpu_small"})
        self.assertIn("build", config["services"]["primary"])
        self.assertNotIn("build", config["services"]["cpu_small"])
        for service in config["services"].values():
            self.assertTrue(service["read_only"])
            self.assertNotIn("privileged", service)
            self.assertNotIn("devices", service)
            self.assertNotIn("network_mode", service)
            self.assertTrue(all(port.startswith("127.0.0.1:") for port in service["ports"]))
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertTrue(service["volumes"][0]["read_only"])
            self.assertIn("--no-gpu", service["command"])
        with self.assertRaises(ValueError):
            configuration("/models", [1, 1, 2], "fixture")

    def test_failed_checksum_never_publishes_a_partial_download(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.bin"
            response = io.BytesIO(b"incorrect contents")
            with patch(
                "live_transcript.runtime.provision.urllib.request.urlopen", return_value=response
            ):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    download("https://example.invalid/model", path, "0" * 64)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name("model.bin.download").exists())

    def test_verified_cache_works_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.bin"
            path.write_bytes(b"verified model")
            with patch(
                "live_transcript.runtime.provision.urllib.request.urlopen",
                side_effect=AssertionError("network"),
            ):
                self.assertEqual(
                    download(
                        "https://example.invalid/model",
                        path,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    ),
                    path,
                )

    def test_cloud_seed_separates_operator_and_browser_privileges(self):
        data = json.loads(
            cloud_config(
                "bot-public", "host-private", "host-public", "owned-id", "admin-public"
            ).split("\n", 1)[1]
        )
        admin, bot = data["users"]
        self.assertEqual(admin["ssh_authorized_keys"], ["admin-public"])
        self.assertEqual(bot["ssh_authorized_keys"], ["bot-public"])
        self.assertNotIn("NOPASSWD:ALL", json.dumps(bot))
        self.assertFalse(data["ssh_pwauth"])
        self.assertTrue(bot["lock_passwd"])
        self.assertNotIn("host-private", json.dumps(bot))

    def test_owned_domain_keeps_disks_independent_and_escapes_xml(self):
        xml = domain_xml(
            "fixture",
            "owned",
            "/volume/a&b/system.qcow2",
            "/volume/seed.iso",
            "default",
            4096,
            2,
            "52:54:00:01:02:03",
        )
        assert_domain_owner(xml, "owned")
        self.assertIn("a&amp;b", xml)
        self.assertNotIn("backingStore", xml)
        self.assertNotIn("filesystem", xml)
        with self.assertRaises(ValueError):
            assert_domain_owner(xml, "different")


if __name__ == "__main__":
    unittest.main()
