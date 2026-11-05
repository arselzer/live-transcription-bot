import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from live_transcript.storage.layout import prepare_link, maintenance_active


class StorageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_join_path_validates_link_without_touching_nas(self):
        source = self.root / "recordings"
        destination = Path("/unavailable-test-nas/recordings")
        source.symlink_to(destination)
        with patch(
            "live_transcript.storage.layout.os.stat",
            side_effect=AssertionError("must not stat remote storage"),
        ):
            # Path.exists for the local policy uses pathlib's stat, so supply a
            # local policy explicitly and leave the remote destination untouched.
            with patch("pathlib.Path.exists", return_value=False):
                prepare_link(source, destination, self.root / "no-policy.json")
        self.assertEqual(os.readlink(source), str(destination))

    def test_initial_link_can_be_created_before_archive_mount_is_available(self):
        source = self.root / "recordings"
        prepare_link(source, self.root / "offline/recordings", self.root / "no-policy.json")
        self.assertTrue(source.is_symlink())
        self.assertFalse((self.root / "offline").exists())

    def test_existing_recordings_are_never_overwritten(self):
        source = self.root / "recordings"
        source.mkdir()
        (source / "audio.wav").write_bytes(b"original")
        with self.assertRaises(ValueError):
            prepare_link(source, self.root / "archive")
        self.assertEqual((source / "audio.wav").read_bytes(), b"original")

    def test_local_disk_reserve_is_enforced_before_starting_another_meeting(self):
        policy = self.root / "policy.json"
        policy.write_text('{"host_free_reserve_bytes":1000}')
        with patch(
            "live_transcript.storage.layout.shutil.disk_usage",
            return_value=SimpleNamespace(free=999),
        ):
            with self.assertRaises(RuntimeError):
                prepare_link(self.root / "recordings", self.root / "archive", policy)

    def test_maintenance_marker_requires_live_matching_owner_and_deadline(self):
        path = self.root / "maintenance.json"
        ticks = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
        data = dict(active=True, pid=os.getpid(), start_ticks=ticks, deadline=time.time() + 60)
        path.write_text(json.dumps(data))
        self.assertTrue(maintenance_active(path))
        data["deadline"] = 1
        path.write_text(json.dumps(data))
        self.assertFalse(maintenance_active(path))
        data.update(deadline=time.time() + 60, start_ticks="wrong")
        path.write_text(json.dumps(data))
        self.assertFalse(maintenance_active(path))


if __name__ == "__main__":
    unittest.main()
