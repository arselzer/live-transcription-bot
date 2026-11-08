from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from live_transcript import cli
from live_transcript.core import config
from live_transcript.core.files import atomic_text
from live_transcript.presentation.artifacts import render_video, subtitles
from live_transcript.transcription import batch
from live_transcript.notes.summary import public_metadata
from live_transcript.transcription.speakers import load as load_speakers
from live_transcript.transcription.store import CaptionStore
from tools.check_public_source import audit, check_blob

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_corrupt_cache_container_is_ignored(self):
        import wave
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "meeting.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000)
            wav.with_suffix(".transcription.json").write_text("[]")
            calls = []

            def runner(command, **kwargs):
                calls.append(command)
                return MagicMock(
                    stdout="[00:00:00.00 --> 00:00:01.00] Independent decode.", stderr=""
                )

            rows, info = batch.transcribe_wav(wav, runner=runner)
            self.assertEqual(rows[0]["text"], "Independent decode.")
            self.assertFalse(info["cache_reused"])
            self.assertTrue(calls)

    def test_notes_strip_nested_credentials_and_url_userinfo(self):
        source = dict(
            meeting="https://fixture:password@example.invalid/room?pwd=secret",
            nested=dict(passcode="fixture", values=[dict(token="fixture", title="keep")]),
        )
        result = public_metadata(source)
        self.assertEqual(result["meeting"], "https://example.invalid/room")
        self.assertEqual(result["nested"], dict(values=[dict(title="keep")]))
        self.assertEqual(source["nested"]["passcode"], "fixture")

    def test_malformed_optional_speaker_observations_do_not_abort_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            speakers = root / "speakers.jsonl"
            speakers.write_text(
                'null\n{"t":0,"who":[{"role":"active","name":"Speaker A"}]}\n'
                '{"t":0,"who":[],"error":"unknown"}\n{"t":"bad"}\n'
            )
            voice = root / "voice.jsonl"
            voice.write_text('{"t":1,"duration":1,"speaker_label":"Recorder"}\n')
            speaker, _ = load_speakers(speakers, voice)
            self.assertIsNone(speaker(0.5))
            self.assertEqual(speaker(1.5), "Recorder")

    def test_shipped_example_loads_with_public_configuration_sections(self):
        settings = config.load(ROOT / "config.example.toml")
        self.assertEqual(settings["postprocess"]["chunk_seconds"], 300)
        self.assertFalse(settings["postprocess"]["diarization_enabled"])
        self.assertTrue((ROOT / "src/live_transcript/runtime/launch.sh").is_file())

    def test_status_preserves_public_admission_diagnostics_key(self):
        output = io.StringIO()
        with (
            patch.object(
                cli,
                "current",
                return_value=dict(status="finished", admission_diagnostics="/example/diagnostics"),
            ),
            redirect_stdout(output),
        ):
            cli.main(["--json", "status"])
        self.assertEqual(
            json.loads(output.getvalue())["admission_diagnostics"], "/example/diagnostics"
        )

    def test_check_checks_disk_without_creating_a_new_workspace(self):
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "new/workspace"
            original = config.get

            def setting(section, key):
                return (
                    str(workspace)
                    if (section, key) == ("paths", "root")
                    else original(section, key)
                )

            with (
                patch.object(config, "get", side_effect=setting),
                patch.object(cli, "current", return_value={"status": "idle"}),
                patch.object(cli.socket, "create_connection", side_effect=OSError("isolated")),
                patch.object(cli.urllib.request, "urlopen", side_effect=OSError("isolated")),
                patch.object(cli.subprocess, "run", return_value=MagicMock(stdout="{}")),
            ):
                report = cli.check_readiness()
            self.assertIn("host disk reserve", [row["name"] for row in report["checks"]])
            self.assertFalse(workspace.exists())

    def test_invalid_guest_root_and_timezone_are_rejected(self):
        for text in (
            '[vm]\nhome="/"',
            '[vm]\nhome="/tmp/../etc"',
            '[calendar]\ntimezone="Not/AZone"',
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                path.write_text(text)
                with self.assertRaises(ValueError):
                    config.load(path)

    def test_parser_cannot_silently_skip_malformed_timestamps_among_valid_rows(self):
        for value in (
            "[00:00:01.00 --> broken] lost speech",
            "[00:61:01.00 --> 00:61:02.00] bad timestamp",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    batch.parse_output("[00:00:00.00 --> 00:00:01.00] valid\n" + value)

    def test_ambient_home_glossary_is_not_read(self):
        original = batch.get

        def setting(section, key):
            return "" if (section, key) == ("paths", "glossary") else original(section, key)

        with (
            patch.object(batch, "get", side_effect=setting),
            patch.object(
                Path, "read_text", side_effect=AssertionError("ambient configuration read")
            ),
        ):
            self.assertEqual(batch.glossary_text(), "")


class ArtifactTests(unittest.TestCase):
    def test_malformed_audio_fails_without_cpu_retry_or_output_replacement(self):
        from live_transcript.runtime import postprocess

        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "meeting.wav"
            wav.touch()
            previous = wav.with_suffix(".txt")
            previous.write_text("Prior output")
            with self.assertRaises(EOFError):
                postprocess.main([str(wav)])
            self.assertEqual(previous.read_text(), "Prior output")
            self.assertEqual(
                json.loads(wav.with_suffix(".postprocess-status.json").read_text())["status"],
                "processing_failed",
            )

    def test_summary_failure_is_visible_and_original_audio_is_preserved(self):
        from live_transcript.runtime import postprocess

        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "meeting.wav"
            wav.write_bytes(b"original evidence")
            decoded = (
                [dict(t=0, end=1, text="Recognized words")],
                dict(languages=["en"], chunks=1, retries=[]),
            )
            with (
                patch("live_transcript.transcription.batch.transcribe_wav", return_value=decoded),
                patch(
                    "live_transcript.notes.summary.summarize",
                    side_effect=RuntimeError("model unavailable"),
                ),
            ):
                postprocess.main([str(wav)])
            report = json.loads(wav.with_suffix(".postprocess-status.json").read_text())
            self.assertEqual(report["status"], "complete_with_warnings")
            self.assertEqual(report["summary"]["status"], "failed")
            self.assertIn("Recognized words", wav.with_suffix(".txt").read_text())
            self.assertEqual(wav.read_bytes(), b"original evidence")

    def test_cache_write_error_does_not_trigger_cpu_recognition(self):
        from live_transcript.runtime import postprocess

        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "meeting.wav"
            wav.write_bytes(b"original evidence")
            decoded = (
                [dict(t=0, end=1, text="Recognized words")],
                dict(languages=["en"], chunks=1, retries=[]),
            )
            with (
                patch("live_transcript.transcription.batch.transcribe_wav", return_value=decoded),
                patch(
                    "live_transcript.capture.guard.atomic_json",
                    side_effect=OSError("storage unavailable"),
                ),
            ):
                with self.assertRaises(OSError):
                    postprocess.main([str(wav)])
            report = json.loads(wav.with_suffix(".postprocess-status.json").read_text())
            self.assertEqual(report["status"], "processing_failed")
            self.assertEqual(wav.read_bytes(), b"original evidence")

    def test_optional_vault_failure_preserves_finished_notes_and_reports_warning(self):
        from live_transcript.runtime import postprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / "meeting.wav"
            wav.write_bytes(b"original evidence")

            def completed(_):
                wav.with_suffix(".md").write_text("Finished notes")
                postprocess.publish_status(
                    str(wav), dict(status="complete", artifacts={"notes": "fixture"})
                )

            settings = {"logs": str(root / "logs"), "vault": str(root / "unavailable-vault")}
            with (
                patch.object(postprocess, "main", side_effect=completed) as process,
                patch.object(postprocess, "get", side_effect=lambda section, key: settings[key]),
                patch("builtins.print"),
            ):
                postprocess.run_logged([str(wav)])
            process.assert_called_once()
            state = json.loads(wav.with_suffix(".postprocess-status.json").read_text())
            self.assertEqual(state["status"], "complete_with_warnings")
            self.assertEqual(state["vault"]["status"], "failed")
            self.assertEqual(state["artifacts"], {"notes": "fixture"})
            self.assertEqual(wav.with_suffix(".md").read_text(), "Finished notes")
            self.assertEqual(wav.read_bytes(), b"original evidence")
            self.assertEqual((root / "logs/post-meeting.log").stat().st_mode & 0o777, 0o600)

    def test_processing_log_refuses_symlinks_before_starting_work(self):
        from live_transcript.runtime import postprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "unrelated.txt"
            target.write_text("Preserved")
            (root / "post-meeting.log").symlink_to(target)
            with (
                patch.object(postprocess, "get", return_value=directory),
                patch.object(postprocess, "main") as process,
            ):
                with self.assertRaises(OSError):
                    postprocess.run_logged([str(root / "meeting.wav")])
            process.assert_not_called()
            self.assertEqual(target.read_text(), "Preserved")

    def test_atomic_write_preserves_old_file_and_cleans_failed_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "notes.md"
            target.write_text("old")
            with patch("live_transcript.core.files.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    atomic_text(target, "new")
            self.assertEqual(target.read_text(), "old")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_failed_encode_keeps_existing_media(self):
        with tempfile.TemporaryDirectory() as directory:
            stem = Path(directory) / "meeting's unusual:path"
            wav = stem.with_suffix(".wav")
            wav.touch()
            screen = Path(str(stem) + "-screen.mp4")
            screen.write_bytes(b"x" * 10001)
            mkv = Path(str(stem) + ".mkv")
            mkv.write_bytes(b"old video")

            def fail(*args, **kwargs):
                raise subprocess.TimeoutExpired("ffmpeg", 600)

            with self.assertRaises(subprocess.TimeoutExpired):
                render_video(wav, Path(directory) / "captions.srt", runner=fail)
            self.assertEqual(mkv.read_bytes(), b"old video")

    def test_zero_exit_without_video_data_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            stem = Path(directory) / "meeting"
            wav = stem.with_suffix(".wav")
            wav.touch()
            Path(str(stem) + "-screen.mp4").write_bytes(b"x" * 10001)
            with self.assertRaises(ValueError):
                render_video(wav, Path(directory) / "captions.srt", runner=lambda *a, **kw: None)

    def test_subtitles_use_explicit_voice_label_consistently(self):
        text = subtitles(
            [dict(t=0, end=1, text="Speech", voice_name="Speaker A")], lambda at: "Speaker B"
        )
        self.assertIn("Speaker A: Speech", text)
        self.assertNotIn("Speaker B", text)


class PrivacyBoundaryTests(unittest.TestCase):
    def test_sdist_hook_publishes_only_normalized_archive(self):
        import build_backend
        import tarfile

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "dist"

            def delegate(operation, output, settings):
                self.assertEqual(operation, "build_sdist")
                filename = "fixture-1.0.tar.gz"
                with tarfile.open(Path(output) / filename, "w:gz") as archive:
                    entry = tarfile.TarInfo("fixture/source.py")
                    entry.size = 1
                    entry.uname = entry.gname = "fixture-account"
                    entry.uid = entry.gid = 1234
                    archive.addfile(entry, io.BytesIO(b"x"))
                return filename

            with patch.object(build_backend, "_delegate", side_effect=delegate):
                filename = build_backend.build_sdist(destination)
            with tarfile.open(destination / filename) as archive:
                entry = archive.getmembers()[0]
                self.assertEqual((entry.uid, entry.gid, entry.uname, entry.gname), (0, 0, "", ""))

    def test_source_archive_ownership_and_timestamps_are_normalized(self):
        import build_backend
        import tarfile

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                entry = tarfile.TarInfo("project/source.py")
                entry.uid = entry.gid = 1234
                entry.uname = entry.gname = "fixture-account"
                entry.mtime = 123456
                entry.size = 4
                output.addfile(entry, io.BytesIO(b"data"))
            build_backend._normalize_sdist(archive)
            with tarfile.open(archive) as source:
                entry = source.getmembers()[0]
                self.assertEqual(
                    (entry.uid, entry.gid, entry.uname, entry.gname, entry.mtime), (0, 0, "", "", 0)
                )
                self.assertEqual(source.extractfile(entry).read(), b"data")

    def test_wheel_build_uses_only_reviewed_sources_despite_stale_build_output(self):
        import build_backend

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src/live_transcript"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("VALUE=1\n")
            (root / "PUBLIC_SOURCE_FILES.txt").write_text(
                "PUBLIC_SOURCE_FILES.txt\nsrc/live_transcript/__init__.py\n"
            )
            stale = root / "build/lib/live_transcript/retired.py"
            stale.parent.mkdir(parents=True)
            stale.write_text("retired helper\n")
            previous = Path.cwd()

            def build(operation, output, settings, metadata):
                self.assertEqual(operation, "build_wheel")
                self.assertNotEqual(Path.cwd(), root)
                self.assertEqual(
                    (Path.cwd() / "src/live_transcript/__init__.py").read_text(), "VALUE=1\n"
                )
                self.assertFalse((Path.cwd() / "build/lib/live_transcript/retired.py").exists())
                self.assertTrue(Path(output).is_absolute())
                return "fixture.whl"

            with (
                patch.object(build_backend, "_validated_root", return_value=root),
                patch.object(build_backend, "_delegate", side_effect=build),
            ):
                self.assertEqual(build_backend.build_wheel(root / "dist"), "fixture.whl")
            self.assertEqual(Path.cwd(), previous)
            self.assertTrue(stale.exists())

    def test_sdist_metadata_exemption_rejects_arbitrary_setup_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PUBLIC_SOURCE_FILES.txt").write_text(
                "PUBLIC_SOURCE_FILES.txt\npyproject.toml\n"
            )
            (root / "pyproject.toml").write_text(
                '[project]\nname="fixture-package"\nversion="1.0"\n'
            )
            (root / "PKG-INFO").write_text(
                "Metadata-Version: 2.4\nName: fixture-package\nVersion: 1.0\n"
            )
            (root / "setup.cfg").write_text("[egg_info]\ntag_build =\ntag_date = 0\n")
            self.assertFalse(audit(root)["ok"])
            self.assertTrue(audit(root, allow_build_metadata=True)["ok"])
            (root / "setup.cfg").write_text("[options]\npackages=find:\n")
            self.assertFalse(audit(root, allow_build_metadata=True)["ok"])

    def test_caption_database_has_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            path.touch()
            path.chmod(0o644)
            store = CaptionStore(path)
            try:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                store.close()

    def test_caption_database_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "other"
            target.write_text("original")
            database = root / "journal.sqlite3"
            database.symlink_to(target)
            with self.assertRaises(OSError):
                CaptionStore(database)
            self.assertEqual(target.read_text(), "original")

    def test_reset_followed_by_large_new_checkpoint_retains_every_new_row(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CaptionStore(Path(directory) / "journal.sqlite3")
            try:
                store.checkpoint(
                    "session", dict(lines=[(i, "Old", "Old words") for i in range(40)])
                )
                store.reset("session")
                new = [(i, "New", "New words") for i in range(30)]
                store.checkpoint("session", dict(lines=new))
                self.assertEqual(store.transcript("session"), new)
            finally:
                store.close()

    def test_unreviewed_files_block_package_build_before_setuptools_runs(self):
        import build_backend

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PUBLIC_SOURCE_FILES.txt").write_text("PUBLIC_SOURCE_FILES.txt\n")
            (root / "unexpected.py").write_text("unreviewed")
            with patch.object(build_backend, "__file__", str(root / "build_backend.py")):
                with self.assertRaises(RuntimeError):
                    build_backend.build_wheel(str(root / "dist"))

    def test_private_file_names_are_not_echoed_in_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PUBLIC_SOURCE_FILES.txt").write_text("PUBLIC_SOURCE_FILES.txt\n")
            private = "synthetic-private-name"
            (root / (private + ".py")).write_text("unreviewed")
            result = audit(root, [private])
            self.assertFalse(result["ok"])
            self.assertNotIn(private, json.dumps(result))

    def test_parent_traversal_in_manifest_is_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PUBLIC_SOURCE_FILES.txt").write_text("PUBLIC_SOURCE_FILES.txt\n../outside\n")
            self.assertFalse(audit(root)["ok"])

    def test_symlinked_parent_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "source.py").write_text("value=1")
            (root / "src").symlink_to(outside, target_is_directory=True)
            (root / "PUBLIC_SOURCE_FILES.txt").write_text(
                "PUBLIC_SOURCE_FILES.txt\nsrc/source.py\n"
            )
            self.assertFalse(audit(root)["ok"])

    def test_unlisted_source_is_reported_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "PUBLIC_SOURCE_FILES.txt").write_text("PUBLIC_SOURCE_FILES.txt\n")
            (root / "unexpected.py").write_text("not reviewed")
            result = audit(root)
            self.assertIn("outside-source-allowlist", {f["category"] for f in result["findings"]})

    def test_adjacent_literal_lines_cannot_hide_private_identifier(self):
        private = "synthetic-private-identifier"
        payload = b'value = ("synthetic-private-"\n"identifier")'
        self.assertIn(
            "private-policy-match",
            {f["category"] for f in check_blob("source.py", payload, [private])},
        )
