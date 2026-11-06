"""Offline regression checks. Run: python3 -m unittest test_proton_meet -v"""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import live_transcript.providers.proton as proton


class Locator:
    def __init__(self, page, key):
        self.page, self.key = page, key
        self.first = self

    def is_visible(self):
        return self.page.visibility.get(self.key, False)

    def is_enabled(self):
        return True

    def all_text_contents(self):
        return self.page.dialogs

    def fill(self, value):
        self.page.name = value

    def click(self, **kwargs):
        self.page.clicks.append(self.key)
        if self.key == "Join meeting":
            self.page.visibility["Name"] = False
            self.page.visibility["Join meeting"] = False
        if self.key == "Mute microphone":
            self.page.visibility[self.key] = False


class Page:
    def __init__(self, admit_at=3):
        self.clock = 0
        self.admit_at = admit_at
        self.visibility = {"Name": True, "Join meeting": True, "Mute microphone": True}
        self.dialogs = []
        self.clicks = []

    def get_by_role(self, role, name=None, **kwargs):
        return Locator(self, name or role)

    def locator(self, selector):
        return Locator(self, selector)

    def wait_for_timeout(self, ms):
        self.clock += ms / 1000
        if self.admit_at is not None and self.clock >= self.admit_at:
            self.visibility.update({proton.TOOLBAR: True, "Leave Meeting": True})


class ProtonTests(unittest.TestCase):
    def test_invitation_retains_encryption_fragment(self):
        url = "https://meet.proton.me/join/id-ABC123#pwd-secret_123"
        self.assertEqual(proton.validate_url(url), url)
        self.assertNotIn("secret", proton.public_url(url))
        example_userinfo = "fixture"
        for invalid in (
            url.replace("https:", "http:"),
            url.split("#")[0],
            url.replace("meet.proton.me", "meet.proton.me.evil"),
            url.replace("id-ABC123", ""),
            url.replace("meet.proton.me", example_userinfo + "@meet.proton.me"),
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                proton.validate_url(invalid)

    def test_waits_for_admission_submits_once_and_mutes(self):
        pg = Page(admit_at=8)
        with patch.object(proton.time, "monotonic", side_effect=lambda: pg.clock):
            proton.prejoin(pg, "Live Transcript", timeout=12)
        self.assertEqual(pg.name, "Live Transcript")
        self.assertEqual(pg.clicks.count("Join meeting"), 1)
        self.assertEqual(pg.clicks[0], "Mute microphone")
        self.assertTrue(proton.joined(pg))

    def test_lobby_is_never_join_confirmation(self):
        pg = Page(admit_at=None)
        with patch.object(proton.time, "monotonic", side_effect=lambda: pg.clock):
            with self.assertRaises(TimeoutError):
                proton.prejoin(pg, "Live Transcript", timeout=3)
        self.assertFalse(proton.joined(pg))

    def test_deployed_join_in_browser_button(self):
        pg = Page(admit_at=2)
        pg.visibility["Join meeting"] = False
        pg.visibility["Join in browser"] = True
        with patch.object(proton.time, "monotonic", side_effect=lambda: pg.clock):
            proton.prejoin(pg, "Live Transcript", timeout=5)
        self.assertEqual(pg.clicks.count("Join in browser"), 1)
        self.assertNotIn("Join meeting", pg.clicks)

    def test_toolbar_and_leave_both_required(self):
        pg = Page()
        pg.visibility["Leave Meeting"] = True
        self.assertFalse(proton.joined(pg))
        pg.visibility[proton.TOOLBAR] = True
        self.assertTrue(proton.joined(pg))

    def test_rejected_admission_aborts(self):
        pg = Page()
        pg.dialogs = ["Entry denied by host"]
        with self.assertRaises(RuntimeError):
            proton.prejoin(pg, "Live Transcript")
        self.assertEqual(pg.clicks, [])

    def test_child_cleanup_terminates_owned_process(self):
        with tempfile.TemporaryDirectory() as d:
            children = proton.Children()
            proc = children.start(
                [sys.executable, "-c", "import time; time.sleep(60)"], Path(d) / "log"
            )
            children.close()
            self.assertIsNotNone(proc.poll())
            children.close()  # idempotent, including exception/finally cleanup

    def test_launcher_quotes_remote_arguments_and_redacts_passcodes(self):
        # Exercise the actual embedded launcher snippets without SSH or mutation
        # of the user's real meeting state.
        import re

        source = (
            Path(__file__).resolve().parents[1] / "src/live_transcript/runtime/launch.sh"
        ).read_text()
        remote_code = re.search(r"REMOTE_CMD=.*?<<'PY'\n(.*?)\nPY", source, re.S)[1]
        args = [
            "https://meet.proton.me/join/id-ABC#pwd-secret",
            "--name",
            "Live Transcript $(touch /tmp/must-not-exist) 'quoted'",
            "--max-min",
            "20",
        ]
        cmd = subprocess.check_output(
            [sys.executable, "-c", remote_code, *args],
            text=True,
            env=dict(os.environ, LT_VM_HOME="/home/transcript"),
        ).strip()
        self.assertEqual(shlex.split(cmd.split(" && exec ", 1)[1])[-len(args) :], args)
        safe_code = re.search(r"ARGS_SAFE=.*?<<'PY'\n(.*?)\nPY", source, re.S)[1]
        with tempfile.TemporaryDirectory() as d:
            for opt in (["--passcode", "two words"], ["--passcode=two words"]):
                safe = subprocess.check_output(
                    [sys.executable, "-c", safe_code, "--name", args[2], *opt],
                    text=True,
                    env=dict(os.environ, LT_LOGS=d),
                )
                self.assertNotIn("two words", safe)
                self.assertIn("REDACTED", safe)
                self.assertEqual((Path(d) / ".passcode").read_text(), "two words")
                self.assertEqual(os.stat(Path(d) / ".passcode").st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
