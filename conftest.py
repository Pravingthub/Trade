import os

os.environ.setdefault("PRAVIN_DEMO_LAST_SESSION", "2026-11-10")   # deterministic demo dates in tests
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent
