"""Offline regression checks for the Jitsi provider adapter."""

import unittest
from unittest.mock import MagicMock, patch

import live_transcript.providers.jitsi as jitsi


def fake_get(section, key):
    if (section, key) == ("providers", "jitsi_hosts"):
        return "192.0.2.10 meet.example.org example.org"
    return ""


class UrlTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(jitsi, "get", fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_and_configured_hosts_validate(self):
        for url in (
            "https://meet.jit.si/LT-Room-42",
            "https://sub.example.jit.si/Room_1",
            "https://192.0.2.10:8443/LT-Room",
            "https://meet.example.org/Room.config",
        ):
            self.assertTrue(jitsi.is_jitsi_url(url), url)
            self.assertEqual(jitsi.validate_url(url), url.split("?")[0])

    def test_foreign_and_malformed_urls_rejected(self):
        for url in (
            "http://192.0.2.10:8443/LT-Room",  # insecure origin cannot capture
            "https://evil.example.com/meet.jit.si",
            "https://jit.si.evil.example/Room",
            "https://192.0.2.10:8443/",
            "https://192.0.2.10:8443/a",  # room names need >= 2 characters
            "https://192.0.2.10:8443/Room/../etc",
            "https://meet.jit.si/Room room",
            "https://user:secret@example.org/Room",
            "https://meet.jit.si:bad/Room",
            "https://meet.jit.si:70000/Room",
            "https://meet.jit.si:0/Room",
            "https://meet.jit.si/..",
            "https://meet.jit.si/Room\n",
        ):
            with self.assertRaises(ValueError, msg=url):
                jitsi.validate_url(url)

    def test_query_parameters_stripped_public_url(self):
        url = "https://meet.jit.si/LT-Room?config.prejoinPageEnabled=false"
        self.assertEqual(jitsi.public_url(jitsi.validate_url(url)), "https://meet.jit.si/LT-Room")

    def test_join_mutes_audio_and_video_and_preserves_private_access_query(self):
        target = jitsi.navigation_url(
            "https://meet.example.org/Room?jwt=fixture#config.startWithAudioMuted=false"
        )
        self.assertIn("?jwt=fixture#", target)
        self.assertIn("config.startWithAudioMuted=true", target)
        self.assertIn("config.startWithVideoMuted=true", target)
        self.assertNotIn("=false", target)
        self.assertNotIn("fixture", jitsi.public_url(target))


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
        if self.key == '[data-testid="prejoin.joinMeeting"]':
            self.page.visibility[self.key] = False


class Page:
    JOIN = '[data-testid="prejoin.joinMeeting"]'
    LEAVE = '[aria-label="Leave the meeting"]'

    def __init__(self, admit_at=3):
        self.clock = 0
        self.admit_at = admit_at
        self.visibility = {
            'input[placeholder*="name" i]': True,
            "Mute microphone": True,
            self.JOIN: True,
        }
        self.dialogs = []
        self.clicks = []

    def get_by_role(self, role, name=None, **kwargs):
        return Locator(self, name or role)

    def locator(self, selector):
        return Locator(self, selector)

    def wait_for_timeout(self, ms):
        self.clock += ms / 1000
        if self.admit_at is not None and self.clock >= self.admit_at:
            self.visibility.update({self.LEAVE: True})


class PrejoinTests(unittest.TestCase):
    def test_conference_state_handles_hidden_toolbars_and_leaving(self):
        page = MagicMock()
        page.evaluate.return_value = True
        self.assertTrue(jitsi.joined(page))
        page.locator.assert_not_called()
        page.evaluate.return_value = False
        self.assertFalse(jitsi.joined(page))
        page.locator.assert_not_called()

    def test_dom_fallback_cannot_confuse_prejoin_for_admission(self):
        page = Page()
        page.visibility[Page.LEAVE] = True
        self.assertFalse(jitsi.joined(page))

    def test_prejoin_names_mutes_and_joins(self):
        with patch.object(jitsi, "get", fake_get):
            page = Page()
            jitsi.prejoin(page, "Live Transcript", timeout=30)
            self.assertEqual(page.name, "Live Transcript")
            self.assertIn("Mute microphone", page.clicks)
            self.assertIn(Page.JOIN, page.clicks)
            self.assertTrue(jitsi.joined(page))

    def test_fatal_dialog_raises_admission_refused(self):
        with patch.object(jitsi, "get", fake_get):
            page = Page(admit_at=None)
            page.dialogs = ["The meeting is over."]
            with self.assertRaises(jitsi.AdmissionRefused):
                jitsi.prejoin(page, "Live Transcript", timeout=4)

    def test_reconnect_notice_is_not_fatal(self):
        with patch.object(jitsi, "get", fake_get):
            page = Page(admit_at=None)
            page.dialogs = ["You have been disconnected. Reconnecting in 28 sec…"]
            with self.assertRaises(TimeoutError):
                jitsi.prejoin(page, "Live Transcript", timeout=4)

    def test_meeting_ended_detection(self):
        page = Page()
        page.dialogs = ["You have left the meeting"]

        class FramePage:
            def __init__(self, page):
                self.page = page

            @property
            def frames(self):
                return [self]

            def get_by_role(self, role, name=None, **kwargs):
                return Locator(self.page, role)

            all_text_contents = None

        # meeting_ended reads frame.get_by_role(role).all_text_contents()
        class Dialogs:
            def __init__(self, page):
                self.page = page

            def all_text_contents(self):
                return self.page.dialogs

        frame = FramePage(page)
        original = frame.get_by_role

        def get_by_role(role, name=None, **kwargs):
            locator = original(role)
            locator.all_text_contents = lambda: page.dialogs if role == "dialog" else []
            return locator

        frame.get_by_role = get_by_role
        self.assertTrue(jitsi.meeting_ended(frame))
        page.dialogs = ["Welcome to the meeting"]
        self.assertFalse(jitsi.meeting_ended(frame))


class DispatchTests(unittest.TestCase):
    def test_observer_and_recovery_use_configured_https_origins_and_ports(self):
        from live_transcript.core.options import provider_origin
        from live_transcript.capture.observer import speaker_script

        with patch.object(jitsi, "get", fake_get):
            self.assertTrue(provider_origin("jitsi", "https://192.0.2.10:8443/Room"))
            self.assertTrue(provider_origin("jitsi", "https://meet.jit.si/Room"))
            for url in (
                "http://meet.example.org/Room",
                "https://foreign.example.org/Room",
                "https://user:secret@example.org/Room",
                "https://meet.example.org:bad/Room",
            ):
                self.assertFalse(provider_origin("jitsi", url), url)
            self.assertFalse(provider_origin("proton", "https://meet.proton.me:8443/Room"))
            self.assertEqual(speaker_script("jitsi"), jitsi.SPEAKERS_JS)

    def test_jitsi_rescue_keeps_camera_disabled_and_session_identity(self):
        from live_transcript.providers.rescue import validate_candidates, plan_recovery
        from live_transcript.runtime.meeting_view import validate_session

        self.assertEqual(
            validate_session(dict(id="fixture", provider="jitsi"))["provider"], "jitsi"
        )
        snapshot = dict(
            provider="jitsi",
            controls=[dict(id=0, label="Resume audio", action="resume_audio", dialog=False)],
        )
        self.assertEqual(len(validate_candidates(snapshot)), 1)
        with patch("live_transcript.providers.rescue.get", return_value=False):
            self.assertEqual(plan_recovery(snapshot)["id"], 0)
        snapshot["controls"][0].update(label="Turn on camera", action="enable_camera")
        with self.assertRaises(ValueError):
            validate_candidates(snapshot)

    def test_state_validation_accepts_configured_jitsi(self):
        from live_transcript.runtime import state

        with patch.object(jitsi, "get", fake_get):
            state.validate_invitation("https://192.0.2.10:8443/LT-Room")

    def test_zoom_dispatch_routes_jitsi_urls(self):
        import sys

        from live_transcript.providers import zoom

        with (
            patch.object(jitsi, "get", fake_get),
            patch.object(jitsi, "run", return_value=0) as run,
            patch.object(sys, "argv", ["join", "https://meet.jit.si/LT-Room"]),
        ):
            self.assertEqual(zoom.main(), 0)
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
