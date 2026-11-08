import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.check_public_source import audit, check_blob


class PrivacyTests(unittest.TestCase):
    def test_only_exact_reviewed_demo_bytes_are_allowed_as_binary(self):
        root = Path(__file__).resolve().parents[1]
        name = "docs/assets/workspace-demo.gif"
        data = (root / name).read_bytes()
        self.assertEqual(check_blob(name, data), [])
        self.assertEqual(check_blob(name, data + b"extra")[0]["category"], "unreviewed-media")
        self.assertEqual(check_blob("docs/other.gif", data)[0]["category"], "binary-content")

    def test_findings_never_echo_the_sensitive_value(self):
        private = "fixture-" + "private-identifier"
        findings = check_blob("source.py", ("value = " + repr(private)).encode(), [private])
        self.assertEqual(findings[0]["category"], "private-policy-match")
        self.assertNotIn(private, json.dumps(findings))

    def test_private_home_and_network_values_are_flagged(self):
        payload = ("/" + "home/private-person/project" + "\n" + "192." + "168.50.4").encode()
        categories = {f["category"] for f in check_blob("source.py", payload)}
        self.assertEqual(categories, {"private-home-path", "private-network-address"})

    def test_documentation_addresses_and_generic_guest_home_are_allowed(self):
        self.assertEqual(check_blob("example.toml", b"192.0.2.20 /home/transcript/project"), [])

    def test_staged_scan_reads_index_even_if_working_copy_is_scrubbed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(root), *args], check=True, capture_output=True
                )

            git("init", "--quiet")
            (root / "PUBLIC_SOURCE_FILES.txt").write_text("PUBLIC_SOURCE_FILES.txt\nsource.py\n")
            forbidden = "fixture-" + "private-identifier"
            (root / "source.py").write_text(forbidden)
            git("add", "PUBLIC_SOURCE_FILES.txt", "source.py")
            (root / "source.py").write_text("sanitized")
            self.assertTrue(audit(root, [forbidden])["ok"])
            result = audit(root, [forbidden], staged=True)
            self.assertFalse(result["ok"])
            self.assertIn("private-policy-match", {f["category"] for f in result["findings"]})

    def test_staged_files_outside_allowlist_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / "PUBLIC_SOURCE_FILES.txt").write_text("PUBLIC_SOURCE_FILES.txt\n")
            (root / "local-settings.toml").write_text("fixture")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            result = audit(root, staged=True)
            self.assertIn("outside-source-allowlist", {f["category"] for f in result["findings"]})
