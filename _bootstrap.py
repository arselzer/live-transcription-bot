"""Source-checkout compatibility for historical script entrypoints."""

import importlib
from pathlib import Path
import runpy
import sys


def dispatch(name, module):
    source = Path(__file__).resolve().parent / "src"
    if source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))
    if name == "__main__":
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    else:
        sys.modules[name] = importlib.import_module(module)
