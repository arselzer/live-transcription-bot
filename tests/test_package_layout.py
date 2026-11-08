"""Deployment is one reviewed package bundle, without legacy script aliases."""

import importlib
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from live_transcript.runtime.deployment import build_bundle

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_source_distribution_includes_the_entire_reviewed_inventory(self):
        from live_transcript.core.source_files import approved_paths

        includes = {
            line.removeprefix("include ").strip()
            for line in (ROOT / "MANIFEST.in").read_text().splitlines()
            if line.startswith("include ")
        }
        self.assertEqual(includes, set(approved_paths(ROOT)))

    def test_guest_bundle_is_reproducible_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.pyz"
            second = Path(directory) / "second.pyz"
            build_bundle(first)
            build_bundle(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)

    def test_package_and_development_tools_do_not_import_retired_modules(self):
        for folder in (ROOT / "src/live_transcript", ROOT / "tools"):
            for path in folder.rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text())):
                    modules = (
                        [node.module]
                        if isinstance(node, ast.ImportFrom) and not node.level
                        else [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else []
                    )
                    for module in modules:
                        if not module or not module.startswith(("live_transcript", "tools")):
                            continue
                        base = (
                            ROOT
                            / ("src" if module.startswith("live_transcript") else "")
                            / module.replace(".", "/")
                        )
                        self.assertTrue(
                            base.with_suffix(".py").is_file() or (base / "__init__.py").is_file(),
                            str(path.relative_to(ROOT)) + ": " + module,
                        )

    def test_operator_modules_are_safe_to_import(self):
        with (
            patch("subprocess.run", side_effect=AssertionError("import attempted an operation")),
            patch("os.kill", side_effect=AssertionError("import sent a signal")),
        ):
            for module in ("capture.audio", "providers.video", "runtime.postprocess"):
                name = "live_transcript." + module
                sys.modules.pop(name, None)
                self.assertTrue(callable(importlib.import_module(name).main))

    def test_guest_bundle_runs_without_an_installation_or_source_wrappers(self):
        with tempfile.TemporaryDirectory(prefix="guest package with spaces ") as directory:
            directory = Path(directory)
            bundle = directory / "live-transcript.pyz"
            build_bundle(bundle)
            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                self.assertIn("live_transcript/providers/google_meet.py", names)
                self.assertFalse(
                    any(n.startswith(("tools/", "recordings/", "models/")) for n in names)
                )
                self.assertNotIn("join_meeting.py", names)
                self.assertNotIn("_bootstrap.py", names)
                self.assertEqual(set(n for n in names if "/" not in n), {"__main__.py"})
            config = directory / "config.toml"
            config.write_text("[paths]\nroot=" + json.dumps(str(directory)) + "\n")
            env = dict(os.environ, LIVE_TRANSCRIPT_CONFIG=str(config))
            env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, "-I", str(bundle), "config"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            self.assertEqual(json.loads(result.stdout)["paths"]["root"], str(directory))

    def fake_package(self, root, extra=""):
        package = root / "src/live_transcript"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (root / "PUBLIC_SOURCE_FILES.txt").write_text(
            "PUBLIC_SOURCE_FILES.txt\nsrc/live_transcript/__init__.py\n" + extra
        )
        return package

    def test_guest_bundle_excludes_unlisted_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.fake_package(root)
            (root / "config.toml").write_text("private fixture")
            (root / "recordings").mkdir()
            (root / "recordings/private.txt").write_text("private fixture")
            names = build_bundle(root / "bundle.pyz", package_dir=package)
            self.assertEqual(set(names), {"live_transcript/__init__.py", "__main__.py"})

    def test_guest_bundle_rejects_unreviewed_or_symlinked_python_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.fake_package(root)
            (package / "unexpected.py").write_text("unreviewed")
            with self.assertRaises(ValueError):
                build_bundle(root / "bundle.pyz", package_dir=package)
            (package / "unexpected.py").unlink()
            (package / "__init__.py").unlink()
            (root / "outside").write_text("private fixture")
            (package / "__init__.py").symlink_to(root / "outside")
            with self.assertRaises(ValueError):
                build_bundle(root / "bundle.pyz", package_dir=package)


class WorkerTests(unittest.TestCase):
    def test_external_interpreter_can_run_checkout_without_installing_package(self):
        import venv
        from live_transcript.worker import command, command_env

        with tempfile.TemporaryDirectory(prefix="external python ") as directory:
            root = Path(directory)
            venv.EnvBuilder(with_pip=False, symlinks=True).create(root / "venv")
            python = str(root / "venv/bin/python")
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("PYTHONPATH", "LIVE_TRANSCRIPT_BUNDLE")
            }
            invocation = command("config", python=python)
            missing = subprocess.run(
                invocation, cwd=root, env=env, capture_output=True, text=True, timeout=15
            )
            self.assertNotEqual(missing.returncode, 0)
            with patch.dict(os.environ, env, clear=True):
                ready = subprocess.run(
                    invocation,
                    cwd=root,
                    env=command_env(python),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertEqual(json.loads(ready.stdout)["google"]["input_mode"], "native")

    def test_external_interpreter_never_inherits_foreign_site_packages(self):
        import live_transcript.worker as worker

        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / "site-packages/live_transcript/worker.py"
            installed.parent.mkdir(parents=True)
            installed.write_text("# installed fixture\n")
            with patch.object(worker, "__file__", str(installed)):
                self.assertIsNone(worker.command_env("other-python"))
        self.assertIsNone(worker.command_env())
        with patch.dict(os.environ, LIVE_TRANSCRIPT_BUNDLE="/guest/live-transcript.pyz"):
            self.assertIsNone(worker.command_env("other-python"))

    def test_legacy_working_directory_cannot_shadow_the_reviewed_child_package(self):
        from live_transcript.runtime.launcher import environment
        from live_transcript.worker import command, command_env

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shadow = root / "live_transcript"
            shadow.mkdir()
            (shadow / "__init__.py").write_text("raise RuntimeError('legacy package selected')")
            for env in (
                command_env(sys.executable),
                environment(dict(os.environ, TEST_MARKER="kept")),
            ):
                result = subprocess.run(
                    command("config"), cwd=root, env=env, capture_output=True, text=True, timeout=15
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["google"]["input_mode"], "native")

    def test_children_stay_in_the_same_guest_bundle(self):
        from live_transcript.worker import command

        with patch.dict(os.environ, {"LIVE_TRANSCRIPT_BUNDLE": "/guest/live-transcript.pyz"}):
            self.assertEqual(
                command("render", "--example", python="python3"),
                ["python3", "-u", "/guest/live-transcript.pyz", "render", "--example"],
            )
        with patch.dict(os.environ, clear=True):
            self.assertEqual(
                command("config", python="python3"),
                ["python3", "-u", "-m", "live_transcript.worker", "config"],
            )
        with self.assertRaises(ValueError):
            command("unknown")

    def test_process_ownership_rejects_role_names_in_unrelated_arguments(self):
        from live_transcript.worker import is_role_process

        for argv in (
            ["python3", "-u", "-m", "live_transcript.worker", "stream"],
            [b"/venv/bin/python3.11", b"-u", b"/guest/live-transcript.pyz", b"stream", b""],
        ):
            self.assertTrue(is_role_process(argv, {"stream"}))
            self.assertFalse(is_role_process(argv, {"join"}))
        for argv in (
            [],
            ["python3", "-m", "live_transcript.worker"],
            ["echo", "python3", "-m", "live_transcript.worker", "stream"],
            ["python3", "unrelated.py", "/guest/live-transcript.pyz", "stream"],
            ["python3", "-c", "print(1)", "-m", "live_transcript.worker", "stream"],
        ):
            self.assertFalse(is_role_process(argv, {"stream"}), argv)

    def test_dispatch_forwards_arguments_and_restores_argv_on_failure(self):
        from live_transcript.worker import main

        original = sys.argv

        def run(*args, **kwargs):
            self.assertEqual(args, ("live_transcript.core.config",))
            self.assertEqual(sys.argv[1:], ["--shell"])
            self.assertTrue(kwargs["alter_sys"])
            raise RuntimeError("fixture")

        with patch("live_transcript.worker.runpy.run_module", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                main(["config", "--shell"])
        self.assertIs(sys.argv, original)

    def test_job_listing_uses_update_time_and_omits_private_error_text(self):
        from live_transcript.storage.worker import recent_jobs

        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            (logs / "recovery-z.json").write_text(json.dumps({"status": "complete", "utc": 1}))
            (logs / "recovery-a.json").write_text(
                json.dumps(
                    {
                        "status": "postprocessing",
                        "utc": 9,
                        "source": "private invitation",
                        "error": "private error",
                    }
                )
            )
            (logs / "postprocess-broken.json").write_text("{")
            with patch("live_transcript.storage.worker.get", return_value=directory):
                latest = recent_jobs(limit=1)
                all_jobs = recent_jobs()
                with self.assertRaises(ValueError):
                    recent_jobs(limit=0)
            self.assertEqual(latest[0]["job"], "recovery-a")
            self.assertEqual(latest[0]["status"], "postprocessing")
            self.assertIn("unreadable", [row["status"] for row in all_jobs])
            self.assertNotIn("private error", json.dumps(all_jobs))
            self.assertNotIn("private invitation", json.dumps(all_jobs))
