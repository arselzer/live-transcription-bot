"""Build a private, reproducible guest bundle from reviewed source only."""

import argparse
import gzip
import io
import json
from pathlib import Path
import tarfile

from live_transcript.core.files import atomic_destination
from live_transcript.core.source_files import approved_paths, read_source


def build_bundle(root, destination):
    root = Path(root).resolve()
    allowed = set(approved_paths(root))
    mapping = json.loads(read_source(root, "docs/module-map.json"))
    names = {"_bootstrap.py", "pyproject.toml"}
    names.update(name + ".py" for name in mapping)
    names.update(
        p.relative_to(root).as_posix()
        for p in (root / "src/live_transcript").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    names.update(p.relative_to(root).as_posix() for p in (root / "tools").glob("*.py"))
    names.update(("orphan_sweep.sh", "prep_remote.sh", "vm_pa_fix.sh", "vm_stop_joiner.sh"))
    if names - allowed:
        raise ValueError("Guest source is outside the reviewed allowlist")
    target = Path(destination).resolve()
    if target.is_relative_to(root) and target.relative_to(root).as_posix() in allowed:
        raise ValueError("Bundle output would replace reviewed source")
    # Validate and read before touching an existing output bundle.
    members = []
    for name in sorted(names):
        data = read_source(root, name)
        if name == "join_meeting.py":
            data = b"from _bootstrap import dispatch\ndispatch(__name__, 'live_transcript.providers.zoom')\n"
        members.append((name, data))
    with atomic_destination(target) as temporary:
        with temporary.open("wb") as output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for name, data in members:
                        item = tarfile.TarInfo(name)
                        item.size = len(data)
                        item.mode = 0o755 if (root / name).stat().st_mode & 0o111 else 0o644
                        item.mtime = 0
                        item.uid = item.gid = 0
                        item.uname = item.gname = ""
                        archive.addfile(item, io.BytesIO(data))
    return [name for name, _ in members]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    args = parser.parse_args(argv)
    from live_transcript.core.config import get

    build_bundle(get("paths", "root"), args.output)


if __name__ == "__main__":
    main()
