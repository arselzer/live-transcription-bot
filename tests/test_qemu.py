"""Managed guests cannot adopt/overwrite disks or share credentials with the app."""

import argparse
import copy
import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from live_transcript.core import config
from live_transcript.runtime import qemu
from live_transcript.runtime.containers import application_options, export_application


class ManagedVMTests(unittest.TestCase):
    def prepare(self, root, **overrides):
        return qemu.prepare(
            root,
            **dict(
                memory_mib=4096,
                vcpus=2,
                disk_gib=64,
                ssh_port=22222,
                caption_port=8099,
                **overrides,
            ),
        )

    def test_export_is_resumable_but_never_rotates_keys_or_resizes_retained_vm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vm"
            spec = self.prepare(root)
            secret = (root / "credentials/operator").read_bytes()
            disk = root / "system.qcow2"
            disk.write_bytes(b"retained evidence")
            (root / "seed.iso").write_bytes(b"retained identity")
            self.assertEqual(self.prepare(root), spec)
            self.assertEqual((root / "credentials/operator").read_bytes(), secret)
            with patch.object(qemu, "download") as download, patch.object(subprocess, "run") as run:
                qemu.initialize_disk(root, spec)
            download.assert_not_called()
            run.assert_not_called()
            self.assertEqual(disk.read_bytes(), b"retained evidence")
            with self.assertRaisesRegex(ValueError, "settings differ"):
                qemu.prepare(
                    root, memory_mib=8192, vcpus=2, disk_gib=64, ssh_port=22222, caption_port=8099
                )

    def test_unowned_storage_symlinks_and_active_runner_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vm"
            root.mkdir(mode=0o700)
            (root / "system.qcow2").write_bytes(b"existing")
            with self.assertRaisesRegex(ValueError, "adopt"):
                self.prepare(root)
            (root / "system.qcow2").unlink()
            self.prepare(root)
            with (root / "runner.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    self.prepare(root)
            (root / "system.qcow2").symlink_to(root / "owner.json")
            with self.assertRaisesRegex(ValueError, "link"):
                self.prepare(root)

    def test_bootstrap_and_ssh_share_pinned_identity_with_distinct_host_port(self):
        from live_transcript.runtime.provision import cloud_config

        value = json.loads(
            cloud_config(
                "public", "private", "host-public", "owner", "admin", gateway_alias=True
            ).split("\n", 1)[1]
        )
        self.assertFalse(value["manage_etc_hosts"])
        self.assertIn("route", value["runcmd"][0][-1])
        self.assertIn("live-transcript-host", value["runcmd"][0][-1])
        # Validate the code without executing privileged bootstrap steps.
        compile(value["runcmd"][0][-1], "gateway", "exec")
        values = copy.deepcopy(config.DEFAULTS)
        values["vm"]["port"] = 22222
        values["_file"] = "fixture.toml"
        with patch.object(config, "CONFIG", values):
            self.assertIn("Port=22222", config.ssh_options())
            self.assertIn("LT_SSH_PORT=22222", config.shell_values())

    def test_managed_compose_separates_vm_disk_and_administrative_key(self):
        from live_transcript.runtime.installation import MODELS

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            models.mkdir()
            for _, _, name, _ in MODELS.values():
                (models / name).write_bytes(b"model fixture")
            parser = argparse.ArgumentParser()
            application_options(parser)
            args = parser.parse_args(
                [
                    str(root / "stack"),
                    "--models",
                    str(models),
                    "--data",
                    str(root / "data"),
                    "--managed-vm",
                    "--kvm-gid",
                    "108",
                ]
            )
            checksums = {name: checksum for _, _, name, checksum in MODELS.values()}
            with (
                patch.object(config, "CONFIG", copy.deepcopy(config.DEFAULTS)),
                patch(
                    "live_transcript.runtime.provision.sha256",
                    side_effect=lambda p: checksums[p.name],
                ),
            ):
                result = export_application(args)
            self.assertTrue(result["vm_managed"])
            services = json.loads((root / "stack/compose.json").read_text())["services"]
            vm, app = services["vm"], services["app"]
            self.assertEqual(vm["devices"], ["/dev/kvm:/dev/kvm"])
            self.assertNotIn("network_mode", vm)
            self.assertNotIn("privileged", vm)
            self.assertEqual(vm["cap_drop"], ["ALL"])
            self.assertEqual(vm["ports"], ["127.0.0.1:22222:2222"])
            self.assertNotIn("depends_on", vm)
            self.assertNotIn("devices", app)
            self.assertEqual(app["depends_on"]["vm"]["condition"], "service_healthy")
            self.assertFalse(any(v["target"] == "/vm" for v in app["volumes"]))
            self.assertFalse(any("admin" in v["source"] for v in app["volumes"]))
            self.assertEqual(services["captions"]["ports"], ["127.0.0.1:8099:8099"])
            generated = config.load(root / "stack/application.toml")
            self.assertEqual(generated["vm"]["port"], 22222)
            self.assertEqual(generated["caption"]["guest_host"], "live-transcript-host")
            with patch.object(config, "CONFIG", generated):
                guest = root / "guest.toml"
                config.write_vm_config(guest)
            self.assertIn('host = "live-transcript-host"', guest.read_text())
            ignored = (root / "stack/.dockerignore").read_text()
            self.assertIn("!vm.Dockerfile", ignored)
            self.assertNotIn("!vm.json", ignored)

    def test_repairs_cannot_touch_initialized_guests_and_admin_payload_uses_pinned_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vm"
            spec = self.prepare(root)
            (root / "initialized.json").write_text("{}")
            with patch.object(qemu, "ssh") as ssh:
                with self.assertRaisesRegex(ValueError, "initialized"):
                    qemu.retry_setup(root, spec)
            ssh.assert_not_called()
            with patch.object(subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertTrue(qemu.ssh(root, "bash -s", input=b"guarded setup", admin=True))
            args, values = run.call_args
            self.assertIn("root@127.0.0.1", args[0])
            self.assertIn(str(root / "credentials/admin"), args[0])
            self.assertEqual(values["input"], b"guarded setup")

    def test_overlapping_vm_and_application_storage_fails_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parser = argparse.ArgumentParser()
            application_options(parser)
            args = parser.parse_args(
                [
                    str(root / "stack"),
                    "--models",
                    str(root / "models"),
                    "--data",
                    str(root / "data"),
                    "--managed-vm",
                    "--vm-data",
                    str(root / "data/vm"),
                ]
            )
            with self.assertRaisesRegex(ValueError, "separate"):
                export_application(args)
            self.assertFalse((root / "data").exists())
            self.assertFalse((root / "stack").exists())


if __name__ == "__main__":
    unittest.main()
