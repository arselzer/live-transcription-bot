import unittest
from live_transcript.transcription.quality import assess, require_acceptable, TranscriptQualityError


class QualityTests(unittest.TestCase):
    def test_long_alternating_failure_is_caught_without_deleting_candidate(self):
        rows = [
            dict(
                t=i * 2,
                text=(
                    "Ja, ja, ja."
                    if i % 2
                    else "Diese lange Testphrase wird absichtlich immer wiederholt."
                ),
            )
            for i in range(100)
        ]
        original = list(rows)
        report = assess(rows, 200)
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(rows, original)
        self.assertGreater(report["suspect_fraction"], 0.9)
        with self.assertRaises(TranscriptQualityError):
            require_acceptable(rows, 200)

    def test_natural_acknowledgements_do_not_trigger_long_phrase_loop(self):
        rows = [dict(t=i * 3, text="Ja, ja, ja.") for i in range(100)]
        self.assertEqual(assess(rows)["status"], "pass")

    def test_repeated_term_in_varied_conversation_passes(self):
        rows = [
            dict(
                t=i * 3,
                text=f"Question {i} about the current research direction and its different implications",
            )
            for i in range(100)
        ]
        self.assertEqual(assess(rows)["status"], "pass")

    def test_short_real_repeat_is_not_removed_or_rejected(self):
        rows = [dict(t=i * 2, text="Please check the audio connection.") for i in range(3)]
        self.assertEqual(assess(rows)["status"], "pass")

    def test_persistent_single_phrase_loop_is_flagged(self):
        rows = [
            dict(t=i * 2, text="Und ich bin sehr glücklich, dass ich hier bin.") for i in range(15)
        ]
        self.assertIn("sustained_repetition", assess(rows)["issues"])

    def test_out_of_bounds_nonfinite_and_backward_timestamps_fail(self):
        for starts in ([0, 100], [0, float("nan")], [10, 1]):
            with self.subTest(starts=starts):
                self.assertIn(
                    "invalid_segment_timestamps",
                    assess([dict(t=t, text="Speech") for t in starts], 20)["issues"],
                )

    def test_silence_is_not_invented_as_a_recognition_failure(self):
        self.assertEqual(assess([], 100)["status"], "pass")

    def test_invalid_end_timestamps_are_detected(self):
        for end in (-1, float("inf"), 30):
            with self.subTest(end=end):
                self.assertEqual(
                    assess([dict(t=0, end=end, text="Speech")], 20)["status"], "needs_review"
                )
