"""Compatibility import for the shared runtime metadata compiler."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.game_table_dsl import *  # noqa: F401,F403
