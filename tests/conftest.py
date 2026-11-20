"""pytest로 돌릴 때도 src와 tests를 import 경로에 올린다."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, "src"), os.path.join(ROOT, "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)
