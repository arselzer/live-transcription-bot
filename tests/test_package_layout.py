import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from live_transcript.runtime.deployment import build_bundle

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_guest_bundle_bytes_do_not_depend_on_output_name_or_local_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            first = directory / "first.tar.gz"
            second = directory / "second.tar.gz"
            build_bundle(ROOT, first)
            build_bundle(ROOT, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)

    def test_operator_modules_are_safe_to_import(self):
        with (
            patch("subprocess.run", side_effect=AssertionError("import attempted an operation")),
            patch("os.kill", side_effect=AssertionError("import sent a signal")),
        ):
            for module in (
                "capture.audio",
                "capture.pulse",
                "providers.video",
                "runtime.postprocess",
            ):
                name = "live_transcript." + module
                sys.modules.pop(name, None)
                imported = importlib.import_module(name)
                self.assertTrue(callable(imported.main))

    def test_guest_bundle_bootstraps_its_own_package_without_installation(self):
        with tempfile.TemporaryDirectory(prefix="guest package with spaces ") as directory:
            directory = Path(directory)
            bundle = directory / "guest.tar.gz"
            build_bundle(ROOT, bundle)
            guest = directory / "guest"
            guest.mkdir()
            with tarfile.open(bundle) as archive:
                names = archive.getnames()
                self.assertTrue(
                    all(m.isfile() and not m.uname and not m.gname for m in archive.getmembers())
                )
                self.assertNotIn("config.toml", names)
                self.assertFalse(
                    any(n.startswith(("recordings/", "models/", ".git/")) for n in names)
                )
                archive.extractall(guest, filter="data")
            config = directory / "config.toml"
            config.write_text("[paths]\nroot = " + json.dumps(str(guest)) + "\n")
            env = dict(os.environ, LIVE_TRANSCRIPT_CONFIG=str(config))
            env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import join_meeting,runtime_config,json; "
                    "print(json.dumps([join_meeting.__name__,runtime_config.__file__]))",
                ],
                cwd=guest,
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            module, path = json.loads(result.stdout)
            self.assertEqual(module, "live_transcript.providers.zoom")
            self.assertTrue(Path(path).is_relative_to(guest))

    def test_guest_bundle_excludes_unlisted_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            root = directory / "source"
            root.mkdir()
            for name in (
                "_bootstrap.py",
                "pyproject.toml",
                "orphan_sweep.sh",
                "prep_remote.sh",
                "vm_pa_fix.sh",
                "vm_stop_joiner.sh",
            ):
                (root / name).write_text("# fixture\n")
            (root / "docs").mkdir()
            (root / "docs/module-map.json").write_text("{}")
            (root / "src/live_transcript").mkdir(parents=True)
            (root / "src/live_transcript/__init__.py").write_text("")
            (root / "tools").mkdir()
            (root / "config.toml").write_text("private fixture")
            (root / "recordings").mkdir()
            (root / "recordings/private.txt").write_text("private fixture")
            selected = [
                p.relative_to(root).as_posix()
                for p in root.rglob("*")
                if p.is_file() and p.name != "config.toml" and "recordings" not in p.parts
            ]
            (root / "PUBLIC_SOURCE_FILES.txt").write_text(
                "PUBLIC_SOURCE_FILES.txt\n" + "\n".join(selected) + "\n"
            )
            names = build_bundle(root, directory / "guest.tar.gz")
            self.assertNotIn("config.toml", names)
            self.assertNotIn("recordings/private.txt", names)

    def test_guest_bundle_refuses_symlinked_source(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "docs").mkdir()
            (directory / "docs/module-map.json").write_text("{}")
            (directory / "target").write_text("not source")
            (directory / "_bootstrap.py").symlink_to(directory / "target")
            (directory / "PUBLIC_SOURCE_FILES.txt").write_text(
                "PUBLIC_SOURCE_FILES.txt\n_bootstrap.py\npyproject.toml\ndocs/module-map.json\norphan_sweep.sh\nprep_remote.sh\nvm_pa_fix.sh\nvm_stop_joiner.sh\n"
            )
            with self.assertRaises(ValueError):
                build_bundle(directory, directory / "guest.tar.gz")
