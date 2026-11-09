import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

from live_transcript.speech.controller import VoiceController


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "rec").mkdir()
        self.page = MagicMock()
        self.voice = VoiceController(self.page, "meeting", "Live Transcript", self.tmp.name, t0=0)

    def request(self, **changes):
        request = dict(session="meeting", language="en", text="Hello everyone.")
        request.update(changes)
        (self.voice.inbox / "request.json").write_text(json.dumps(request))

    def result(self):
        return json.loads((self.voice.done / "request.json").read_text())

    def audio(self, connect):
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 1600)
        connect.return_value.__enter__.return_value.makefile.return_value.read.return_value = (
            data.getvalue()
        )

    @patch("live_transcript.speech.controller.play_wav")
    @patch("live_transcript.speech.controller.socket.create_connection")
    def test_idle_bot_never_speaks(self, connect, play):
        self.voice.poll()
        connect.assert_not_called()
        play.assert_not_called()
        self.page.get_by_role.assert_not_called()

    @patch("live_transcript.speech.controller.socket.create_connection")
    def test_other_meeting_request_is_rejected(self, connect):
        self.request(session="old-meeting")
        self.voice.poll()
        self.assertFalse(self.result()["ok"])
        connect.assert_not_called()

    @patch("live_transcript.speech.controller.play_wav", return_value={"duration": 0.1})
    @patch("live_transcript.speech.controller.socket.create_connection")
    def test_explicit_speech_is_recorded_and_remuted(self, connect, play):
        self.audio(connect)
        self.request()
        self.voice.poll()
        self.assertTrue(self.result()["ok"])
        self.assertTrue(play.call_args.kwargs["monitor"])
        self.assertIn("Hello everyone.", (self.root / "rec/voice.jsonl").read_text())
        self.page.get_by_role.assert_any_call("button", name="Mute microphone", exact=True)
        self.assertFalse((self.voice.inbox / "request.json").exists())

    @patch("live_transcript.speech.controller.play_wav", return_value={"duration": 0.1})
    @patch("live_transcript.speech.controller.socket.create_connection")
    def test_jitsi_speech_reveals_hidden_controls_and_restores_mute(self, connect, play):
        self.voice.provider = "jitsi"
        self.page.viewport_size = {"width": 1000, "height": 700}
        self.audio(connect)
        self.request()
        self.voice.poll()
        self.assertTrue(self.result()["ok"])
        self.assertGreaterEqual(self.page.mouse.move.call_count, 2)
        self.page.get_by_role.assert_any_call("button", name="Unmute microphone", exact=True)
        self.page.get_by_role.assert_any_call("button", name="Mute microphone", exact=True)
        self.assertEqual(play.call_count, 1)

    @patch(
        "live_transcript.speech.controller.play_wav",
        side_effect=RuntimeError("playback interrupted"),
    )
    @patch("live_transcript.speech.controller.socket.create_connection")
    def test_playback_failure_still_restores_mute(self, connect, play):
        self.audio(connect)
        self.request()
        self.voice.poll()
        self.assertFalse(self.result()["ok"])
        self.page.get_by_role.assert_any_call("button", name="Mute microphone", exact=True)

    @patch("live_transcript.speech.controller.play_wav", return_value={"duration": 0.1})
    @patch("live_transcript.speech.controller.socket.create_connection")
    def test_human_display_name_does_not_attribute_bot_speech_to_a_human(self, connect, play):
        self.voice.name = "Example User"
        self.audio(connect)
        self.request()
        self.voice.poll()
        event = json.loads((self.root / "rec/voice.jsonl").read_text())
        self.assertEqual(event["name"], "Example User")
        self.assertEqual(event["speaker_label"], "Live Transcript (Example User)")
        self.assertEqual(event["source"], "bot-tts")

    @patch("live_transcript.speech.controller.socket.create_connection")
    def test_completed_request_id_cannot_speak_twice(self, connect):
        self.request()
        (self.voice.done / "request.json").write_text('{"ok":true}')
        self.voice.poll()
        connect.assert_not_called()
        self.assertFalse((self.voice.inbox / "request.json").exists())

    def test_interrupted_playback_requires_explicit_retry(self):
        pending = self.voice.inbox / "speech.inflight"
        pending.write_text('{"session":"meeting","text":"Possibly spoken"}')
        VoiceController(self.page, "meeting", "Live Transcript", self.tmp.name, t0=0)
        result = json.loads((self.voice.done / "speech.json").read_text())
        self.assertFalse(result["ok"])
        self.assertIn("may have played", result["error"])
        self.assertTrue((self.voice.done / "speech.json.interrupted").exists())

    @patch("live_transcript.speech.controller.socket.create_connection")
    def test_expired_voice_request_stays_silent(self, connect):
        self.request(queued_at=1)
        self.voice.poll()
        connect.assert_not_called()
        self.assertFalse(self.result()["ok"])


if __name__ == "__main__":
    unittest.main()
