"""Run offline regressions without loading the operator's deployment config."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="live-transcript-tests-") as directory:
        temporary = Path(directory)
        config = temporary / "config.toml"
        settings = dict(
            root=str(root),
            state=str(temporary / "state"),
            logs=str(temporary / "logs"),
            recordings=str(temporary / "recordings"),
            safety_copies=str(temporary / "safety"),
        )
        config.write_text(
            "[paths]\n"
            + "\n".join(k + " = " + json.dumps(v) for k, v in settings.items())
            + '\n[vm]\nssh = "transcript@192.0.2.20"\nip = "192.0.2.20"\nmanage_libvirt = false\n'
        )
        clean = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CAPTION_", "LT_", "LIVE_TRANSCRIPT_")) and key != "SCRIBE_DIR"
        }
        env = dict(
            clean,
            LIVE_TRANSCRIPT_CONFIG=str(config),
            XDG_CONFIG_HOME=str(temporary / "config-home"),
            PYTHONPATH=os.pathsep.join((str(root / "src"), str(root))),
        )
        return subprocess.call(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            cwd=root,
            env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
