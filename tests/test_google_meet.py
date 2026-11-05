"""Google Meet URL admission gates (pure logic; no browser)."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
