"""Private, atomic artifact publication; failures retain the previous file."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile


@contextmanager
def atomic_destination(path):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(
        prefix="." + path.stem + "-", suffix=path.suffix, dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path, text):
    with atomic_destination(path) as temporary:
        temporary.write_text(text, encoding="utf-8")


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, allow_nan=False))
