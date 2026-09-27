"""Make ``docker/smoke.py`` importable as ``smoke`` for the tests in this directory."""

from __future__ import annotations

import sys
from pathlib import Path

DOCKER_DIR = str(Path(__file__).resolve().parents[1])
if DOCKER_DIR not in sys.path:
    sys.path.insert(0, DOCKER_DIR)
