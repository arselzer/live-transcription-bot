"""Run on the VM (Pillow installed): python3 -m unittest test_caption_render -v."""

import unittest
from unittest.mock import patch
import tempfile, json, time
from pathlib import Path
import live_transcript.presentation.render as render
from live_transcript.presentation.render import link_warning


class LinkWarningTests(unittest.TestCase):
    def test_fresh_epoch_timestamp_is_healthy(self):
        with patch(
            "live_transcript.presentation.render.os.path.getmtime", return_value=1_800_000_000
        ):
            self.assertIsNone(
                link_warning({"t": 1_800_000_000, "chunk_age": 1.1}, "gpu.json", now=1_800_000_003)
            )

    def test_stale_file_reports_link_failure(self):
        with patch(
            "live_transcript.presentation.render.os.path.getmtime", return_value=1_800_000_000
        ):
            self.assertEqual(
                link_warning({"t": 1_800_000_000}, "gpu.json", now=1_800_000_013), "DATA LINK DOWN"
            )

    def test_fresh_link_can_report_stale_audio(self):
        with patch(
            "live_transcript.presentation.render.os.path.getmtime", return_value=1_800_000_000
        ):
            self.assertEqual(
                link_warning({"chunk_age": 9}, "gpu.json", now=1_800_000_001), "NO AUDIO ARRIVING"
            )

    def test_missing_file_or_invalid_data_reports_link_failure(self):
        with patch(
            "live_transcript.presentation.render.os.path.getmtime", side_effect=FileNotFoundError
        ):
            self.assertEqual(link_warning({}, "gpu.json"), "DATA LINK DOWN")
        with patch(
            "live_transcript.presentation.render.os.path.getmtime", return_value=1_800_000_000
        ):
            self.assertEqual(link_warning(None, "gpu.json", now=1_800_000_001), "DATA LINK DOWN")


class LayoutTests(unittest.TestCase):
    def test_consecutive_same_speaker_fragments_form_one_turn(self):
        rows = [
            (1, "Alice", "We should keep"),
            (2, "Alice", "the full audio."),
            (3, "Bob", "Agreed."),
            (4, "Alice", "Thank you."),
        ]
        self.assertEqual(
            render.group_turns(rows),
            [
                ("Alice", "We should keep the full audio."),
                ("Bob", "Agreed."),
                ("Alice", "Thank you."),
            ],
        )

    def test_pause_and_unknown_speakers_do_not_merge(self):
        rows = [
            (0, "Alice", "First turn."),
            (30, "Alice", "Later turn."),
            (31, "", "Unknown one."),
            (32, "", "Unknown two."),
        ]
        self.assertEqual(len(render.group_turns(rows)), 4)

    def test_recent_complete_sentence_survives_display_limit(self):
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (500, 500)))
        font = render._font(render.FONT_REG, 30)
        text = "An older sentence has many words. Keep all audio."
        result = render.visible_text(draw, text, font, 300, 2)
        self.assertEqual(" ".join(result), "Keep all audio.")

    def test_wrapping_keeps_long_words_inside_pixel_width(self):
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGB", (500, 500)))
        font = render._font(render.FONT_REG, 30)
        lines = render.wrap(draw, "AnExtremelyLongUnbrokenName" * 8 + " normal words", font, 300)
        self.assertTrue(all(draw.textlength(line, font=font) <= 300 for line in lines))
        self.assertIn("normal words", " ".join(lines))

    def test_portrait_and_landscape_cards_never_overlap_or_clip(self):
        with tempfile.TemporaryDirectory() as d, patch.object(render, "D", d):
            Path(d, "gpu.json").write_text(json.dumps({"t": time.time(), "chunk_age": 0.5}))
            Path(d, "captions.json").write_text(
                json.dumps(
                    {
                        "lines": [
                            [
                                i,
                                "A long participant display name " * 4,
                                "Words that need readable wrapping. " * 20,
                            ]
                            for i in range(7)
                        ]
                    }
                )
            )
            for width, height in ((1080, 1350), (1440, 1080), (1920, 1080)):
                with (
                    self.subTest(size=(width, height)),
                    patch.object(render, "W", width),
                    patch.object(render, "H", height),
                ):
                    image = render.frame(time.time() - 60)
                    self.assertEqual(image.size, (width, height))
                    self.assertTrue(render.LAST_LAYOUT)
                    previous = 0
                    for left, top, right, bottom in render.LAST_LAYOUT:
                        self.assertGreaterEqual(top, previous)
                        self.assertGreaterEqual(top, 268)
                        self.assertLessEqual(bottom, height - 172)
                        self.assertGreaterEqual(left, 0)
                        self.assertLessEqual(right, width)
                        previous = bottom

    def test_summary_peek_waits_for_pause_and_returns_to_transcript(self):
        board = {"bullets": ["The team is reviewing the release."], "version": 1, "updated_at": 100}
        with patch.object(render, "_PEEK", {"version": None, "until": 0}):
            self.assertFalse(render.visible_board(board, {"last_speech_age": 0}, "Alice", now=100))
            self.assertTrue(render.visible_board(board, {"last_speech_age": 3}, "Alice", now=101))
            self.assertFalse(render.visible_board(board, {"last_speech_age": 3}, "Alice", now=110))

    def test_decisions_can_surface_during_speech_without_permanent_takeover(self):
        board = {
            "kind": "decision",
            "bullets": ["The agreed release date is Friday."],
            "version": 2,
            "updated_at": 100,
        }
        with patch.object(render, "_PEEK", {"version": None, "until": 0}):
            self.assertTrue(render.visible_board(board, {}, "Alice", now=100))
            self.assertFalse(render.visible_board(board, {}, "Alice", now=109))


if __name__ == "__main__":
    unittest.main()
