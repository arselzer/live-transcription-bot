import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch
import wave

import live_transcript.transcription.daemon as app
from live_transcript.transcription.runtime import CaptionRuntime, language_choice
from live_transcript.transcription.store import CaptionStore
from live_transcript.notes.summary import sections, summarize
from live_transcript.storage.layout import migrate


def wav_bytes(pcm):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return out.getvalue()


class WhisperRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.wav = Path(temporary.name) / "input.wav"
        self.wav.write_bytes(wav_bytes(b"\0\0" * 16000))

    def failure(self, message, status=500):
        error = urllib.error.HTTPError(
            "http://example.invalid/inference", status, "failure", {}, io.BytesIO(message)
        )
        self.addCleanup(error.close)
        return error

    def response(self, segments=(), probabilities=None):
        return io.BytesIO(
            json.dumps(
                dict(segments=list(segments), language_probabilities=probabilities or {})
            ).encode()
        )

    def test_optional_language_failure_retries_same_audio_within_original_budget(self):
        failure = self.failure(
            b"500 Internal Server Error\nbasic_string: construction from null is not valid"
        )
        response = self.response(
            [dict(start=0, end=1, text="Keep every spoken word.", avg_logprob=-0.1)]
        )
        with (
            patch("urllib.request.urlopen", side_effect=[failure, response]) as request,
            patch("time.monotonic", side_effect=[100, 102]),
        ):
            result = app.stt_server(self.wav, "en", "project context", timeout=5)
        self.assertEqual(result, [(0, "Keep every spoken word.")])
        self.assertTrue(result.language_metadata_fallback)
        self.assertEqual(request.call_args_list[0].kwargs["timeout"], 5)
        self.assertEqual(request.call_args_list[1].kwargs["timeout"], 3)
        first, second = [call.args[0].data for call in request.call_args_list]
        self.assertIn(b'name="no_language_probabilities"\r\n\r\nfalse', first)
        self.assertIn(b'name="no_language_probabilities"\r\n\r\ntrue', second)
        for payload in (first, second):
            self.assertIn(self.wav.read_bytes(), payload)
            self.assertIn(b'name="language"\r\n\r\nen', payload)
            self.assertIn(b"project context", payload)

    def test_actual_empty_response_is_accepted_after_metadata_retry(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=[self.failure(b"basic_string: construction from null"), self.response()],
        ):
            self.assertEqual(app.stt_server(self.wav, "en"), [])

    def test_other_errors_are_not_reinterpreted_as_silence(self):
        for code, message in [(500, b"failed to process audio"), (503, b"unavailable")]:
            with patch(
                "urllib.request.urlopen", side_effect=self.failure(message, code)
            ) as request:
                with self.assertRaises(urllib.error.HTTPError):
                    app.stt_server(self.wav, "en")
                self.assertEqual(request.call_count, 1)

    def test_failed_retry_remains_a_failure_and_cannot_loop(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=[
                self.failure(b"basic_string: construction from null"),
                self.failure(b"failed to process audio"),
            ],
        ) as request:
            with self.assertRaises(urllib.error.HTTPError):
                app.stt_server(self.wav, "en")
            self.assertEqual(request.call_count, 2)

    def test_successful_requests_keep_language_probabilities(self):
        with patch(
            "urllib.request.urlopen",
            return_value=self.response(probabilities=dict(en=0.85, de=0.15)),
        ) as request:
            result = app.stt_server(self.wav, "en")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result.language_probs, dict(en=0.85, de=0.15))
        self.assertFalse(result.language_metadata_fallback)


