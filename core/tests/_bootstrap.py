"""Shared test bootstrap: put the repository root on sys.path.

Import this first in every new test file, so that `python3 path/to/test_x.py`
works from any working directory.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
