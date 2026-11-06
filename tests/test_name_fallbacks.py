import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import live_transcript.providers.zoom as zoom
from live_transcript.providers.admission import AdmissionFailure
import live_transcript.core.config as runtime_config


class NameFallbackTests(unittest.TestCase):
    def run_sequence(self, outcomes, argv=None):
        names = []
        deadlines = []
        calls = []

        def main(**kw):
            names.append(zoom.sys.argv[-1])
            deadlines.append(kw["absolute_deadline"])
            calls.append(list(zoom.sys.argv))
            outcome = outcomes[len(names) - 1]
            if outcome:
                raise AdmissionFailure(outcome)
            return "joined"

        args = argv or [
            "live-transcript join",
            "https://zoom.us/j/12345678901?pwd=secret",
            "--name=Live Transcript",
            "--passcode",
            "a secret with spaces",
            "--max-min",
            "30",
        ]
        original = runtime_config.get

        def setting(section, key):
            if (section, key) == ("admission", "zoom_name_fallbacks"):
                return ["Example User (Transcript)", "Example User"]
            return original(section, key)

        with (
            patch.object(zoom.sys, "argv", args),
            patch.object(zoom, "main", side_effect=main),
            patch.object(zoom, "get", side_effect=setting),
            patch.object(zoom.time, "sleep"),
            patch.dict(os.environ, {"LT_DEADLINE": str(zoom.time.time() + 1800)}),
        ):
            result = zoom.run_with_name_fallbacks()
            self.assertEqual(zoom.sys.argv, args)
        return result, names, deadlines, calls

    def test_exact_authorized_names_and_secrets_preserved(self):
        result, names, deadlines, calls = self.run_sequence(
            ["zoom_automation_blocked", "zoom_automation_blocked", None]
        )
        self.assertEqual(result, "joined")
        self.assertEqual(names, ["Live Transcript", "Example User (Transcript)", "Example User"])
        self.assertEqual(len(set(deadlines)), 1)
        for call in calls:
            self.assertIn("a secret with spaces", call)
            self.assertIn("https://zoom.us/j/12345678901?pwd=secret", call)

    def test_successful_first_name_does_not_rename_or_rejoin(self):
        _, names, _, _ = self.run_sequence([None])
        self.assertEqual(names, ["Live Transcript"])

    def test_host_denial_lock_and_captcha_never_trigger_fallback(self):
        for reason in ("host_denied", "meeting_locked", "captcha_required"):
            with self.subTest(reason=reason), self.assertRaises(AdmissionFailure) as result:
                self.run_sequence([reason])
            self.assertEqual(result.exception.reason, reason)

    def test_last_name_failure_is_terminal(self):
        with self.assertRaises(AdmissionFailure):
            self.run_sequence(["zoom_automation_blocked"] * 3)

    def test_recovery_starts_at_last_successful_name(self):
        _, names, _, _ = self.run_sequence(
            ["zoom_automation_blocked", None],
            [
                "live-transcript join",
                "https://zoom.us/j/12345678901",
                "--name",
                "Example User (Transcript)",
                "--max-min",
                "30",
            ],
        )
        self.assertEqual(names, ["Example User (Transcript)", "Example User"])

    def test_plain_systemexit_is_not_treated_as_an_automation_block(self):
        with (
            patch.object(zoom.sys, "argv", ["live-transcript join", "12345678901"]),
            patch.object(zoom, "main", side_effect=SystemExit(4)) as main,
        ):
            with self.assertRaises(SystemExit):
                zoom.run_with_name_fallbacks()
            self.assertEqual(main.call_count, 1)

    def test_option_like_secret_is_never_rewritten(self):
        source = ["https://zoom.us/j/12345678901", "--passcode", "--name", "--name", "Original"]
        result = zoom.with_option(source, "--name", "Example User")
        self.assertEqual(
            result,
            ["https://zoom.us/j/12345678901", "--passcode", "--name", "--name", "Example User"],
        )

    def test_budget_expiry_does_not_start_another_browser(self):
        with (
            patch.object(zoom.sys, "argv", ["live-transcript join", "12345678901"]),
            patch.dict(os.environ, {"LT_DEADLINE": "1"}),
            patch.object(zoom, "main") as main,
        ):
            with self.assertRaises(AdmissionFailure):
                zoom.run_with_name_fallbacks()
            main.assert_not_called()

    def test_config_limits_fallback_count_and_validates_names(self):
        for value in ('["one","two","three"]', '["\\n"]', "[12]"):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "config.toml"
                p.write_text("[admission]\nzoom_name_fallbacks=" + value)
                with self.assertRaises(ValueError):
                    runtime_config.load(p)


if __name__ == "__main__":
    unittest.main()
