"""Internal process entrypoint shared by local services and the guest bundle.

Users operate the bot through ``live-transcript``. Every supervised child uses
this dispatcher, so deployment never needs a parallel set of wrapper scripts.
"""

import argparse
import os
from pathlib import Path
import runpy
import re
import sys

ROLES = {
    "session": "live_transcript.runtime.session",
    "join": "live_transcript.providers.zoom",
    "prepare": "live_transcript.runtime.prepare",
    "check-media": "live_transcript.runtime.media_check",
    "audio": "live_transcript.capture.audio",
    "capture": "live_transcript.capture.guard",
    "observe": "live_transcript.capture.observer",
    "stream": "live_transcript.transcription.transport",
    "render": "live_transcript.presentation.render",
    "captions": "live_transcript.transcription.daemon",
    "app": "live_transcript.runtime.application",
    "application-job": "live_transcript.runtime.application",
    "watchdog": "live_transcript.runtime.watchdog",
    "calendar": "live_transcript.integrations.calendar",
    "asr": "live_transcript.transcription.service",
    "config": "live_transcript.core.config",
    "console": "live_transcript.runtime.console",
    "desktop": "live_transcript.runtime.console",
    "layout": "live_transcript.storage.layout",
    "state": "live_transcript.runtime.state",
    "bundle": "live_transcript.runtime.deployment",
    "mirror": "live_transcript.storage.mirror",
    "pull": "live_transcript.storage.pull",
    "recover": "live_transcript.storage.worker",
    "process": "live_transcript.runtime.postprocess",
    "calendar-dispatch": "live_transcript.integrations.calendar_dispatch",
    "tts": "live_transcript.speech.control",
    "video": "live_transcript.providers.video",
    "test-sender": "tools.tts_test_bot",
    "test-capture": "tools.smoke_recording_guard",
    "test-admission": "tools.smoke_admission",
    "fixture-session": "tools.session_lifecycle_fixture",
    "fixture-stop": "tools.stop_recording_fixture",
}
BUNDLE_NAME = "live-transcript.pyz"


def command(role, *arguments, python=None):
    """Build a child command for the current installed package or guest bundle."""
    if role not in ROLES:
        raise ValueError("Unknown worker role")
    bundle = os.environ.get("LIVE_TRANSCRIPT_BUNDLE")
    prefix = [python or sys.executable, "-u"]
    prefix += [bundle] if bundle else ["-m", "live_transcript.worker"]
    return [*prefix, role, *map(str, arguments)]


def command_env(python=None, *, base=None):
    """Expose checkout source to an ML interpreter without sharing site-packages."""
    if not python or os.environ.get("LIVE_TRANSCRIPT_BUNDLE"):
        return None
    src = Path(__file__).resolve().parent.parent
    if src.name != "src" or not (src.parent / "PUBLIC_SOURCE_FILES.txt").is_file():
        return None  # installed build: the interpreter must provide the package
    env = dict(os.environ if base is None else base)
    env["PYTHONSAFEPATH"] = "1"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(src) + (os.pathsep + existing if existing else "")
    return env


def guest_command(role, *arguments, test=False):
    """Build an SSH-side command without assuming a Python installation path."""
    from live_transcript.core.config import get

    if role not in ROLES:
        raise ValueError("Unknown worker role")
    name = "live-transcript-test.pyz" if test else BUNDLE_NAME
    return [
        get("vm", "python"),
        "-u",
        str(Path(get("vm", "home")) / name),
        role,
        *map(str, arguments),
    ]


def is_role_process(arguments, roles):
    """Recognize only our explicit dispatcher command line for process ownership."""
    values = [os.fsdecode(value) for value in arguments]
    if not values or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(values[0]).name):
        return False
    index = 1
    while index < len(values) and values[index] in ("-u", "-I", "-B", "-E", "-s"):
        index += 1
    if values[index : index + 2] == ["-m", "live_transcript.worker"]:
        index += 2
    elif index < len(values) and Path(values[index]).name in (
        BUNDLE_NAME,
        "live-transcript-test.pyz",
    ):
        index += 1
    else:
        return False
    return index < len(values) and values[index] in roles


def main(argv=None, *, bundle=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=sorted(ROLES))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if bundle is not None:
        os.environ["LIVE_TRANSCRIPT_BUNDLE"] = str(Path(bundle).resolve())
    original = sys.argv
    try:
        sys.argv = [f"live-transcript {args.role}", *args.arguments]
        runpy.run_module(ROLES[args.role], run_name="__main__", alter_sys=True)
    finally:
        sys.argv = original
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
