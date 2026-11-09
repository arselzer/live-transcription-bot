"""Lab isolation, deterministic inputs and scoped evidence recovery."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from live_transcript.core import config
from live_transcript.runtime import lab, lab_guest


class LabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        values = deepcopy(config.DEFAULTS)
        values["paths"].update(
            root=str(self.root), logs=str(self.root / "logs"), state=str(self.root / "state")
        )
        values["vm"].update(home=str(self.root / "guest"), known_hosts="")
        values["providers"]["jitsi_hosts"] = "meet.example.org, 192.0.2.10"
        self.displays = patch("live_transcript.runtime.console.displays", return_value=[])
        self.displays.start()
        self.addCleanup(self.displays.stop)
        self.patch = patch.object(config, "CONFIG", values)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_lab_only_joins_an_explicit_self_owned_server_origin(self):
        self.assertEqual(
            lab.server_url("https://meet.example.org:8443/"), "https://meet.example.org:8443"
        )
        for value in (
            "https://meet.jit.si",
            "https://unknown.example.org",
            "http://meet.example.org",
            "https://meet.example.org/Room",
            "https://user:secret@example.org",
            "https://meet.example.org:bad",
        ):
            with self.assertRaises(ValueError, msg=value):
                lab.server_url(value)

    def test_guest_lease_refuses_an_owned_meeting_before_any_children_start(self):
        home = self.root / "guest"
        home.mkdir()
        with patch.object(
            lab_guest.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="live-transcript-archive-active.service"),
        ):
            with self.assertRaises(RuntimeError):
                with lab_guest.idle_lease(home):
                    self.fail("active recording was not protected")
        with patch.object(lab_guest.subprocess, "run", return_value=SimpleNamespace(stdout="")):
            with lab_guest.idle_lease(home):
                with self.assertRaises(BlockingIOError):
                    with lab_guest.idle_lease(home):
                        self.fail("second lease acquired")

    def test_foreign_receipt_cannot_retrieve_another_vms_files(self):
        receipt = dict(token="a" * 32, identity="different", remote="/unrelated")
        with patch.object(lab.subprocess, "run") as process:
            with self.assertRaises(ValueError):
                lab.collect(self.root, receipt)
            process.assert_not_called()

    def test_artifact_upload_refuses_path_traversal_before_ssh(self):
        with patch.object(lab, "remote") as remote:
            with self.assertRaises(ValueError):
                lab.upload(self.root, {"../private": b"fixture"})
            remote.assert_not_called()

    def test_scenarios_preserve_original_audio_and_reproduce_transformations(self):
        source = self.root / "source"
        source.mkdir()
        path = source / "phrase.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x10" * 16000)
        original = path.read_bytes()
        phrase = dict(
            id="one",
            language="en",
            text="Known sentence",
            file="phrase.wav",
            duration=1,
            speaker="A",
        )
        for scenario in ("clean", "quiet", "noise"):
            a = lab.prepare_inputs(source, self.root / (scenario + "a"), [phrase], scenario)
            b = lab.prepare_inputs(source, self.root / (scenario + "b"), [phrase], scenario)
            self.assertEqual(a, b)
        self.assertEqual(path.read_bytes(), original)

    def test_existing_output_is_never_overwritten(self):
        args = SimpleNamespace(
            lab_action="jitsi",
            silence_finalize=None,
            server="https://meet.example.org",
            output=str(self.root),
        )
        with self.assertRaisesRegex(ValueError, "new directory"):
            lab.run(args)

    def test_incomplete_playback_cannot_produce_a_passing_report(self):
        receipt = dict(session="lab-fixture", phrases=[dict(id="expected")])
        (self.root / "receipt.json").write_text(json.dumps(receipt))
        archive = self.root / "guest/rec/lab-fixture.archive"
        archive.mkdir(parents=True)
        (self.root / "guest/result.json").write_text('{"ok":true,"finalized":true}')
        (archive / "events.jsonl").write_text('{"kind":"primary_capture_started","utc":10}')
        with patch("live_transcript.capture.guard.verify_inventory", return_value=1):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                lab.assess(self.root)

    def test_inherited_caption_and_job_environment_cannot_escape_lab_isolation(self):
        import os

        with patch.dict(
            os.environ,
            {
                "CAPTION_DB": "/unrelated/journal",
                "CAPTION_HOST": "foreign.example.org",
                "LT_APPLICATION_JOB": "production",
                "LIVE_TRANSCRIPT_BUNDLE": "/unrelated/source",
            },
        ):
            value = lab.profile_environment(self.root / "lab.toml")
        self.assertNotIn("CAPTION_DB", value)
        self.assertNotIn("CAPTION_HOST", value)
        self.assertNotIn("LT_APPLICATION_JOB", value)
        self.assertNotIn("LIVE_TRANSCRIPT_BUNDLE", value)
        self.assertEqual(value["LIVE_TRANSCRIPT_CONFIG"], str(self.root / "lab.toml"))

    def test_readiness_proves_listener_belongs_to_our_process(self):
        import os
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(lab.owns_listener(os.getpid(), port))
            self.assertFalse(lab.owns_listener(99999999, port))

    def test_custom_plan_cannot_read_audio_outside_its_directory(self):
        directory = self.root / "plan"
        directory.mkdir()
        plan = directory / "plan.json"
        plan.write_text(
            json.dumps(
                [
                    dict(
                        id="x",
                        language="en",
                        speaker="A",
                        text="Expected words",
                        file="../private.wav",
                    )
                ]
            )
        )
        with self.assertRaises(ValueError):
            lab.import_plan(plan, self.root)

    def test_unavailable_asr_does_not_trigger_a_meeting_join(self):
        args = SimpleNamespace(
            lab_action="jitsi",
            server="https://meet.example.org",
            output=str(self.root / "run"),
            silence_finalize=None,
            endpoint=None,
            asr_mode="existing",
            baseline=None,
        )
        with (
            patch.object(lab, "preflight_asr", return_value=dict(ok=False)),
            patch.object(lab, "synthesize") as synthesize,
        ):
            value = lab.run(args)
        self.assertFalse(value["ok"])
        self.assertEqual(value["stage"], "preflight")
        synthesize.assert_not_called()

    def test_offline_report_never_overwrites_input_or_previous_results(self):
        path = self.root / "reference.json"
        path.write_text("fixture evidence")
        args = SimpleNamespace(
            lab_action="report", reference=str(path), transcript=str(path), output=str(path)
        )
        with self.assertRaises(ValueError):
            lab.run(args)
        self.assertEqual(path.read_text(), "fixture evidence")

    def test_final_timeout_stops_only_its_owned_group(self):
        import subprocess
        from unittest.mock import Mock

        proc = Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("recognizer", 1)
        with (
            patch.object(lab.subprocess, "Popen", return_value=proc) as start,
            patch.object(lab_guest, "stop_process") as stop,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                lab.run_owned(["recognizer"], {}, self.root / "recognizer.log", 1)
        self.assertTrue(start.call_args.kwargs["start_new_session"])
        stop.assert_called_once_with(proc)

    def test_workers_exit_if_the_lab_controller_is_killed(self):
        import os
        import signal
        import subprocess
        import sys
        import time
        from live_transcript.runtime.application import stamp

        ready = self.root / "child-ready"
        child_code = (
            "import pathlib,os,time; pathlib.Path("
            + repr(str(ready))
            + ").write_text(str(os.getpid())); time.sleep(30)"
        )
        program = (
            "from live_transcript.runtime.lab import guarded_command; import subprocess,sys,time; subprocess.Popen(guarded_command([sys.executable,'-c',"
            + repr(child_code)
            + "]),start_new_session=True); time.sleep(30)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", program],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        child, ticks = None, None
        try:
            until = time.monotonic() + 6
            while not ready.exists() and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "protected worker did not start")
            child = int(ready.read_text())
            ticks = stamp(child)
            parent.kill()
            parent.wait(timeout=3)
            until = time.monotonic() + 4
            while stamp(child) == ticks and time.monotonic() < until:
                time.sleep(0.02)
            self.assertIsNone(stamp(child), "worker survived its controller")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)
            if child and ticks and stamp(child) == ticks:
                os.killpg(child, signal.SIGKILL)

    def test_recollection_cannot_turn_empty_live_output_into_success(self):
        receipt = dict(
            session="lab-fixture", phrases=[dict(id="one")], clock_offset=0, clock_uncertainty=0
        )
        (self.root / "receipt.json").write_text(json.dumps(receipt))
        guest = self.root / "guest"
        archive = guest / "rec/lab-fixture.archive"
        archive.mkdir(parents=True)
        (guest / "result.json").write_text('{"ok":true,"finalized":true}')
        (archive / "finalized.json").write_text("{}")
        (archive / "events.jsonl").write_text('{"kind":"primary_capture_started","utc":10}')
        (guest / "sender-0.jsonl").write_text(
            json.dumps(
                dict(
                    id="one", language="en", speaker="A", text="Known speech", started=11, ended=12
                )
            )
        )
        (self.root / "captions.jsonl").write_text("")
        with (
            patch("live_transcript.capture.guard.verify_inventory", return_value=1),
            patch("live_transcript.transcription.store.CaptionStore") as store,
            patch.object(
                lab,
                "canonical_audio",
                return_value=(self.root / "analysis/fixture.wav", {"gaps": []}),
            ),
        ):
            store.return_value.transcript.return_value = []
            value = lab.assess(self.root)
        self.assertTrue(value["capture_ok"])
        self.assertFalse(value["ok"])
        self.assertEqual(value["live"]["wer"]["word_error_rate"], 1)

    def test_canonical_reconstruction_retains_original_and_rejects_modified_derivatives(self):
        archive = self.root / "guest/rec/lab-fixture.archive"
        archive.mkdir(parents=True)
        original = archive.parent / "lab-fixture.wav"
        original.write_bytes(b"original audio fixture")
        (archive / "integrity.json").write_text("verified fixture inventory")

        def rebuild(root, output):
            output.write_bytes(b"reconstructed audio fixture")
            output.with_suffix(".recovery.json").write_text('{"gaps":[]}')
            return {"gaps": []}

        with patch("live_transcript.storage.recover.recover", side_effect=rebuild) as recover:
            derived, _ = lab.canonical_audio(self.root, archive, "lab-fixture")
            lab.canonical_audio(self.root, archive, "lab-fixture")
            self.assertEqual(recover.call_count, 1)
        self.assertEqual(original.read_bytes(), b"original audio fixture")
        self.assertEqual(derived.with_suffix(".monitor.wav").read_bytes(), original.read_bytes())
        derived.write_bytes(b"operator edited derivative")
        with self.assertRaises(ValueError):
            lab.canonical_audio(self.root, archive, "lab-fixture")

    def test_previous_gpu_log_cannot_certify_a_new_cpu_worker(self):
        from unittest.mock import Mock

        profile = self.root / "config.toml"
        profile.write_text(
            "[paths]\nstate=" + json.dumps(str(self.root)) + '\n[asr]\nprimary_device="gpu"\n'
        )
        log = self.root / "engine.log"
        log.write_text("whisper_backend_init_gpu: found GPU device 0\n")
        process = Mock(pid=1234)
        process.poll.return_value = None
        response = Mock()
        response.__enter__ = Mock(return_value=SimpleNamespace(status=200))
        response.__exit__ = Mock(return_value=False)
        with (
            patch.object(lab.subprocess, "Popen", return_value=process),
            patch.object(lab, "owns_listener", return_value=True),
            patch(
                "live_transcript.transcription.service.owned_engine", return_value=dict(pid=1235)
            ),
            patch.object(lab.urllib.request, "urlopen", return_value=response),
            patch.object(lab_guest, "stop_process") as stop,
        ):
            with self.assertRaisesRegex(RuntimeError, "accelerator"):
                lab.start_engine(profile, "http://127.0.0.1:9999/inference", log)
        stop.assert_called_once_with(process)

    def test_accuracy_failures_do_not_hide_remaining_matrix_conditions(self):
        args = SimpleNamespace(
            lab_action="jitsi",
            server="https://meet.example.org",
            output=str(self.root / "matrix"),
            silence_finalize=None,
            endpoint=None,
            asr_mode="existing",
            baseline=None,
            plan=None,
            scenario="all",
            no_final=True,
        )
        results = [dict(ok=False, capture_ok=True, error="recognition failure")] + [
            dict(ok=True, capture_ok=True)
        ] * 3

        def run_scenario(directory, server, scenario, *args, **kwargs):
            (directory / scenario).mkdir()
            return results.pop(0)

        with (
            patch.object(lab, "preflight_asr", return_value=dict(ok=True)),
            patch.object(lab, "synthesize", return_value=[]),
            patch("live_transcript.runtime.state.current", return_value={}),
            patch.object(lab, "run_case", side_effect=run_scenario) as run,
        ):
            value = lab.run(args)
        self.assertFalse(value["ok"])
        self.assertEqual(run.call_count, 4)

    def test_offline_report_accepts_the_final_transcript_format(self):
        reference = self.root / "reference.json"
        reference.write_text(
            json.dumps(
                [dict(id="one", language="en", speaker="A", text="Known speech", start=1, end=2)]
            )
        )
        transcript = self.root / "final-transcript.json"
        transcript.write_text(json.dumps(dict(lines=[dict(t=1, end=2, text="Known speech")])))
        args = SimpleNamespace(
            lab_action="report",
            reference=str(reference),
            transcript=str(transcript),
            output=str(self.root / "score.json"),
            baseline=None,
        )
        value = lab.run(args)
        self.assertEqual(value["wer"]["word_error_rate"], 0)
