from __future__ import annotations

import sys
from pathlib import Path

OBSERVER_ROOT = Path(__file__).parents[2] / "services" / "observer"
sys.path.insert(0, str(OBSERVER_ROOT))
