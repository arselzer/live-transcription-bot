import unittest
from live_transcript.notes.board import parse_board


class BoardTests(unittest.TestCase):
    def test_explicit_decision_requires_literal_evidence(self):
        raw = '{"kind":"decision","bullets":["Release on Friday."],"evidence":"We agreed to release on Friday"}'
        self.assertEqual(parse_board(raw, "We agreed to release on Friday")["kind"], "decision")
        with self.assertRaises(ValueError):
            parse_board(raw, "We are still discussing dates.")

    def test_fenced_json_and_bounded_bullets(self):
        raw = '```json\n{"kind":"topic","bullets":["One","Two","Three","Four"]}\n```'
        self.assertEqual(parse_board(raw, "")["bullets"], ["One", "Two", "Three"])

    def test_empty_summary_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_board('{"bullets":[]}', "")


if __name__ == "__main__":
    unittest.main()
