"""Build the single self-contained Python guest program from reviewed package files."""

import argparse
import base64
import csv
import hashlib
import io
from importlib import metadata
from pathlib import Path
import re
import zipfile

from live_transcript.core.files import atomic_destination
from live_transcript.core.source_files import approved_paths, read_source

TEST_TOOLS = (
    "__init__.py",
    "tts_test_bot.py",
    "session_lifecycle_fixture.py",
    "stop_recording_fixture.py",
    "smoke_recording_guard.py",
    "smoke_audio_server.py",
    "smoke_admission.py",
)


def package_members(*, package_dir=None, with_test_tools=False, resources=False):
    package = Path(package_dir or Path(__file__).resolve().parents[1]).resolve()
    checkout = package.parent.parent
    if (checkout / "PUBLIC_SOURCE_FILES.txt").is_file():
        allowed = set(approved_paths(checkout))
        selected = {
            name
            for name in allowed
            if name.startswith("src/live_transcript/") and (resources or name.endswith(".py"))
        }
        expected = {name for name in selected if name.endswith(".py")}
        actual = {
            path.relative_to(checkout).as_posix()
            for path in package.rglob("*.py")
            if "__pycache__" not in path.parts
        }
        if actual != expected:
            raise ValueError("Guest package contains missing or unreviewed source")
        members = [
            (name.removeprefix("src/"), read_source(checkout, name)) for name in sorted(selected)
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
            if str(path).startswith("live_transcript/")
            and "__pycache__" not in path.parts
            and (resources or str(path).endswith(".py"))
        }
        if not expected:
            raise ValueError("Installed package source inventory unavailable")
        members = [(name, read_source(package.parent, name)) for name in sorted(expected)]
    return package, members


def write_zip(destination, members):
    with atomic_destination(destination) as temporary:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted(members):
                item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                item.compress_type = zipfile.ZIP_DEFLATED
                item.create_system = 3
                item.external_attr = 0o100644 << 16
                bundle.writestr(item, data)


def build_application_wheel(directory):
    """Repackage only reviewed/installed application files, with no host config."""
    package, members = package_members(resources=True)
    version = metadata.version("live-transcription-bot")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version):
        raise ValueError("Application export requires a numeric release version")
    info = f"live_transcription_bot-{version}.dist-info"
    members += [
        (
            info + "/METADATA",
            f"Metadata-Version: 2.1\nName: live-transcription-bot\nVersion: {version}\nRequires-Python: >=3.11\nRequires-Dist: Pillow>=10\n\n".encode(),
        ),
        (
            info + "/WHEEL",
            b"Wheel-Version: 1.0\nGenerator: live-transcript\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        ),
        (
            info + "/entry_points.txt",
            b"[console_scripts]\nlive-transcript = live_transcript.cli:main\n",
        ),
    ]
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for name, data in members:
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, len(data)])
    writer.writerow([info + "/RECORD", "", ""])
    members.append((info + "/RECORD", record.getvalue().encode()))
    destination = Path(directory) / f"live_transcription_bot-{version}-py3-none-any.whl"
    if destination.resolve().is_relative_to(package):
        raise ValueError("Application export must be outside the package")
    write_zip(destination, members)
    return destination


def build_bundle(destination, *, package_dir=None, with_test_tools=False):
    package, members = package_members(package_dir=package_dir, with_test_tools=with_test_tools)
    target = Path(destination).resolve()
    if target.is_relative_to(package):
        raise ValueError("Bundle output must be outside the package")
    main = b"from live_transcript.worker import main\nimport sys\nraise SystemExit(main(bundle=sys.argv[0]))\n"
    members.append(("__main__.py", main))
    write_zip(target, members)
    return [name for name, _ in members]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--with-test-tools", action="store_true")
    args = parser.parse_args(argv)
    build_bundle(args.output, with_test_tools=args.with_test_tools)


if __name__ == "__main__":
    main()