class PipelineTests(unittest.TestCase):
    def test_low_confidence_leading_segment_cannot_erase_published_clause(self):
        self.add()
        st = self.state()
        st["generation"] = 1
        st["lines"] = [
            (0, "Alice", "Bitte stellt sicher, dass die Untertitel gut lesbar und"),
            (3, "Alice", "genau sind."),
        ]
        st["utterance_pcm"] = bytearray(st["pcm"])
        st["utterance_t0"] = 0
        st["utterance_mark"] = 0
        segments = app.ASRSegments([(3, "genau sind.")])
        with patch.object(app, "transcribe", return_value=segments):
            self.runtime.finalize_speech("meeting", st, bytes(st["pcm"]), 0, 0, 1)
        self.assertEqual(len(st["lines"]), 2)
        self.assertTrue(st["lines"][0][2].startswith("Bitte stellt"))

    def test_speaker_switch_in_padding_does_not_disable_final_correction(self):
        self.add()
        st = self.state()
        st["generation"] = 1
        st["lines"] = [(0, "Prior speaker", "Provisional mistake here.")]
        st["timeline"] = [(0, "Prior speaker"), (0.2, "Current speaker")]
        st["utterance_pcm"] = bytearray(st["pcm"])
        st["utterance_t0"] = 0
        st["utterance_mark"] = 0
        segments = app.ASRSegments([(0.3, "Correct full utterance.")])
        segments.segment_ends[(0.3, "Correct full utterance.")] = 1.4
        with patch.object(app, "transcribe", return_value=segments):
            self.runtime.finalize_speech("meeting", st, bytes(st["pcm"]), 0, 0, 1)
        self.assertEqual(st["lines"], [(0.3, "Current speaker", "Correct full utterance.")])

    def test_ambiguous_shared_segment_preserves_both_speaker_names(self):
        self.assertEqual(app.segment_speaker([(0, "Alice"), (1, "Bob")], 0, 2), "Alice / Bob")
        self.assertEqual(app.segment_speaker([(0, "Alice"), (0.1, "Bob")], 0, 2), "Bob")

    def test_capture_gap_settles_prior_utterance_before_crossing_clock_gap(self):
        self.add(audio_t0=0)
        st = self.state()
        st["decoded_until"] = 1.5
        self.add(seq=1, audio_t0=5)
        self.runtime.fill("meeting", st)
        self.assertTrue(st["force_finalize"])
        self.assertEqual(st["ingest_id"], 1)
        self.assertEqual(len(st["utterance_pcm"]), 48000)

    def test_long_history_trim_keeps_current_utterance_anchor(self):
        st = self.runtime.new_state()
        st["lines"] = [(i, "Alice", "Earlier statement.") for i in range(3000)]
        st["utterance_mark"] = 2999
        app.commit_apply(st, "Alice", 3000, "The current statement is still being spoken.")
        self.assertEqual(len(st["lines"]), 3000)
        self.assertEqual(st["utterance_mark"], 2998)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite3"
        self.saved = app.STATE, app.RUNTIME, app.CAP_FILE
        app.STATE = {}
        app.CAP_FILE = str(Path(self.temp.name) / "caps.tsv")
        self.store = CaptionStore(self.path)
        self.runtime = CaptionRuntime(app, self.store)
        app.RUNTIME = self.runtime

    def tearDown(self):
        self.runtime.stopped.set()
        with self.runtime.changed:
            self.runtime.changed.notify_all()
        self.store.close()
        app.STATE, app.RUNTIME, app.CAP_FILE = self.saved
        self.temp.cleanup()

    def add(self, seq=0, pcm=None, **extra):
        meta = dict(stream="capture-a", seq=seq, timeline=[])
        meta.update(extra)
        return self.runtime.accept("meeting", meta, pcm or b"\x01\x08" * 24000)

    def state(self):
        self.runtime.fill("meeting", app.STATE["meeting"])
        st = app.STATE["meeting"]
        st["pick"] = "en"
        return st

    def test_lost_ack_retry_does_not_duplicate_audio(self):
        self.assertTrue(self.add())
        self.assertFalse(self.add())
        self.assertEqual(self.store.backlog_seconds("meeting"), 1.5)

    def test_cpu_backlog_preserves_audio_time_silence_boundary(self):
        self.add(seq=0, audio_t0=0, pcm=b"\x01\x08" * 12000)
        for seq in range(1, 7):
            self.add(seq=seq, audio_t0=seq * 0.75, pcm=b"\0\0" * 12000)
        self.add(seq=7, audio_t0=5.25, pcm=b"\x01\x08" * 12000)
        st = self.state()
        self.assertTrue(st["force_finalize"])
        boundary = st["ingest_id"]
        self.runtime.fill("meeting", st)
        self.assertEqual(st["ingest_id"], boundary)
        self.assertGreater(self.store.backlog_seconds("meeting", boundary), 0)
        self.assertLessEqual(len(st["utterance_pcm"]) / 32000, 4.5)

    def test_receipt_survives_retirement_and_reopen(self):
        self.add()
        st = self.state()
        self.store.checkpoint("meeting", st)
        self.assertEqual(self.store.backlog_seconds("meeting"), 0)
        other = CaptionStore(self.path)
        try:
            self.assertFalse(
                other.accept(
                    "meeting", dict(stream="capture-a", seq=0, timeline=[]), b"\x01\x08" * 24000
                )
            )
            self.assertEqual(bytes(other.restore("meeting")["pcm"]), bytes(st["pcm"]))
        finally:
            other.close()

    def test_sequence_gap_and_identity_reuse_are_rejected(self):
        with self.assertRaises(ValueError):
            self.add(seq=1)
        self.add()
        with self.assertRaises(ValueError):
            self.add(pcm=b"\x02\x08" * 24000)

    def test_packet_ids_keep_increasing_after_checkpoint_empties_queue(self):
        self.add()
        st = self.state()
        previous = st["ingest_id"]
        self.store.checkpoint("meeting", st)
        self.add(seq=1)
        rows = self.store.pending("meeting", previous)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0][0], previous)

    def test_full_transcript_outlives_the_visible_caption_window(self):
        self.add()
        st = self.state()
        st["lines"] = [(0, "Alice", "The first decision.")]
        self.store.checkpoint("meeting", st)
        st["lines"] = [(100, "Bob", "The final decision.")]
        self.store.checkpoint("meeting", st)
        self.assertEqual(len(self.store.transcript("meeting")), 2)

    def test_first_large_checkpoint_persists_more_than_camera_window(self):
        st = dict(lines=[(i, "Alice", f"Statement {i}") for i in range(40)])
        self.store.checkpoint("meeting", st)
        self.assertEqual(self.store.transcript("meeting"), st["lines"])

    def test_large_appended_batch_is_not_truncated_to_twelve_entries(self):
        st = dict(lines=[(i, "Alice", f"Statement {i}") for i in range(15)])
        self.store.checkpoint("meeting", st)
        st["lines"].extend((i, "Bob", f"Statement {i}") for i in range(15, 50))
        self.store.checkpoint("meeting", st)
        self.assertEqual(self.store.transcript("meeting"), st["lines"])

    def test_long_correction_persists_its_leading_segments_and_shifted_start(self):
        st = dict(lines=[(0, "Alice", "Old final speech"), (10, "Bob", "Draft")])
        self.store.checkpoint("meeting", st)
        st["reconcile_from"] = 10
        st["lines"] = [(0, "Alice", "Old final speech")] + [
            (9.5 + i, "Bob", f"Corrected {i}") for i in range(30)
        ]
        self.store.checkpoint("meeting", st)
        self.assertEqual(self.store.transcript("meeting"), st["lines"])

    def test_first_checkpoint_after_restart_repairs_legacy_card_omissions(self):
        st = dict(lines=[(i, "Alice", f"Statement {i}") for i in range(40)])
        self.store.checkpoint("meeting", st)
        with self.store.db:
            self.store.db.execute("DELETE FROM cards WHERE t<20")
        other = CaptionStore(self.path)
        try:
            restored = other.restore("meeting")
            other.checkpoint("meeting", restored)
            self.assertEqual(other.transcript("meeting"), st["lines"])
        finally:
            other.close()

    def test_journal_backpressure_retains_existing_audio(self):
        self.store.max_pending_bytes = 50000
        self.add()
        with self.assertRaises(BufferError):
            self.add(seq=1)
        self.assertEqual(self.store.backlog_seconds("meeting"), 1.5)

    def test_restore_uncheckpointed_audio_after_crash(self):
        self.add()
        self.add(seq=1)
        app.STATE = {}
        restored = CaptionRuntime(app, self.store)
        restored.fill("meeting", app.STATE["meeting"])
        self.assertEqual(len(app.STATE["meeting"]["pcm"]), 96000)

    def test_speaker_uses_fractional_meeting_timestamp(self):
        self.add(audio_t0=12, timeline=[(0, "Alice"), (12.2, "Bob")])
        st = self.state()
        st["generation"] = 1
        with (
            patch.object(app, "COMMIT_ONCE", True),
            patch.object(
                app, "transcribe", return_value=[(0.25, "Here are several clear spoken words.")]
            ),
        ):
            self.runtime.decode("meeting", st, bytes(st["pcm"]), 12, 1)
        self.assertEqual(st["lines"][0][1], "Bob")
        self.assertAlmostEqual(st["lines"][0][0], 12.25)

    def test_acoustic_language_switch_prevents_translation_lock(self):
        self.add()
        st = self.state()
        st["generation"] = 1
        en = app.ASRSegments(
            [(0, "This is an automatic German transcription test.")], {"de": 0.96, "en": 0.02}
        )
        de = app.ASRSegments(
            [(0, "Dies ist der automatische deutsche Transkriptionstest.")],
            {"de": 0.96, "en": 0.02},
        )
        with (
            patch.object(app, "COMMIT_ONCE", True),
            patch.object(app, "transcribe", side_effect=[en, de]) as calls,
        ):
            self.runtime.decode("meeting", st, bytes(st["pcm"]), 0, 1)
        self.assertEqual(st["pick"], "de")
        self.assertIn("Dies ist", st["lines"][-1][2])
        self.assertEqual(calls.call_args_list[1].args[1], "de")

    def test_language_router_never_selects_other_languages(self):
        self.assertEqual(language_choice({"ru": 0.95, "de": 0.02, "en": 0.01}, "en"), "en")
        self.assertEqual(language_choice({"de": 0.46, "en": 0.44}, "en"), "en")
        self.assertEqual(language_choice({"de": 0.9, "en": 0.05}, "en"), "de")

    def test_provisional_guesses_are_corrected_before_final_publication(self):
        st = CaptionRuntime.new_state()
        app.commit_apply(st, "TTS", 0, "This is the automated Ingle Transcription Test")
        app.commit_apply(st, "TTS", 0, "This is the automated English transcription test.")
        app.commit_apply(st, "TTS", 1, "automated English transcription test.")
        app._finalize(st)
        self.assertEqual(st["lines"][-1][2], "This is the automated English transcription test.")

    def test_unpublished_short_name_guess_can_be_replaced(self):
        st = CaptionRuntime.new_state()
        app.commit_apply(st, "TTS", 0, "Example Wilson")
        app.commit_apply(st, "TTS", 0, "Example will send the project report before Friday.")
        app._finalize(st)
        self.assertEqual(st["lines"][-1][2], "Example will send the project report before Friday.")

    def test_utterance_audio_survives_checkpoint(self):
        self.add()
        st = self.state()
        self.store.checkpoint("meeting", st)
        restored = self.store.restore("meeting")
        self.assertEqual(restored["utterance_pcm"], st["utterance_pcm"])

    def test_final_pass_corrects_draft_without_changing_older_history(self):
        self.add()
        st = self.state()
        st["generation"] = 1
        st["lines"] = [
            (-10, "Alice", "Earlier final statement."),
            (0, "Alice", "A provisional mistake."),
        ]
        self.store.checkpoint("meeting", st)
        result = app.ASRSegments([(0.25, "The complete corrected final sentence.")])
        result.segment_ends[(0.25, result[0][1])] = 1.4
        with patch.object(app, "transcribe", return_value=result):
            self.runtime.finalize_speech("meeting", st, bytes(st["utterance_pcm"]), 0, 1, 1)
        self.assertEqual(st["lines"][0][2], "Earlier final statement.")
        self.assertEqual(st["lines"][1][2], "The complete corrected final sentence.")
        self.assertNotIn("provisional", str(self.store.transcript("meeting")))

    def test_failed_final_pass_retains_utterance_audio(self):
        self.add()
        st = self.state()
        st["generation"] = 1
        original = bytes(st["utterance_pcm"])
        with patch.object(app, "transcribe", return_value=None):
            self.runtime.finalize_speech("meeting", st, original, 0, 0, 1)
        self.assertEqual(bytes(st["utterance_pcm"]), original)

    def test_final_pass_preserves_new_speech_arriving_during_inference(self):
        self.add()
        st = self.state()
        st["generation"] = 1
        original = bytes(st["utterance_pcm"])
        new_audio = b"\x03\x08" * 12000

        def recognize(*args):
            st["utterance_pcm"].extend(new_audio)
            st["pcm"].extend(new_audio)
            return [(0, "The complete corrected final sentence.")]

        with patch.object(app, "transcribe", side_effect=recognize):
            self.runtime.finalize_speech("meeting", st, original, 0, 0, 1)
        self.assertEqual(bytes(st["utterance_pcm"]), new_audio)
        self.assertAlmostEqual(st["utterance_t0"], 1.5)

    def test_old_decoder_cannot_mutate_new_generation(self):
        self.add()
        st = self.state()
        st["generation"] = 2
        with patch.object(
            app, "transcribe", return_value=[(0, "Here are several clear spoken words.")]
        ):
            self.runtime.decode("meeting", st, bytes(st["pcm"]), 0, 1)
        self.assertEqual(st["lines"], [])
        self.assertEqual(st["decoded_until"], 0)

    def test_backend_failure_retains_audio_for_retry(self):
        self.add()
        st = self.state()
        st["generation"] = 1
        original = bytes(st["pcm"])
        with patch.object(app, "transcribe", return_value=None):
            self.runtime.decode("meeting", st, original, 0, 1)
        self.assertEqual(bytes(st["pcm"]), original)
        self.assertEqual(st["decoded_until"], 0)
        self.assertFalse(st["busy"])

    def test_no_live_audio_discarded_while_decode_runs(self):
        self.add(pcm=b"\x01\x08" * 64000)
        st = self.state()
        st["generation"] = 1
        snap = bytes(st["pcm"])

        def recognize(*_):
            st["pcm"].extend(b"\x03\x08" * 12000)
            return [(0, "Here are several clear spoken words.")]

        with (
            patch.object(app, "COMMIT_ONCE", True),
            patch.object(app, "transcribe", side_effect=recognize),
        ):
            self.runtime.decode("meeting", st, snap, 0, 1)
        self.assertTrue(st["pcm"].endswith(b"\x03\x08" * 12000))
        self.assertEqual(len(st["pcm"]), int(3.25 * 32000))

    def test_timeline_resends_do_not_grow_or_unsort_events(self):
        timeline = [(0, "Alice"), (1, "Bob")]
        self.add(timeline=timeline)
        self.add(seq=1, timeline=timeline)
        st = self.state()
        self.assertEqual(st["timeline"], timeline)

    def test_memory_is_bounded_and_excess_remains_durable(self):
        for i in range(24):
            self.add(seq=i)
        st = self.state()
        self.runtime.fill("meeting", st)
        self.assertLessEqual(len(st["pcm"]), 30 * 32000)
        self.assertGreater(self.store.backlog_seconds("meeting", st["ingest_id"]), 0)

    def test_persistent_audio_protocol_retries_same_packet_once(self):
        pcm = b"\x01\x08" * 24000
        wav = wav_bytes(pcm)
        for _ in range(2):
            client, server = socket.socketpair()
            thread = threading.Thread(target=app.worker, args=(server,))
            thread.start()
            reader = client.makefile("rb")
            client.sendall(b"STREAM2 meeting\n")
            self.assertEqual(reader.readline(), b"OK\n")
            meta = dict(stream="wire-a", seq=0, size=len(wav), timeline=[])
            client.sendall(json.dumps(meta).encode() + b"\n" + wav)
            self.assertEqual(reader.readline(), b"OK\n")
            client.shutdown(socket.SHUT_WR)
            thread.join(timeout=2)
            reader.close()
            client.close()
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.backlog_seconds("meeting"), 1.5)

    def test_subscription_pushes_without_poll_interval(self):
        self.add()
        st = self.state()
        client, server = socket.socketpair()
        client.settimeout(1)
        thread = threading.Thread(target=self.runtime.subscribe, args=(server, "meeting"))
        thread.start()
        reader = client.makefile("rb")
        json.loads(reader.readline())
        start = time.monotonic()
        with self.runtime.changed:
            st["lines"] = [(0, "Alice", "A newly published caption.")]
            self.runtime.publish("meeting", st)
        data = json.loads(reader.readline())
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(data["lines"][0][2], "A newly published caption.")
        self.runtime.stopped.set()
        with self.runtime.changed:
            self.runtime.changed.notify_all()
        thread.join(timeout=2)
        reader.close()
        client.close()
        server.close()


