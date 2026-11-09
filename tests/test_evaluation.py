"""Reference-backed scoring must not disguise missing speech or overlaps."""

from copy import deepcopy
import unittest

from live_transcript.transcription import evaluation as e


class EvaluationTests(unittest.TestCase):
    def reference(self):
        return [
            dict(
                id="one",
                text="Seven green notebooks",
                language="en",
                speaker="A",
                start=2,
                end=5,
                offset=0,
                audio_sha256="a",
            ),
            dict(
                id="two",
                text="Die blaue Tasse",
                language="de",
                speaker="B",
                start=8,
                end=10,
                offset=6,
                audio_sha256="b",
            ),
        ]

    def test_edit_counts_and_unicode_normalization(self):
        result = e.errors("one two three", "one four extra three")
        self.assertEqual(
            (result["substitutions"], result["insertions"], result["deletions"]), (1, 1, 0)
        )
        self.assertEqual(e.errors("GRÜNE, Straße!", "grüne strasse")["edits"], 0)
        self.assertEqual(e.errors("one two", "")["deletions"], 2)
        self.assertIsNone(e.errors("", "unexpected")["word_error_rate"])

    def test_missing_phrases_and_extra_passages_remain_visible(self):
        report = e.score(
            self.reference(),
            [
                dict(t=2, end=5, text="Seven green notebooks"),
                dict(t=20, end=21, text="Unrelated words"),
            ],
        )
        self.assertEqual(report["possibly_missed_phrases"], 1)
        self.assertEqual(len(report["unreferenced_passages"]), 1)
        self.assertGreater(report["wer"]["deletions"], 0)
        self.assertGreater(report["tokens"]["unmatched_tokens"], 0)
        self.assertEqual(report["phrases"][0]["speaker_observation"], "unknown")

    def test_overlap_has_no_ordered_wer_or_unique_speaker_grade(self):
        reference = self.reference()
        reference[1].update(start=3, end=7)
        report = e.score(
            reference, [dict(t=2, end=7, text="Die blaue Tasse Seven green notebooks", speaker="A")]
        )
        self.assertIsNone(report["wer"])
        self.assertEqual(report["tokens"]["token_recall"], 1)
        self.assertTrue(
            all(r["speaker_observation"] == "ambiguous-overlap" for r in report["phrases"])
        )

    def test_first_caption_latency_and_speaker_uncertainty_are_separate(self):
        events = [dict(received_t=6, lines=[dict(t=2, text="Seven green notebooks")])]
        report = e.score(
            self.reference(),
            [dict(t=2, end=5, text="Seven green notebooks", speaker="A")],
            caption_events=events,
        )
        self.assertEqual(report["caption_latency"]["p95"], 4)
        self.assertEqual(report["caption_latency"]["count"], 1)
        self.assertEqual(report["phrases"][0]["speaker_observation"], "correct")
        self.assertEqual(report["phrases"][1]["speaker_observation"], "unknown")

    def test_faster_output_cannot_excuse_accuracy_regression(self):
        reference = self.reference()
        baseline = e.score(
            reference, [dict(t=2, end=10, text="Seven green notebooks Die blaue Tasse")]
        )
        current = e.score(reference, [])
        current["caption_latency"]["p95"] = 0.1
        result = e.compare(current, baseline)
        self.assertFalse(result["ok"])
        self.assertIn("word_error_rate", result["regressions"])
        changed = deepcopy(baseline)
        changed["reference_signature"] = "different input"
        with self.assertRaises(ValueError):
            e.compare(current, changed)

    def test_invalid_or_unbounded_reference_and_transcript_are_rejected(self):
        for value in (float("nan"), float("inf"), -1):
            reference = self.reference()
            reference[0]["start"] = value
            with self.assertRaises(ValueError):
                e.score(reference, [])
        with self.assertRaises(ValueError):
            e.score(self.reference(), [dict(t=0, end=1, text=3)])
        with self.assertRaises(ValueError):
            e.errors("word " * 3000, "word " * 3000)

    def test_reference_order_matters_for_baseline_compatibility(self):
        original = self.reference()
        changed = deepcopy(original)
        changed[1]["offset"] = 30
        self.assertNotEqual(e.signature(original), e.signature(changed))

    def test_nearby_unrelated_speaker_does_not_invent_a_mismatch(self):
        reference = self.reference()
        value = e.score(
            reference,
            [
                dict(t=2, end=5, text="Seven green notebooks", speaker="A"),
                dict(t=6, end=7, text="Unrelated remark", speaker="C"),
                dict(t=8, end=10, text="Die blaue Tasse", speaker="B"),
            ],
        )
        self.assertEqual(value["phrases"][1]["speaker_observation"], "correct")

    def test_nonfinite_or_corrupt_baselines_cannot_pass(self):
        baseline = e.score(self.reference(), [])
        for changed in (
            dict(baseline, tokens={"token_recall": float("nan")}),
            dict(baseline, wer={"word_error_rate": -1}),
            dict(baseline, schema=True),
        ):
            with self.assertRaises(ValueError):
                e.compare(baseline, changed)
