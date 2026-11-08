import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from live_transcript.runtime import stack


class StackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (*stack.CODE_FILES, "application.whl"):
            (self.root / name).write_text("original " + name)
        self.vm = dict(
            image="vm:retained", volumes=[dict(source=str(self.root / "vm"), target="/vm")]
        )
        compose = dict(
            name="fixture-installation",
            services={
                **{
                    name: dict(image=name + ":original")
                    for name in ("app", "workspace", "captions")
                },
                "vm": self.vm,
            },
        )
        (self.root / "compose.json").write_text(json.dumps(compose))
        self.state = stack.register(self.root)

    def prepare(self):
        def wheel(directory):
            path = directory / "application.whl"
            path.write_text("new reviewed wheel")
            return path

        def bundle(path):
            path.write_text("new backend")

        def dockerfile(directory):
            (directory / "application.Dockerfile").write_text("new image")

        with (
            patch("live_transcript.runtime.deployment.build_application_wheel", side_effect=wheel),
            patch("live_transcript.runtime.deployment.build_bundle", side_effect=bundle),
            patch(
                "live_transcript.runtime.containers.write_application_dockerfile",
                side_effect=dockerfile,
            ),
        ):
            return stack.prepare_update(self.root)

    def test_updates_and_rollbacks_preserve_vm_configuration_and_operator_tuning(self):
        compose = json.loads((self.root / "compose.json").read_text())
        compose["services"]["app"]["cpus"] = 3
        (self.root / "compose.json").write_text(json.dumps(compose))
        result = self.prepare()
        self.assertTrue(result["changed"])
        after = json.loads((self.root / "compose.json").read_text())
        self.assertEqual(after["services"]["vm"], self.vm)
        self.assertEqual(after["services"]["app"]["cpus"], 3)
        stack.commit_update(self.root)
        self.assertNotEqual(stack.manifest(self.root)["revision"], self.state["revision"])
        stack.rollback(self.root)
        self.assertEqual((self.root / "application.whl").read_text(), "original application.whl")
        self.assertEqual(
            json.loads((self.root / "compose.json").read_text())["services"]["vm"], self.vm
        )

    def test_edited_code_is_refused_before_any_pending_release_is_written(self):
        (self.root / "backend.pyz").write_text("operator edit")
        with self.assertRaisesRegex(ValueError, "edited"):
            self.prepare()
        self.assertFalse((self.root / "pending-release.json").exists())

    def test_pending_update_can_be_rolled_back_after_partial_artifact_publication(self):
        self.prepare()
        (self.root / "application.whl").write_text("partial")
        stack.rollback(self.root)
        self.assertEqual((self.root / "application.whl").read_text(), "original application.whl")
        self.assertFalse((self.root / "pending-release.json").exists())

    def test_commit_without_a_pending_update_is_idempotent(self):
        self.assertFalse(stack.commit_update(self.root)["changed"])

    def test_invalid_artifact_paths_are_refused_before_restoring_a_release(self):
        value = stack.manifest(self.root)
        value["files"]["../unrelated.whl"] = value["files"].pop("application.whl")
        (self.root / "deployment.json").write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            stack.manifest(self.root)

    def test_startup_budget_uses_configuration_and_only_stopped_services(self):
        (self.root / "vm").mkdir()
        (self.root / "vm.json").write_text('{"memory_mib":4096}')
        (self.root / "application.toml").write_text(
            "[storage]\nhost_free_reserve_bytes=0\n[capacity]\nstartup_app_mib=1024\n"
        )
        with patch(
            "live_transcript.runtime.capacity.memory_available", return_value=8192 * 1024**2
        ):
            report = stack.startup_capacity(
                self.root, ["vm", "primary", "cpu_small", "captions", "workspace"]
            )
            self.assertEqual(report["required_bytes"], 1024 * 1024**2)
            with self.assertRaises(RuntimeError):
                stack.startup_capacity(self.root, [])

    def stop_fixture(self):
        compose = json.loads((self.root / "compose.json").read_text())
        compose["services"]["app"]["volumes"] = [
            dict(source=str(self.root / "data"), target="/data"),
            dict(source=str(self.root / "keys"), target="/credentials"),
        ]
        (self.root / "compose.json").write_text(json.dumps(compose))
        (self.root / "application.toml").write_text(
            '[paths]\nroot="/data"\n[vm]\nssh="transcript@192.0.2.1"\nidentity_file="/credentials/key"\nknown_hosts="/credentials/hosts"\npython="python3"\nhome="/home/transcript"\n'
        )

    def test_shutdown_requires_idle_guest_even_when_host_bookkeeping_is_missing(self):
        self.stop_fixture()
        with patch.object(
            stack.subprocess, "run", return_value=type("Result", (), {"returncode": 1})()
        ):
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                stack.guard_stop(self.root, ["vm"])
        with patch.object(stack.subprocess, "run") as probe:
            self.assertEqual(stack.guard_stop(self.root, [])["vm"], "stopped")
            probe.assert_not_called()

    def test_shutdown_lease_excludes_a_new_host_launch(self):
        self.stop_fixture()
        with stack.launch_lock(self.root):
            with self.assertRaises(BlockingIOError):
                with stack.launch_lock(self.root):
                    self.fail("another launch acquired ownership")

    def test_release_lock_excludes_a_second_mutation(self):
        with stack.release_lock(self.root):
            with self.assertRaises(BlockingIOError):
                with stack.release_lock(self.root):
                    self.fail("second writer")
