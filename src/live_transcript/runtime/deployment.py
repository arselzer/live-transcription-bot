"""Build the single self-contained Python guest program from reviewed package files."""

import argparse
from importlib import metadata
from pathlib import Path
import zipfile

from live_transcript.core.files import atomic_destination
from live_transcript.core.source_files import approved_paths, read_source

TEST_TOOLS = (
    "__init__.py",
    "tts_test_bot.py",
    "session_lifecycle_fixture.py",
    "stop_recording_fixture.py",
    "smoke_recording_guard.py",
    "smoke_admission.py",
)


def build_bundle(destination, *, package_dir=None, with_test_tools=False):
    package = Path(package_dir or Path(__file__).resolve().parents[1]).resolve()
    checkout = package.parent.parent
    if (checkout / "PUBLIC_SOURCE_FILES.txt").is_file():
        allowed = set(approved_paths(checkout))
        expected = {
            name
            for name in allowed
            if name.startswith("src/live_transcript/") and name.endswith(".py")
        }
        actual = {
            path.relative_to(checkout).as_posix()
            for path in package.rglob("*.py")
            if "__pycache__" not in path.parts
        }
        if actual != expected:
            raise ValueError("Guest package contains missing or unreviewed source")
        members = [
            (name.removeprefix("src/"), read_source(checkout, name)) for name in sorted(expected)
        ]
        if with_test_tools:
            for filename in TEST_TOOLS:
                name = "tools/" + filename
                if name not in allowed:
                    raise ValueError("Unreviewed test helper")
                members.append((name, read_source(checkout, name)))
    else:
        # A normal installed wheel is already checked by the build backend. Its
        # RECORD is the source inventory; unrelated files never enter the bundle.
        if with_test_tools:
            raise ValueError("Test tooling requires a development checkout")
        files = metadata.distribution("live-transcription-bot").files or []
        expected = {
            str(path)
            for path in files
            if str(path).startswith("live_transcript/") and str(path).endswith(".py")
        }
        if not expected:
            raise ValueError("Installed package source inventory unavailable")
        members = [(name, read_source(package.parent, name)) for name in sorted(expected)]
    target = Path(destination).resolve()
    if target.is_relative_to(package):
        raise ValueError("Bundle output must be outside the package")
    main = b"from live_transcript.worker import main\nimport sys\nraise SystemExit(main(bundle=sys.argv[0]))\n"
    members.append(("__main__.py", main))
    with atomic_destination(target) as temporary:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted(members):
                item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                item.compress_type = zipfile.ZIP_DEFLATED
                item.create_system = 3
                item.external_attr = 0o100644 << 16
                bundle.writestr(item, data)
    return [name for name, _ in members]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--with-test-tools", action="store_true")
    args = parser.parse_args(argv)
    build_bundle(args.output, with_test_tools=args.with_test_tools)


if __name__ == "__main__":
    main()
