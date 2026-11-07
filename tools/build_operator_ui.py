"""Build pinned frontend assets outside the source tree; Node is build-time only."""

from pathlib import Path
import shutil
import subprocess
import tempfile

from live_transcript.core.source_files import approved_paths, read_source


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="live-transcript-ui-") as directory:
        target = Path(directory)
        for name in approved_paths(root):
            if name.startswith("frontend/"):
                destination = target / name.removeprefix("frontend/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(read_source(root, name))
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=target, check=True
        )
        subprocess.run(["npm", "run", "build"], cwd=target, check=True)
        assets = root / "src/live_transcript/operator_ui"
        assets.mkdir(exist_ok=True)
        output = list((target / "dist").iterdir())
        if {path.name for path in output} != {"app.js", "style.css", "index.html"} or not all(
            path.is_file() for path in output
        ):
            raise ValueError("Unexpected frontend output")
        for path in output:
            shutil.copyfile(path, assets / path.name)
        # Preserve upstream license with the distributed, bundled runtime.
        shutil.copyfile(target / "node_modules/vue/LICENSE", assets / "VUE-LICENSE.txt")


if __name__ == "__main__":
    main()
