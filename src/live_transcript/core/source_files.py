"""Validate explicitly selected source paths before reading or exporting them."""

from pathlib import Path, PurePosixPath
import re
import stat

MANIFEST = "PUBLIC_SOURCE_FILES.txt"
MAX_SOURCE_BYTES = 4 * 1024 * 1024


def validate_name(name):
    path = PurePosixPath(name)
    if (
        not name
        or name == "."
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or re.match(r"^[A-Za-z]:", name)
        or path.as_posix() != name
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError("Invalid source member name")
    return path


def read_source(root, name):
    relative = validate_name(name)
    root = Path(root).resolve()
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symlinked source member")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_SOURCE_BYTES:
        raise ValueError("Unsupported source member")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Source member escaped root")
    return path.read_bytes()


def approved_paths(root):
    text = read_source(root, MANIFEST).decode("utf-8")
    paths = []
    for line in text.splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        validate_name(name)
        if name in paths:
            raise ValueError("Duplicate source member")
        paths.append(name)
    if not paths or MANIFEST not in paths:
        raise ValueError("Incomplete source allowlist")
    return paths
