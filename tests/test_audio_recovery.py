from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from live_transcript.capture import audio


class AudioRecoveryTests(unittest.TestCase):
    def test_startup_devices_are_explicit_and_do_not_suspend_or_autodiscover(self):
        value = audio.startup_script("meeting_sink", "hw:0,0", "meeting_mic")
        self.assertIn("module-null-sink sink_name=meeting_sink", value)
        self.assertIn("module-alsa-source device=hw:0,0 source_name=meeting_mic", value)
        self.assertIn("set-default-source meeting_mic", value)
        self.assertNotIn("module-udev-detect", value)
        self.assertNotIn("module-suspend-on-idle", value)
        for bad in ("x\nload-module hostile", "x;evil", "../x"):
            with self.assertRaises(ValueError):
                audio.startup_script(bad, "hw:0,0", "mic")

    def test_active_archive_refuses_topology_changes_before_writing_files(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(audio.Path, "home", return_value=Path(directory)),
            patch.object(
                audio,
                "run",
                return_value=SimpleNamespace(stdout="live-transcript-archive-fixture.service"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "active"):
                audio.configure()
            self.assertFalse((Path(directory) / ".config").exists())
