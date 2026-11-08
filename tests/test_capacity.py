import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from collections import namedtuple

from live_transcript.core import config
from live_transcript.runtime import capacity


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        values = copy.deepcopy(config.DEFAULTS)
        values["paths"].update(root=str(self.root), state=str(self.root / "state"))
        values["storage"]["host_free_reserve_bytes"] = 100 * capacity.MIB
        values["capacity"].update(
            recording_bytes_per_minute=10 * capacity.MIB,
            guest_free_reserve_bytes=100 * capacity.MIB,
        )
        self.patch = patch.object(config, "CONFIG", values)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.usage = patch.object(
            capacity.shutil,
            "disk_usage",
            return_value=namedtuple("Usage", "total used free")(
                2000 * capacity.MIB, 1000 * capacity.MIB, 1000 * capacity.MIB
            ),
        )
        self.usage.start()
        self.addCleanup(self.usage.stop)
        self.memory = patch.object(capacity, "memory_available", return_value=4096 * capacity.MIB)
        self.memory.start()
        self.addCleanup(self.memory.stop)

    def status(self, free, filesystem=None, age=0):
        directory = self.root / "state/vm-status"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "capacity.json").write_text(
            json.dumps(
                dict(
                    checked_at=time.time() - age,
                    free_bytes=free * capacity.MIB,
                    filesystem=filesystem or str(self.root.stat().st_dev),
                    disk_allocated_bytes=100,
                )
            )
        )

    def test_shared_backing_filesystem_accounts_for_vm_and_host_copies(self):
        self.status(1000)
        value = capacity.report(40, guest_free=1000 * capacity.MIB)
        self.assertEqual(value["estimated_minutes_remaining"], 30)
        self.assertEqual(value["status"], "blocked")
        self.assertIn("VM backing", value["reasons"][-1])

    def test_separate_vm_filesystem_and_low_memory_are_checked(self):
        self.status(200, filesystem="separate")
        self.assertEqual(
            capacity.report(20, guest_free=1000 * capacity.MIB)["estimated_minutes_remaining"], 10
        )
        self.memory.stop()
        with patch.object(capacity, "memory_available", return_value=128 * capacity.MIB):
            self.assertIn("memory", capacity.report(0)["reasons"][0])

    def test_stale_metrics_and_unknown_guest_do_not_look_ready(self):
        self.status(0, age=100)
        value = capacity.report()
        self.assertIsNone(value["vm_host_free_bytes"])
        self.assertEqual(value["status"], "warning")
        self.assertEqual(len(value["warnings"]), 2)
