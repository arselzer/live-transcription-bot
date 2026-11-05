"""Check selected source, unexpected files and exact staged blobs for private data.

Deployment-specific forbidden strings live in an external JSON policy. Findings
report categories and locations without echoing sensitive matched values.
"""

import argparse
import ast
import configparser
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
# The checker must also work before installation and during isolated builds.
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from live_transcript.core.source_files import (
    MANIFEST,
    MAX_SOURCE_BYTES,
    approved_paths,
    read_source,
    validate_name,
)

PATTERNS = {
    "private-home-path": re.compile(
        r"/(?:home|Users)/(?!transcript(?:/|[\s\x22\x27])|example(?:/|[\s\x22\x27]))[^/\s\x22\x27]+"
    ),
    "private-network-address": re.compile(
        r"\b(?:192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b"
    ),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "access-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"
    ),
    "url-credentials": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@([^/\s\x22\x27]+)"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
IGNORED_ROOTS = {
    ".git",
    ".venv",
    ".research-venv",
    ".ruff_cache",
    ".pytest_cache",
    "build",
    "dist",
    "logs",
    "state",
    "recordings",
    "safety-copies",
    "models",
    "tts",
    "backups",
    "previews",
    "private",
    "data",
    "benchmarks",
    "reports",
    "htmlcov",
}


def load_policy(path):
    if path is None:
        return ()
    data = json.loads(Path(path).read_text())
    values = data.get("forbidden_strings") if isinstance(data, dict) else None
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError("Invalid private policy schema")
    return tuple(values)


def check_blob(name, data, forbidden=()):
    display = name
    if any(value.casefold() in name.casefold() for value in forbidden):
        display = "[redacted-name-" + hashlib.sha256(name.encode()).hexdigest()[:12] + "]"

    def finding(category, line=0):
        return dict(file=display, line=line, category=category)

    try:
        validate_name(name)
    except ValueError:
        return [finding("unsafe-path")]
    if len(data) > MAX_SOURCE_BYTES:
        return [finding("oversized-content")]
    if b"\0" in data:
        return [finding("binary-content")]
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return [finding("non-utf8-content")]
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            match = pattern.search(line)
            if match:
                if category == "url-credentials" and (
                    match[1] in ("example.com", "example.org", "example.net")
                    or match[1].endswith((".invalid", ".example", ".test"))
                ):
                    continue
                findings.append(finding(category, number))
        if any(value.casefold() in line.casefold() for value in forbidden):
            findings.append(finding("private-policy-match", number))
        for match in EMAIL.finditer(line):
            domain = match[1].casefold()
            if domain not in ("example.com", "example.org", "example.net") and not domain.endswith(
                (".invalid", ".example", ".test")
            ):
                findings.append(finding("email-address", number))
    if display != name:
        findings.append(finding("private-file-name"))
    if name.endswith(".py") and forbidden:
        # Python joins adjacent string literals across line breaks. Inspect the
        # joined value as well so formatting cannot hide a private identifier.
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if any(value.casefold() in node.value.casefold() for value in forbidden):
                        item = finding("private-policy-match", node.lineno)
                        if item not in findings:
                            findings.append(item)
        except SyntaxError:
            findings.append(finding("invalid-python"))
    return findings


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=check,
    )


def _additional_files(root, allowed):
    found = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        directories[:] = [
            d
            for d in directories
            if d != "__pycache__"
            and not d.endswith(".egg-info")
            and not (relative == Path(".") and d in IGNORED_ROOTS)
        ]
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = (relative / name).as_posix()
            if path not in allowed:
                found.add(path)
    # CI must also see ignored-but-tracked material. No Git repository is needed
    # for the ordinary pre-repository source audit.
    try:
        probe = _git(root, "rev-parse", "--show-toplevel", check=False)
    except FileNotFoundError:
        return sorted(found)
    if probe.returncode == 0 and Path(probe.stdout.decode().strip()).resolve() == root.resolve():
        for name in _git(root, "ls-files", "-z").stdout.split(b"\0"):
            if name:
                decoded = name.decode("utf-8")
                if decoded not in allowed:
                    found.add(decoded)
    return sorted(found)


def _build_metadata(root, name, data):
    """Recognize only the two benign files generated by setuptools sdists."""
    try:
        if name == "setup.cfg":
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(data.decode())
            return parser.sections() == ["egg_info"] and dict(parser["egg_info"]) == {
                "tag_build": "",
                "tag_date": "0",
            }
        if name == "PKG-INFO":
            project = tomllib.loads(read_source(root, "pyproject.toml").decode())["project"]
            metadata = BytesParser().parsebytes(data)
            return (
                metadata["Metadata-Version"] is not None
                and metadata["Name"] == project["name"]
                and metadata["Version"] == project["version"]
            )
    except (OSError, ValueError, KeyError, configparser.Error):
        return False
    return False


def audit(root, forbidden=(), staged=False, allow_build_metadata=False):
    root = Path(root).resolve()
    findings = []
    try:
        allowed = set(approved_paths(root))
    except (OSError, ValueError):
        return dict(
            ok=False,
            files_checked=0,
            findings=[dict(file=MANIFEST, line=0, category="invalid-source-allowlist")],
            scope="source-allowlist",
        )
    blobs = []
    if staged:
        records = _git(root, "ls-files", "--stage", "-z").stdout.split(b"\0")
        for record in records:
            if not record:
                continue
            info, raw_name = record.split(b"\t", 1)
            mode, digest, stage = info.decode().split()
            name = raw_name.decode("utf-8")
            if mode not in ("100644", "100755") or stage != "0":
                findings.append(dict(file=name, line=0, category="unsafe-index-entry"))
                continue
            blobs.append((name, _git(root, "cat-file", "blob", digest).stdout))
        staged_manifest = next((data for name, data in blobs if name == MANIFEST), None)
        if staged_manifest is None:
            findings.append(dict(file=MANIFEST, line=0, category="missing-staged-manifest"))
        elif staged_manifest != read_source(root, MANIFEST):
            findings.append(dict(file=MANIFEST, line=0, category="manifest-index-mismatch"))
    else:
        for name in sorted(allowed) + _additional_files(root, allowed):
            try:
                blobs.append((name, read_source(root, name)))
            except (OSError, ValueError):
                findings.append(dict(file=name, line=0, category="missing-or-unsafe-source"))
    for name, data in blobs:
        if name not in allowed and not (
            allow_build_metadata and not staged and _build_metadata(root, name, data)
        ):
            findings.append(dict(file=name, line=0, category="outside-source-allowlist"))
        findings.extend(check_blob(name, data, forbidden))
    for item in findings:
        if any(value.casefold() in item["file"].casefold() for value in forbidden):
            item["file"] = (
                "[redacted-name-" + hashlib.sha256(item["file"].encode()).hexdigest()[:12] + "]"
            )
    return dict(
        ok=not findings,
        files_checked=len(blobs),
        findings=findings,
        scope="git-index" if staged else "source-allowlist",
        limitation="Targeted checks; private policy, history/author identity and human review remain necessary.",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--private-policy", type=Path, default=os.environ.get("LIVE_TRANSCRIPT_PRIVATE_POLICY")
    )
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = audit(args.root, load_policy(args.private_policy), args.staged)
    except (OSError, ValueError, subprocess.SubprocessError):
        result = dict(
            ok=False,
            files_checked=0,
            findings=[dict(file=MANIFEST, line=0, category="audit-unavailable")],
        )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