class SummaryAndStorageTests(unittest.TestCase):
    def test_summary_reads_ending_after_old_truncation_boundary(self):
        calls = []

        def model(system, text, **kwargs):
            calls.append(text)
            return (
                "Final decision: keep the final outcome."
                if "FINAL_OUTCOME" in text
                else "Earlier section notes."
            )

        transcript = (
            "A meeting statement about the project.\n" * 2000
        ) + "FINAL_OUTCOME: approve the final plan."
        result = summarize(transcript, {}, model, "Write English notes.")
        self.assertTrue(any("FINAL_OUTCOME" in text for text in calls))
        self.assertIn("Final decision:", calls[-1])
        self.assertTrue(result)

    def test_sections_preserve_all_text_including_long_lines(self):
        text = "a" * 500 + "\n" + ("another line\n" * 100)
        parts = sections(text, 200)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part) <= 200 for part in parts))

    def test_recording_migration_verifies_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as d:
            source, dest = Path(d) / "local", Path(d) / "data"
            source.mkdir()
            (source / "recording.wav").write_bytes(b"original audio")
            migrate(source, dest)
            self.assertTrue(source.is_symlink())
            self.assertEqual((source / "recording.wav").read_bytes(), b"original audio")

    def test_storage_conflict_preserves_original(self):
        with tempfile.TemporaryDirectory() as d:
            source, dest = Path(d) / "local", Path(d) / "data"
            source.mkdir()
            dest.mkdir()
            (source / "a").write_text("original")
            (dest / "a").write_text("different")
            with self.assertRaises(ValueError):
                migrate(source, dest)
            self.assertEqual((source / "a").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
