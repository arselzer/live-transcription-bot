"""Exercise the actual shell launcher with isolated filesystem/network stubs."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lt path with spaces ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "logs").mkdir()
        source = Path(__file__).resolve().parents[1]
        shutil.copytree(
            source / "src", self.root / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
        shutil.copy2(source / "PUBLIC_SOURCE_FILES.txt", self.root / "PUBLIC_SOURCE_FILES.txt")
        self.config = self.root / "config.toml"
        self.config.write_text(
            "[paths]\nroot = "
            + json.dumps(str(self.root))
            + '\n[vm]\nssh = "transcript@192.0.2.20"\nip = "192.0.2.20"\n'
        )
        self.stub = self.bin / "stub"
        self.stub.write_text("""#!/usr/bin/python3
import json,os,sys,subprocess
from pathlib import Path
root=Path(os.environ['TEST_ROOT']);name=Path(sys.argv[0]).name;mode=os.environ['TEST_MODE']
with open(root/'calls.jsonl','a') as f:f.write(json.dumps([name,*sys.argv[1:]])+'\\n')
if name=='sudo':
 if 'domifaddr' in sys.argv:print('vnet0 52:54:00 ipv4 192.0.2.20/24')
elif name=='ssh':
 command=sys.argv[-1]
 if 'list-units' in command and mode=='busy':raise SystemExit(3)
 if 'list-units' in command and mode=='probe-failed':
  raise SystemExit(subprocess.run(['bash','-c',command]).returncode)
 if 'exec ' in command and 'live-transcript.pyz' in command and ' session ' in command:
  if mode=='automation-blocked':
   print('ADMISSION_FAILED '+json.dumps(dict(reason='zoom_automation_blocked',automatic_retry=False)))
   print('ADMISSION_DIAGNOSTICS to /home/transcript/rec/fixture.admission')
   raise SystemExit(4)
  if mode=='fallback-crash':
   print('ADMISSION_FAILED '+json.dumps(dict(reason='zoom_automation_blocked')))
   print('ADMISSION_SUCCEEDED '+json.dumps(dict(name='Example User')))
   print('ARCHIVE to /home/transcript/rec/fixture.archive\\nRECORDING to /home/transcript/rec/fixture.wav')
   raise SystemExit(1)
  counter=root/'connects';n=int(counter.read_text()) if counter.exists() else 0;counter.write_text(str(n+1))
  if mode=='reconnect' and n==0:raise SystemExit(255)
  if mode=='pull-failed':print('ARCHIVE to /home/transcript/rec/fixture.archive\\nDONE /home/transcript/rec/fixture.wav')
  else:print('DRY: inspected initial page; no join buttons clicked')
elif name=='nc':
 sys.stdin.read()
 if mode=='backend-down':raise SystemExit(1)
elif name=='systemctl' and mode in ('backend-down','probe-failed'):raise SystemExit(1)
""")
        self.stub.chmod(0o755)
        for name in ("sudo", "ssh", "scp", "systemctl", "systemd-run", "nc", "sleep"):
            (self.bin / name).symlink_to(self.stub)

    def run_launcher(
        self, mode="dry", args=("--dry",), config_from_xdg=False, relative_config=False
    ):
        env = dict(
            os.environ,
            PATH=str(self.bin) + ":" + os.environ["PATH"],
            TEST_ROOT=str(self.root),
            TEST_MODE=mode,
            LIVE_TRANSCRIPT_CONFIG=str(self.config),
            LIVE_TRANSCRIPT_PYTHON=__import__("sys").executable,
            PYTHONPATH=str(self.root / "src"),
        )
        if relative_config:
            env["LIVE_TRANSCRIPT_CONFIG"] = os.path.relpath(self.config)
        if config_from_xdg:
            selected = self.root / "config/live-transcript/config.toml"
            selected.parent.mkdir(parents=True)
            shutil.copy2(self.config, selected)
            env.pop("LIVE_TRANSCRIPT_CONFIG", None)
            env["XDG_CONFIG_HOME"] = str(self.root / "config")
        for name in ("LT_EXPECT_RUN_ID", "LT_DEADLINE", "LT_WAIT_FOR_LOCK"):
            env.pop(name, None)
        return subprocess.run(
            [
                "bash",
                str(self.root / "src/live_transcript/runtime/launch.sh"),
                "12345678901",
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_xdg_profile_is_retained_for_background_jobs(self):
        result = self.run_launcher(config_from_xdg=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = (self.root / "calls.jsonl").read_text()
        self.assertIn(
            "--setenv=LIVE_TRANSCRIPT_CONFIG="
            + str(self.root / "config/live-transcript/config.toml"),
            calls,
        )

    def test_relative_profile_is_absolute_for_detached_jobs(self):
        result = self.run_launcher(relative_config=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "--setenv=LIVE_TRANSCRIPT_CONFIG=" + str(self.config),
            (self.root / "calls.jsonl").read_text(),
        )

    def test_busy_vm_cannot_clear_state_or_prepare_audio(self):
        path = self.root / "logs/current_meeting.json"
        path.write_text('{"owner":"existing"}')
        result = self.run_launcher("busy")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(path.read_text()), {"owner": "existing"})
        self.assertNotIn(" prepare ", (self.root / "calls.jsonl").read_text())

    def test_failed_ownership_probe_never_deploys_or_changes_run_state(self):
        result = self.run_launcher("probe-failed")
        self.assertEqual(result.returncode, 3, result.stderr)
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(call[0] in ("scp", "systemd-run") for call in calls))
        self.assertFalse((self.root / "logs/current_meeting.json").exists())

    def test_provider_automation_denial_is_terminal_and_preserves_diagnostics(self):
        result = self.run_launcher("automation-blocked", ())
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.root / "logs/current_meeting.json").read_text())
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["result"], "zoom_automation_blocked")
        self.assertEqual(state["remote_exit"], 4)
        self.assertIn("fixture.admission", (self.root / "calls.jsonl").read_text())
        self.assertNotIn("recover", (self.root / "calls.jsonl").read_text())

    def test_dry_run_finishes_without_postprocess_or_rejoin_state(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.root / "logs/current_meeting.json").read_text())
        self.assertTrue(state["dry_run"])
        self.assertEqual(state["status"], "finished")

    def test_old_denial_cannot_hide_a_crash_after_successful_name_fallback(self):
        result = self.run_launcher("fallback-crash", ())
        self.assertEqual(result.returncode, 1)
        state = json.loads((self.root / "logs/current_meeting.json").read_text())
        self.assertEqual(state["status"], "control_lost")
        self.assertNotEqual(state.get("result"), "zoom_automation_blocked")

    def test_ssh_loss_reconnects_same_session_token(self):
        result = self.run_launcher("reconnect")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(x) for x in (self.root / "calls.jsonl").read_text().splitlines()]
        starts = [
            x[-1]
            for x in calls
            if x[0] == "ssh"
            and "exec " in x[-1]
            and "live-transcript.pyz" in x[-1]
            and " session " in x[-1]
        ]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0], starts[1])

    def test_archive_transfer_is_always_async_and_releases_finished_meeting(self):
        result = self.run_launcher("pull-failed", ())
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.root / "logs/current_meeting.json").read_text())
        self.assertEqual(state["status"], "finished")
        self.assertIn("recover", (self.root / "calls.jsonl").read_text())
        self.assertNotIn(" pull ", (self.root / "calls.jsonl").read_text())

    def test_caption_backend_outage_does_not_prevent_browser_launch(self):
        result = self.run_launcher("backend-down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "connects").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
