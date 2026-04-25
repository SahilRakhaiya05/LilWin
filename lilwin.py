"""Run LilWin from the repo root without typing ``src/`` — same as ``python src/main.py``."""

from __future__ import annotations

import os
import runpy
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

os.chdir(_SRC)
runpy.run_path(os.path.join(_SRC, "main.py"), run_name="__main__")
