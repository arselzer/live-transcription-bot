"""Launch the supervised host workflow using the installed package's assets."""

import os
from pathlib import Path
import subprocess
import sys


def command(*arguments):
    return ["bash", str(Path(__file__).with_name("launch.sh")), *map(str, arguments)]


def environment(base=None):
    return dict(os.environ if base is None else base, LIVE_TRANSCRIPT_PYTHON=sys.executable)


def start(*arguments):
    return subprocess.call(command(*arguments), env=environment())
