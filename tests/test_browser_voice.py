import unittest
from unittest.mock import MagicMock
from live_transcript.speech.browser import play_wav


class BrowserVoiceTests(unittest.TestCase):
    def test_speech_targets_live_meeting_frame_not_unused_top_level_destination(self):
        root = MagicMock()
        child = MagicMock()
        root.evaluate.return_value = {"live": 0, "last": 0, "requests": 0}
        child.evaluate.side_effect = [
            {"live": 1, "last": 100, "requests": 2},
            {"started": 100, "duration": 1},
        ]
        page = MagicMock(frames=[root, child])
        result = play_wav(page, b"wav")
        self.assertEqual(root.evaluate.call_count, 1)
        self.assertEqual(child.evaluate.call_count, 2)
        self.assertEqual(result["live_microphone_tracks"], 1)

    def test_no_consumer_is_error_instead_of_false_playback_success(self):
        frame = MagicMock()
        frame.evaluate.return_value = {"live": 0, "last": 0, "requests": 0}
        page = MagicMock(frames=[frame])
        with self.assertRaises(RuntimeError):
            play_wav(page, b"wav")
        self.assertEqual(frame.evaluate.call_count, 1)

    def test_latest_live_microphone_wins_after_device_switch(self):
        older = MagicMock()
        latest = MagicMock()
        older.evaluate.return_value = {"live": 1, "last": 10, "requests": 1}
        latest.evaluate.side_effect = [
            {"live": 1, "last": 20, "requests": 1},
            {"started": 20, "duration": 1},
        ]
        play_wav(MagicMock(frames=[older, latest]), b"wav")
        self.assertEqual(older.evaluate.call_count, 1)
        self.assertEqual(latest.evaluate.call_count, 2)
