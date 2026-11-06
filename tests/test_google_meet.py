"""Google Meet URL admission gates (pure logic; no browser)."""

import unittest
import tempfile
from pathlib import Path
import signal
from unittest.mock import MagicMock, patch
from live_transcript.providers import google_meet
from live_transcript.providers import google_admission as google_ui
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
        page.evaluate.return_value = dict(
            controls=["Join now"],
            body="Ready to join? No one can join a meeting unless invited or admitted by the host",
        )
        with (
            patch.object(google_ui, "joined", side_effect=[False, True]),
            patch.object(
                google_ui, "click_if_visible", side_effect=lambda pg, label: label == "Join now"
            ) as click,
        ):
            google_meet.prejoin(page, "Recorder")
        click.assert_any_call(page, "Join now")
        page.get_by_role.return_value.first.fill.assert_not_called()

    def test_waiting_room_request_is_submitted_once(self):
        page = MagicMock()
        page.get_by_role.return_value.all_text_contents.return_value = []
        page.evaluate.return_value = dict(controls=["Join now"], body="Ready to join")
        with (
            patch.object(google_ui, "joined", side_effect=[False, False, True]),
            patch.object(
                google_ui,
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
        page.evaluate.return_value = dict(controls=["Join now"], body="Ready to join")
        with (
            patch.object(google_ui, "joined", side_effect=[False, False, True]),
            patch.object(google_ui, "click_if_visible", return_value=False),
        ):
            google_meet.prejoin(page, "Recorder")
        page.get_by_role.return_value.first.fill.assert_called_once_with("Recorder")
        page.get_by_role.return_value.first.press.assert_called_once_with("Tab")

    def test_replaced_target_is_reobserved_without_dom_fallback_or_double_name_entry(self):
        from live_transcript.providers.interaction import NativeTargetChanged

        page = MagicMock(url="https://meet.google.com/abc-abcd-efg")
        page.evaluate.return_value = dict(controls=["Join now"], name_field=True)
        interaction = MagicMock()
        interaction.click.side_effect = [
            NativeTargetChanged("detached before click"),
            None,
            None,
            None,
        ]
        with patch.object(google_ui, "joined", side_effect=[False, False, True]):
            google_ui.prejoin(page, "Recorder", interaction=interaction)
        interaction.fill_name.assert_called_once()
        page.get_by_role.return_value.first.fill.assert_not_called()

    def test_rpc_refusal_during_native_verification_is_not_reported_as_input_failure(self):
        from live_transcript.providers.interaction import NativeInputError

        page = MagicMock(url="https://meet.google.com/abc-abcd-efg")
        page.evaluate.side_effect = [
            dict(controls=["Join now"], name_field=True),
            dict(body="You can't join this video call"),
        ]
        page._lt_google_refusal = None
        interaction = MagicMock()

        def refused(_):
            page._lt_google_refusal = dict(status=403, method="ResolveMeetingSpace")
            raise NativeInputError("control disappeared")

        interaction.click.side_effect = refused
        with patch.object(google_ui, "joined", return_value=False):
            with self.assertRaises(google_ui.AdmissionRefused) as result:
                google_ui.prejoin(page, "Recorder", interaction=interaction)
        self.assertEqual(result.exception.reason, "google_join_refused")

    def test_explicit_google_denial_has_a_terminal_reason(self):
        for text, reason in (
            ("You can't join this video call", "google_access_denied"),
            ("Sign in to join this meeting", "google_sign_in_required"),
        ):
            page = MagicMock()
            page.get_by_role.return_value.all_text_contents.return_value = []
            page.evaluate.return_value = dict(body=text)
            with patch.object(google_ui, "joined", return_value=False):
                with self.assertRaises(google_meet.AdmissionRefused) as result:
                    google_meet.prejoin(page, "Recorder")
            self.assertEqual(result.exception.reason, reason)

    def test_joined_chat_is_never_an_admission_denial(self):
        page = MagicMock()
        with patch.object(google_ui, "joined", return_value=True):
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


class GoogleStateTests(unittest.TestCase):
    def test_foreign_toolbar_cannot_prove_admission(self):
        page = MagicMock(url="https://example.invalid/")
        page.evaluate.return_value = dict(controls=["Leave call", "Present now"])
        self.assertFalse(google_ui.joined(page))

    def test_leave_button_in_lobby_does_not_prove_admission(self):
        for data in (
            dict(controls=["Leave call", "Turn off microphone"]),
            dict(controls=["Leave call", "Present now", "Join now"]),
            dict(
                controls=["Leave call", "Present now"],
                statuses=["Waiting for the host to let you in"],
            ),
        ):
            self.assertNotEqual(google_ui.classify(data).stage, "admitted")

    def test_actual_in_call_controls_ignore_chat_denials_and_old_rpc_errors(self):
        result = google_ui.classify(
            dict(
                controls=["Leave call (Ctrl+h)", "Present now"],
                body="Someone said: You can't join this video call",
            ),
            refusal=dict(status=403, method="CreateMeetingDevice"),
        )
        self.assertEqual(result.stage, "admitted")

    def test_host_denial_wins_over_stale_waiting_status(self):
        result = google_ui.classify(
            dict(statuses=["Asking to be let in"], body="The host denied your request")
        )
        self.assertEqual(result.reason, "host_denied")

    def test_unanswered_request_is_not_host_rejection(self):
        result = google_ui.classify(
            dict(body="No one responded to your request to join", statuses=["Asking to be let in"])
        )
        self.assertEqual(result.reason, "google_admission_timeout")

    def test_recording_notice_requires_entering_the_call_and_unknown_consent_needs_operator(self):
        data = dict(
            controls=["Leave call", "Present now"],
            notices=[dict(text="Gemini is taking notes", controls=["Join now", "Leave"])],
        )
        self.assertEqual(google_ui.classify(data).stage, "join_notice")
        data["notices"] = [dict(text="Agree to start taking notes for me", controls=["Start"])]
        self.assertEqual(google_ui.classify(data).reason, "google_consent_required")

    def test_changed_recording_notice_is_not_silently_treated_as_admission(self):
        state = google_ui.classify(
            dict(
                controls=["Leave call", "Present now"],
                notices=[dict(text="Gemini is taking notes", controls=["Continue"])],
            )
        )
        self.assertEqual(state.reason, "google_consent_required")

    def test_signed_out_profile_cannot_silently_join_as_guest(self):
        data = dict(name_field=True, controls=["Join now"])
        self.assertEqual(
            google_ui.classify(data, require_sign_in=True).reason, "google_auth_session_missing"
        )
        self.assertEqual(google_ui.classify(data).stage, "prejoin")

    def test_sign_in_redirect_and_foreign_navigation_cannot_receive_native_input(self):
        self.assertEqual(
            google_ui.classify({}, url="https://accounts.google.com/signin").reason,
            "google_auth_session_missing",
        )
        self.assertEqual(
            google_ui.classify({}, url="https://example.invalid/").reason,
            "google_navigation_unexpected",
        )

    def test_account_switch_never_displaces_an_existing_participant(self):
        self.assertEqual(
            google_ui.classify(dict(controls=["Switch here"])).reason, "google_account_in_use"
        )

    def test_visible_challenge_needs_operator_without_a_solver(self):
        self.assertEqual(google_ui.classify(dict(challenge=True)).reason, "captcha_required")

    def test_rpc_refusal_is_scoped_and_does_not_retain_credentials(self):
        page = MagicMock()
        google_ui.watch_refusals(page)
        callback = page.on.call_args.args[1]
        for url in (
            "https://example.invalid/$rpc/google.rtc.meetings.v1.MeetingDeviceService/CreateMeetingDevice",
            "https://meet.google.com/images/ResolveMeetingSpace",
        ):
            callback(MagicMock(url=url, status=403))
        self.assertIsNone(page._lt_google_refusal)
        callback(
            MagicMock(
                url="https://meet.google.com/$rpc/google.rtc.meetings.v1.MeetingDeviceService/CreateMeetingDevice?token=fixture-secret",
                status=403,
            )
        )
        self.assertEqual(page._lt_google_refusal, dict(status=403, method="CreateMeetingDevice"))
        self.assertNotIn("fixture-secret", str(page._lt_google_refusal))
        self.assertEqual(
            google_ui.classify({}, refusal=page._lt_google_refusal).reason, "google_join_refused"
        )

    def test_ui_locale_pin_keeps_other_invitation_options(self):
        result = google_ui.navigation_url("https://meet.google.com/abc-abcd-efg?authuser=1&hl=de")
        self.assertIn("authuser=1", result)
        self.assertIn("hl=en", result)
        self.assertNotIn("hl=de", result)

    def test_admission_evidence_is_private_and_does_not_store_body_or_invitation(self):
        import json

        page = MagicMock()
        page._lt_google_refusal = dict(status=403, method="ResolveMeetingSpace")
        with tempfile.TemporaryDirectory() as directory:
            google_ui.checkpoint(
                page,
                directory,
                google_ui.AdmissionState("rejected", "google_join_refused"),
                dict(
                    body="fixture-private-body",
                    url="fixture-private-url",
                    controls=["Return to home screen"],
                ),
            )
            data = (Path(directory) / "state.json").read_text()
            self.assertNotIn("fixture-private", data)
            self.assertEqual(json.loads(data)["rpc"]["method"], "ResolveMeetingSpace")
            self.assertEqual((Path(directory) / "transitions.jsonl").stat().st_mode & 0o777, 0o600)


class NativeInputTests(unittest.TestCase):
    def test_coordinate_translation_uses_observed_offset_and_display_scale(self):
        from live_transcript.providers.interaction import NativeInput

        self.assertEqual(NativeInput.root_point((500, 600), (200, 250), 2, (300, 350)), (700, 800))

    def test_invalid_coordinates_are_rejected(self):
        from live_transcript.providers.interaction import NativeInput, NativeInputError

        with self.assertRaises(NativeInputError):
            NativeInput.root_point((0, 0), (0, 0), float("nan"), (5, 5))

    def test_foreign_page_gets_no_desktop_events(self):
        from live_transcript.providers.interaction import NativeInput, NativeInputError

        interaction = NativeInput.__new__(NativeInput)
        interaction.page = MagicMock(url="https://example.invalid")
        interaction.run = MagicMock()
        with self.assertRaises(NativeInputError):
            interaction.move_to(MagicMock())
        interaction.run.assert_not_called()

    def test_unverified_target_never_receives_click(self):
        from live_transcript.providers.interaction import NativeInput, NativeInputError

        interaction = NativeInput.__new__(NativeInput)
        interaction.move_to = MagicMock(side_effect=NativeInputError("covered control"))
        interaction.run = MagicMock()
        with self.assertRaises(NativeInputError):
            interaction.click(MagicMock())
        interaction.run.assert_not_called()

    def test_name_requires_verified_keyboard_focus_before_typing(self):
        from live_transcript.providers.interaction import NativeInput, NativeInputError

        interaction = NativeInput.__new__(NativeInput)
        interaction.click = MagicMock()
        interaction.run = MagicMock()
        control = MagicMock()
        control.evaluate.return_value = False
        with self.assertRaises(NativeInputError):
            interaction.fill_name(control, "Recorder")
        interaction.run.assert_not_called()

    def test_focus_change_rechecks_target_before_click(self):
        from live_transcript.providers.interaction import NativeInput, NativeInputError

        interaction = NativeInput.__new__(NativeInput)
        interaction.move_to = MagicMock(return_value=dict(x=10, y=20))
        interaction.trusted_page = MagicMock()
        interaction.run = MagicMock(return_value="WINDOW=1234")
        control = MagicMock()
        control.evaluate.return_value = False
        with self.assertRaises(NativeInputError):
            interaction.click(control)
        self.assertFalse(any(call.args[0] == "click" for call in interaction.run.call_args_list))

    def test_unicode_clipboard_owner_is_reaped_on_failure(self):
        import io
        from live_transcript.providers.interaction import NativeInput, NativeInputError

        interaction = NativeInput.__new__(NativeInput)
        interaction.clipboard_tool = "xclip"
        interaction.display = ":fixture"
        interaction.page = MagicMock()
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        with (
            patch("live_transcript.providers.interaction.subprocess.Popen", return_value=process),
            patch(
                "live_transcript.providers.interaction.subprocess.run",
                return_value=MagicMock(stdout="Recorder Ä"),
            ),
        ):
            with self.assertRaises(NativeInputError):
                with interaction.clipboard("Recorder Ä"):
                    raise NativeInputError("fixture interruption")
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(process.stdin.closed)

    def test_unknown_clipboard_data_is_never_pasted(self):
        import io
        from live_transcript.providers.interaction import NativeInput, NativeInputError

        interaction = NativeInput.__new__(NativeInput)
        interaction.clipboard_tool = "xclip"
        interaction.display = ":fixture"
        interaction.page = MagicMock()
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        entered = False
        with (
            patch("live_transcript.providers.interaction.subprocess.Popen", return_value=process),
            patch(
                "live_transcript.providers.interaction.subprocess.run",
                return_value=MagicMock(stdout="unrelated"),
            ),
            patch("live_transcript.providers.interaction.time.monotonic", side_effect=[0, 0, 3]),
        ):
            with self.assertRaises(NativeInputError):
                with interaction.clipboard("Recorder Ä"):
                    entered = True
        self.assertFalse(entered)
        process.terminate.assert_called_once()

    def test_control_characters_cannot_become_desktop_input(self):
        from live_transcript.providers.interaction import NativeInput, NativeInputError

        interaction = NativeInput.__new__(NativeInput)
        interaction.click = MagicMock()
        with self.assertRaises(NativeInputError):
            interaction.fill_name(MagicMock(), "Recorder\ncommand")
        interaction.click.assert_not_called()


class OwnedBrowserTests(unittest.TestCase):
    def test_playwright_private_mode_never_loads_the_signed_in_profile(self):
        from live_transcript.providers.browser import google_context

        for private in (True, False):
            with self.subTest(private=private):
                playwright = MagicMock()
                settings = dict(
                    google_mode="playwright", google_executable="", google_private=private
                )
                with patch(
                    "live_transcript.providers.browser.get",
                    side_effect=lambda section, key: settings[key],
                ):
                    with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                        with google_context(playwright, Path("profile"), env={}, args=[]):
                            raise RuntimeError("fixture failure")
                if private:
                    playwright.chromium.launch_persistent_context.assert_not_called()
                    browser = playwright.chromium.launch.return_value
                    browser.new_context.return_value.close.assert_called_once()
                    browser.close.assert_called_once()
                else:
                    playwright.chromium.launch.assert_not_called()
                    playwright.chromium.launch_persistent_context.return_value.close.assert_called_once()

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

    def test_profile_readiness_never_treats_an_anonymous_cookie_as_login(self):
        from live_transcript.providers.browser import google_profile_ready

        context = MagicMock()
        context.cookies.return_value = [dict(name="NID", value="anonymous")]
        self.assertFalse(google_profile_ready(context))
        context.cookies.return_value = [dict(name="SID", value="")]
        self.assertFalse(google_profile_ready(context))
        context.cookies.return_value = [dict(name="__Secure-1PSID", value="private-fixture-value")]
        self.assertTrue(google_profile_ready(context))

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
