"""Google Meet URL admission gates (pure logic; no browser)."""

import unittest
import tempfile
from pathlib import Path
import signal
from unittest.mock import MagicMock, patch
from live_transcript.providers import google_meet
from live_transcript.core.options import provider_origin

from live_transcript.providers.google_meet import is_google_url, validate_url, public_url


class GoogleMeetTests(unittest.TestCase):
    def test_valid_meeting_code_passes_unchanged(self):
        url = "https://meet.google.com/abc-abcd-efg"
        self.assertTrue(is_google_url(url))
        self.assertEqual(validate_url(url), url)
        self.assertEqual(validate_url(url + "/"), url + "/")

    def test_rejects_wrong_host_shape_and_case(self):
        for bad in (
            "http://meet.google.com/abc-abcd-efg",
            "https://evil.example/abc-abcd-efg",
            "https://meet.google.com/abcd-abcd-efg",
            "https://meet.google.com/ABC-ABCD-EFG",
            "https://meet.google.com/",
            "https://meet.google.com/abc-abcd-efg/evil",
        ):
            with self.assertRaises(ValueError, msg=bad):
                validate_url(bad)

    def test_proton_urls_are_not_google(self):
        self.assertFalse(is_google_url("https://meet.proton.me/join/id-abc#pwd-x"))

    def test_public_url_drops_query_and_fragment(self):
        self.assertEqual(
            public_url("https://meet.google.com/abc-abcd-efg?hl=en#t=1"),
            "https://meet.google.com/abc-abcd-efg",
        )

    def test_privacy_footer_does_not_prevent_signed_in_join(self):
        page = MagicMock()
        page.get_by_role.return_value.all_text_contents.return_value = []
        page.get_by_role.return_value.first.is_visible.return_value = False
        page.evaluate.return_value = (
            "Ready to join? No one can join a meeting unless invited or admitted by the host"
        )
        with (
            patch.object(google_meet, "joined", side_effect=[False, True]),
            patch.object(
                google_meet, "click_if_visible", side_effect=lambda pg, label: label == "Join now"
            ) as click,
        ):
            google_meet.prejoin(page, "Recorder")
        click.assert_any_call(page, "Join now")
        page.get_by_role.return_value.first.fill.assert_not_called()

    def test_waiting_room_request_is_submitted_once(self):
        page = MagicMock()
        page.get_by_role.return_value.all_text_contents.return_value = []
        page.evaluate.return_value = "Ready to join"
        with (
            patch.object(google_meet, "joined", side_effect=[False, False, True]),
            patch.object(
                google_meet,
                "click_if_visible",
                side_effect=lambda pg, label: label == "Ask to join",
            ) as click,
        ):
            google_meet.prejoin(page, "Recorder")
        self.assertEqual(sum(call.args[1] == "Ask to join" for call in click.call_args_list), 1)
        page.get_by_role.return_value.first.fill.assert_called_once_with("Recorder")

    def test_name_validation_is_not_reset_while_join_is_disabled(self):
        page = MagicMock()
        page.get_by_role.return_value.all_text_contents.return_value = []
        page.get_by_role.return_value.first.input_value.return_value = "Recorder"
        page.evaluate.return_value = "Ready to join"
        with (
            patch.object(google_meet, "joined", side_effect=[False, False, True]),
            patch.object(google_meet, "click_if_visible", return_value=False),
        ):
            google_meet.prejoin(page, "Recorder")
        page.get_by_role.return_value.first.fill.assert_called_once_with("Recorder")
        page.get_by_role.return_value.first.press.assert_called_once_with("Tab")

    def test_explicit_google_denial_has_a_terminal_reason(self):
        for text, reason in (
            ("You can't join this video call", "google_access_denied"),
            ("Sign in to join this meeting", "google_sign_in_required"),
        ):
            page = MagicMock()
            page.get_by_role.return_value.all_text_contents.return_value = []
            page.evaluate.return_value = text
            with patch.object(google_meet, "joined", return_value=False):
                with self.assertRaises(google_meet.AdmissionRefused) as result:
                    google_meet.prejoin(page, "Recorder")
            self.assertEqual(result.exception.reason, reason)

    def test_joined_chat_is_never_an_admission_denial(self):
        page = MagicMock()
        with patch.object(google_meet, "joined", return_value=True):
            google_meet.prejoin(page, "Recorder")
        page.evaluate.assert_not_called()

    def test_admission_timeout_is_explicit_and_not_a_rejoin_loop(self):
        with self.assertRaises(google_meet.AdmissionRefused) as result:
            google_meet.prejoin(MagicMock(), "Recorder", timeout=0)
        self.assertEqual(result.exception.reason, "google_admission_timeout")

    def test_google_controls_allow_shortcut_suffixes_but_not_unrelated_text(self):
        page = MagicMock()
        google_meet.button(page, "Leave call")
        pattern = page.get_by_role.call_args.kwargs["name"]
        self.assertIsNotNone(pattern.fullmatch("Leave call (Ctrl + Alt + H)"))
        self.assertIsNone(pattern.search("Do not Leave call"))

    def test_google_microphone_and_speaker_observer_use_google_controls(self):
        from live_transcript.speech.controller import VoiceController
        from live_transcript.capture.observer import speaker_script

        voice = VoiceController.__new__(VoiceController)
        voice.page, voice.provider = MagicMock(), "google"
        for unmute, label in (
            (True, "Turn on microphone (Ctrl + d)"),
            (False, "Turn off microphone"),
        ):
            voice.mic_button(unmute)
            self.assertIsNotNone(voice.page.get_by_role.call_args.kwargs["name"].fullmatch(label))
        self.assertEqual(speaker_script("google"), google_meet.SPEAKERS_JS)

    def test_google_recovery_accepts_only_trusted_origin_and_safe_media_actions(self):
        from live_transcript.providers.rescue import validate_candidates

        self.assertTrue(provider_origin("google", "https://meet.google.com/abc-abcd-efg"))
        for url in (
            "https://meet.google.com.evil.invalid",
            "https://accounts.google.com",
            "http://meet.google.com",
            "https://fixture@example.invalid",
        ):
            self.assertFalse(provider_origin("google", url), url)
        data = dict(
            provider="google",
            controls=[dict(id=0, label="Turn on camera", dialog=False, action="enable_camera")],
        )
        self.assertEqual(validate_candidates(data)[0]["action"], "enable_camera")
        data["controls"][0].update(label="Turn on microphone", action="enable_camera")
        with self.assertRaises(ValueError):
            validate_candidates(data)


