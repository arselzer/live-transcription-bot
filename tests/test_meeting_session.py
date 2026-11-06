import json
from pathlib import Path
import tempfile
import unittest
import fcntl
import hashlib
from unittest.mock import patch

import live_transcript.runtime.session as session


class SessionTests(unittest.TestCase):
    def test_vm_lease_blocks_audio_preparation_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "logs").mkdir()
            with open(root / "logs/meeting-owner.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (
                    patch.object(session, "HOME", root),
                    patch.object(session.subprocess, "run") as run,
                ):
                    with self.assertRaises(RuntimeError):
                        session.assert_idle()
                    run.assert_not_called()

    def test_reconnect_rejects_another_invocation_using_same_token(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "session-test"
            prefix.with_suffix(".started.json").write_text(
                json.dumps({"identity": hashlib.sha256(b"other").hexdigest()})
            )
            with (
                patch.object(session, "paths", return_value=prefix),
                patch.object(session.subprocess, "run") as run,
            ):
                with self.assertRaises(RuntimeError):
                    session.follow("test", ["different invitation"])
                run.assert_not_called()

    def test_worker_failure_always_leaves_completion_record(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "session-test"
            with (
                patch.object(session, "paths", return_value=prefix),
                patch.dict("os.environ", {"LT_TEST_ROLE": "fixture-session"}),
                patch.object(
                    session.subprocess, "Popen", side_effect=RuntimeError("browser failed")
                ),
            ):
                with self.assertRaises(RuntimeError):
                    session.worker("test", [])
            self.assertEqual(json.loads(prefix.with_suffix(".done.json").read_text())["code"], 1)

    def test_reconnecting_log_follower_never_launches_second_meeting(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d) / "session-test"
            prefix.with_suffix(".started.json").write_text("{}")
            prefix.with_suffix(".log").write_text("RECORDING to session.wav\nDONE session.wav\n")
            prefix.with_suffix(".done.json").write_text('{"code":0}')
            with (
                patch.object(session, "paths", return_value=prefix),
                patch.object(session.subprocess, "run") as run,
                patch("builtins.print"),
            ):
                self.assertEqual(session.follow("test", ["private-invitation"]), 0)
            run.assert_not_called()

    def test_browser_cleanup_matches_each_real_provider_profile(self):
        from live_transcript.runtime import prepare

        for profile in ("chrome-prof", "chrome-proton-prof", "chrome-meet-prof"):
            args = [
                b"/usr/bin/chromium",
                ("--user-data-dir=" + str(prepare.HOME / profile)).encode(),
            ]
            self.assertTrue(prepare.owned_process(args), profile)
        self.assertFalse(prepare.owned_process([b"chromium", b"--user-data-dir=/tmp/unrelated"]))

    def test_session_token_rejects_shell_and_path_characters(self):
        for token in ("../x", "x;command", "x/y", "", "x" * 81):
            with self.subTest(token=token), self.assertRaises(ValueError):
                session.paths(token)


if __name__ == "__main__":
    unittest.main()
