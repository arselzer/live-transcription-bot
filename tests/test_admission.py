import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import live_transcript.providers.admission as admission


class AdmissionTests(unittest.TestCase):
    def test_console_status_rejects_corrupt_types_and_invalid_deadlines(self):
        for raw in (
            [],
            None,
            dict(stage="unknown", utc=1),
            dict(stage="needs_operator", utc=1),
            dict(stage="needs_operator", utc=1, deadline=float("nan")),
            dict(stage="joining", utc=True),
            dict(stage="joining", utc=1, reason="untrusted content"),
        ):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "admission.json").write_text(json.dumps(raw))
                self.assertIsNone(admission.read_status(root))

    def test_console_status_exports_only_admission_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "admission.json").write_text(
                json.dumps(
                    dict(
                        stage="needs_operator",
                        utc=1,
                        deadline=5,
                        reason="google_join_refused",
                        cookie="fixture-private-value",
                        url="https://example.invalid/?token=fixture",
                    )
                )
            )
            self.assertEqual(
                admission.read_status(root),
                dict(stage="needs_operator", utc=1, deadline=5, reason="google_join_refused"),
            )

    def test_explicit_provider_blocks_are_precise(self):
        for text, expected in [
            ("Automated bots aren't allowed to join this meeting.", "zoom_automation_blocked"),
            ("Automated bots aren’t allowed to join this meeting.", "zoom_automation_blocked"),
            ("Please confirm you are not a robot", "captcha_required"),
            ("The host has denied your admission", "host_denied"),
            ("This meeting is locked", "meeting_locked"),
        ]:
            self.assertEqual(admission.block_reason(text), expected)
        self.assertIsNone(admission.block_reason("Protected by reCAPTCHA"))

    def test_joined_chat_is_not_an_admission_denial(self):
        page = MagicMock()
        self.assertIsNone(admission.inspect_zoom(page, joined=True))
        page.frames.__iter__.assert_not_called()

    def test_host_and_captcha_denials_take_precedence_over_bot_text(self):
        self.assertEqual(
            admission.block_reason("Host denied entry. Automated bots aren't allowed."),
            "host_denied",
        )
        self.assertEqual(
            admission.block_reason("Confirm you are not a robot. Automated bots aren't allowed."),
            "captcha_required",
        )

    def test_untrusted_frame_cannot_create_provider_denial(self):
        frame = MagicMock(url="https://evil.invalid/")
        page = MagicMock(frames=[frame])
        self.assertIsNone(admission.inspect_zoom(page))
        frame.locator.assert_not_called()

    def test_scoped_failure_has_no_invitation_secret_or_invented_cause(self):
        with tempfile.TemporaryDirectory() as d:
            page = MagicMock(url="https://zoom.us/wc/123/join?pwd=secret#secret")
            with patch("builtins.print"):
                admission.save_failure(page, d, "zoom_automation_blocked", "Live Transcript")
            text = (Path(d) / "failure.json").read_text()
            data = json.loads(text)
            self.assertNotIn("secret", text)
            self.assertEqual(data["cause"], "not established")
            self.assertFalse(data["automatic_retry"])

    def test_full_disk_does_not_remove_terminal_reason(self):
        with tempfile.TemporaryDirectory() as d:
            page = MagicMock(url="https://zoom.us/wc/123/join")
            with (
                patch.object(admission, "atomic_json", side_effect=OSError("disk full")),
                patch("builtins.print"),
            ):
                data = admission.save_failure(page, d, "zoom_automation_blocked", "Live Transcript")
            self.assertEqual(data["reason"], "zoom_automation_blocked")
            self.assertEqual(data["diagnostic_storage_error"], "OSError")

    def test_renaming_does_not_bypass_test_profile_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            admission.claim_test_join("tts-9231", d, 1000)
            with self.assertRaises(RuntimeError):
                admission.claim_test_join("tts-9231", d, 1001)
            admission.claim_test_join("tts-9232", d, 1001)
            admission.claim_test_join("tts-9231", d, 1200)

    def test_rolling_test_budget_spans_profiles_and_eventually_expires(self):
        with tempfile.TemporaryDirectory() as d:
            for index in range(6):
                admission.claim_test_join(str(index), d, 1000 + index)
            with self.assertRaises(RuntimeError):
                admission.claim_test_join("another", d, 1100)
            admission.claim_test_join("another", d, 2000)

    def test_corrupt_budget_is_visible_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "test-join-budget.json").write_text("broken")
            with self.assertRaises(ValueError):
                admission.claim_test_join("tts", d, 1000)