class OwnedBrowserTests(unittest.TestCase):
    def test_owned_private_browser_is_reaped_when_the_meeting_fails(self):
        from live_transcript.providers.browser import google_context

        with tempfile.TemporaryDirectory() as directory:
            settings = {
                "browser": {"google_mode": "cdp", "google_executable": "", "google_private": True},
                "vm": {"home": directory},
            }
            playwright = MagicMock()
            playwright.chromium.executable_path = "/example/chrome"
            process = MagicMock(pid=123456)
            process.poll.return_value = None
            expected = playwright.chromium.connect_over_cdp.return_value.new_context.return_value
            with (
                patch(
                    "live_transcript.providers.browser.get",
                    side_effect=lambda section, key: settings[section][key],
                ),
                patch(
                    "live_transcript.providers.browser.socket.create_connection",
                    side_effect=OSError,
                ),
                patch("live_transcript.providers.browser.urllib.request.urlopen"),
                patch(
                    "live_transcript.providers.browser.subprocess.Popen", return_value=process
                ) as start,
                patch("live_transcript.providers.browser.os.killpg") as kill,
            ):
                with self.assertRaisesRegex(RuntimeError, "meeting failed"):
                    with google_context(
                        playwright, Path(directory) / "profile", env={}, args=[], port=9230
                    ) as context:
                        self.assertIs(context, expected)
                        raise RuntimeError("meeting failed")
            command = start.call_args.args[0]
            self.assertIn("--remote-debugging-port=9230", command)
            self.assertNotIn("--enable-automation", command)
            self.assertTrue(start.call_args.kwargs["start_new_session"])
            kill.assert_called_once_with(process.pid, signal.SIGTERM)
            process.wait.assert_called_once_with(timeout=10)

    def test_existing_control_port_is_never_attached_to_or_killed(self):
        from live_transcript.providers.browser import google_context

        with (
            patch(
                "live_transcript.providers.browser.get",
                side_effect=lambda section, key: "cdp" if key == "google_mode" else "",
            ),
            patch("live_transcript.providers.browser.socket.create_connection"),
            patch("live_transcript.providers.browser.subprocess.Popen") as start,
        ):
            with self.assertRaisesRegex(RuntimeError, "already owned"):
                with google_context(MagicMock(), Path("/example/profile"), env={}, args=[]):
                    pass
            start.assert_not_called()

    def test_invalid_control_port_fails_before_browser_start(self):
        from live_transcript.providers.browser import google_context

        with patch("live_transcript.providers.browser.subprocess.Popen") as start:
            with self.assertRaises(ValueError):
                with google_context(MagicMock(), Path("/example/profile"), env={}, args=[], port=0):
                    pass
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
