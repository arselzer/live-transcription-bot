"""Exercise durable scheduler ownership and recovery with real subprocesses."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from live_transcript.core import config
from live_transcript.runtime import application as app


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        profile = self.root / "config.toml"
        profile.write_text(
            "[paths]\nroot = " + json.dumps(str(self.root / "data")) + "\n"
            '[vm]\nmanage_libvirt = false\nssh = "transcript@192.0.2.20"\n'
            '[services]\ncaptions=""\nprimary=""\ncpu_large=""\ncpu_small=""\n'
            '[runtime]\nsupervisor="app"\nwatchdog_seconds=0\ncalendar_seconds=0\n'
        )
        self.patch = patch.object(config, "CONFIG", config.load(profile))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = dict(os.environ, LIVE_TRANSCRIPT_CONFIG=str(profile))
        self.processes = []
        self.queue = app.Queue()
        self.addCleanup(self.queue.close)
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        for row in self.queue.active():
            if row.get("pid") != os.getpid():
                app.terminate(row, force=True)

    def wait(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("application state did not converge")

    def start(self):
        process = subprocess.Popen(
            [sys.executable, "-m", "live_transcript.worker", "app"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        self.wait(app.ready)
        return process

    def test_cancellation_is_requested_without_signalling_foreign_namespaces(self):
        row = self.queue.submit("process", [str(self.root / "fixture.wav")], key="cancel-fixture")
        row.update(status="running", namespace="another-namespace", pid=os.getpid())
        with self.queue.db:
            self.queue.save(row)
        with patch.object(app, "terminate") as terminate:
            app.request_cancel(row["id"])
            first = self.queue.row(row["id"])
            app.request_cancel(row["id"])
            self.assertEqual(self.queue.row(row["id"])["cancel_at"], first["cancel_at"])
            terminate.assert_not_called()
        app.Queue.finish(self.queue, row, 1, interrupted=True)
        self.assertEqual(self.queue.row(row["id"])["status"], "cancelled")
        self.assertTrue(app.cancellation_file(self.root / "fixture.wav").is_file())

    def test_recording_and_backup_workers_are_not_cancellable_from_the_workspace(self):
        row = self.queue.submit("mirror", [], key="protected-backup")
        with self.assertRaises(ValueError):
            app.request_cancel(row["id"])
        self.assertEqual(self.queue.row(row["id"])["status"], "queued")

    def test_expired_meeting_is_not_started_and_nonfinite_deadlines_are_rejected(self):
        with self.assertRaises(ValueError):
            self.queue.submit("launch", [], env={"LT_DEADLINE": "nan"})
        row = self.queue.submit("launch", ["fixture"], env={"LT_DEADLINE": str(time.time() - 1)})
        self.start()
        self.wait(lambda: self.queue.row(row["id"])["status"] == "expired")
        self.assertFalse((app.directory() / (row["id"] + ".log")).exists())

    def test_shared_heartbeat_does_not_signal_or_resolve_foreign_pids(self):
        from live_transcript.core.files import atomic_json

        atomic_json(
            app.directory() / "health.json",
            dict(utc=time.time(), identity=app.identity(), pid=999999, namespace="foreign"),
        )
        self.assertFalse(app.ready())
        self.assertTrue(app.heartbeat_ready())

    def test_durable_completion_idempotency_and_scheduler_lock(self):
        self.start()
        first = app.submit("watchdog", ["--dry-run", "--json"], key="readiness-check")
        complete = self.wait(
            lambda: (r := self.queue.row(first["id"]))["status"] == "complete" and r
        )
        again = app.submit("watchdog", ["--dry-run", "--json"], key="readiness-check")
        self.assertEqual(again["attempts"], complete["attempts"])
        self.assertEqual(again["status"], "complete")
        duplicate = subprocess.run(
            [sys.executable, "-m", "live_transcript.worker", "app"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("already running", duplicate.stderr)
        self.assertTrue(app.ready())

    def test_scheduler_restart_adopts_wrapper_and_shutdown_preserves_retry(self):
        scheduler = self.start()
        row = app.submit(
            "mirror",
            ["--vm", "transcript@192.0.2.20", "--join-log", str(self.root / "absent.log")],
            key="mirror-fixture",
        )
        running = self.wait(lambda: (r := self.queue.row(row["id"])).get("child_pid") and r)
        scheduler.kill()
        scheduler.wait(timeout=5)
        self.assertTrue(app.alive(running))
        replacement = self.start()
        time.sleep(0.7)
        adopted = self.queue.row(row["id"])
        self.assertEqual(adopted["pid"], running["pid"])
        self.assertEqual(adopted["attempts"], 1)
        replacement.terminate()
        replacement.wait(timeout=25)
        self.assertFalse(app.alive(running))
        self.assertFalse(app.alive(running, "child_"))
        self.assertEqual(self.queue.row(row["id"])["status"], "queued")

    def test_stale_launch_is_never_replayed_but_backup_is_retryable(self):
        row = self.queue.submit(
            "launch", ["https://meet.google.com/abc-abcd-efg"], key="launch-fixture"
        )
        row.update(
            status="running",
            nonce="fixture",
            attempts=1,
            pid=99999999,
            ticks="stale",
            namespace=app.namespace(),
        )
        with self.queue.db:
            self.queue.save(row)
        self.start()
        ended = self.wait(lambda: (r := self.queue.row(row["id"]))["status"] == "interrupted" and r)
        self.assertEqual(ended["attempts"], 1)
        with self.assertRaises(ValueError):
            self.queue.submit("launch", ["different"], key="launch-fixture")

    def test_job_inputs_and_process_namespace_are_bounded(self):
        for kind, args, env in (
            ("shell", ["rm"], {}),
            ("process", ["a\0b"], {}),
            ("process", [], {"PATH": "wrong"}),
        ):
            with self.assertRaises(ValueError):
                self.queue.submit(kind, args, env=env)
        self.assertFalse(
            app.alive(
                dict(pid=os.getpid(), ticks=app.stamp(os.getpid()), namespace="another-namespace")
            )
        )
        row = self.queue.submit(
            "process", ["fixture.wav"], key="cancel-fixture", due=time.time() + 60
        )
        app.cancel(row["id"])
        self.assertEqual(self.queue.row(row["id"])["status"], "cancelled")

    def test_console_worker_invokes_the_installed_cli_entrypoint(self):
        result = subprocess.run(
            app.command(dict(kind="console", arguments=["--help"])),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--advertise-host", result.stdout)

    def test_recovery_worker_uses_the_cli_result_contract_and_pinned_vm(self):
        from contextlib import redirect_stdout
        import io
        from live_transcript.cli import main

        row = dict(kind="recover", arguments=[config.get("vm", "ssh"), "fixture.wav", "--help"])
        result = subprocess.run(
            app.command(row), env=self.env, capture_output=True, text=True, timeout=5
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("guest_wav", result.stdout)
        row["arguments"][0] = "other@192.0.2.21"
        with self.assertRaises(ValueError):
            app.command(row)
        with (
            patch.dict(os.environ, dict(self.env, LT_APPLICATION_JOB="fixture")),
            patch(
                "live_transcript.storage.worker.run", return_value=dict(status="postprocess_failed")
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "--config",
                        self.env["LIVE_TRANSCRIPT_CONFIG"],
                        "--json",
                        "recover",
                        "owned-fixture",
                        "fixture.wav",
                    ]
                ),
                1,
            )

    def test_calendar_restart_adopts_committed_launch_without_rejoining(self):
        from live_transcript.integrations import calendar_dispatch, calendar

        path = self.root / "calendar.json"
        path.write_text(json.dumps(dict(status="dispatching", start=time.time())))
        logs = Path(config.get("paths", "logs"))
        logs.mkdir(parents=True)
        (logs / "current_meeting.json").write_text(
            json.dumps(dict(calendar_job=str(path), run_id="owned-fixture"))
        )
        with patch.object(
            calendar, "fetch_ics", side_effect=AssertionError("must not fetch or rejoin")
        ):
            calendar_dispatch.dispatch(path)
        self.assertEqual(json.loads(path.read_text())["status"], "started")

    def test_abandoned_claim_does_not_overwrite_a_live_wrapper(self):
        row = self.queue.submit("watchdog", ["--dry-run"], key="claim-fixture")
        row.update(
            nonce="fixture",
            status="running",
            pid=os.getpid(),
            ticks=app.stamp(os.getpid()),
            namespace=app.namespace(),
        )
        with self.queue.db:
            self.queue.save(row)

        self.queue.finish(row, 1, interrupted=True, abandoned=True)
        self.assertEqual(self.queue.row(row["id"])["status"], "running")
        row["status"] = "complete"
        with self.queue.db:
            self.queue.save(row)

    def test_watchdog_recovery_attempts_have_distinct_durable_job_ids(self):
        from types import SimpleNamespace
        from live_transcript.runtime import watchdog

        meeting = dict(run_id="owned-fixture", pid=123, url="https://meet.google.com/abc-abcd-efg")
        path = self.root / "current.json"
        path.write_text(json.dumps(meeting))
        state = {}
        with (
            patch.object(watchdog, "MJSON", path),
            patch.object(
                watchdog, "recovery_options", return_value=(["--max-min", "60"], time.time() + 3600)
            ),
            patch.object(watchdog, "pending_alive", return_value=False),
            patch.object(watchdog.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
            patch.object(
                app, "submit", side_effect=lambda kind, args, **kw: dict(id=kw["key"])
            ) as enqueue,
        ):
            self.assertTrue(watchdog.rejoin(meeting, state, 1000))
            self.assertTrue(watchdog.rejoin(meeting, state, 1100))
        self.assertEqual(
            [call.kwargs["key"] for call in enqueue.call_args_list],
            ["rejoin-owned-fixture-1", "rejoin-owned-fixture-2"],
        )


if __name__ == "__main__":
    unittest.main()
