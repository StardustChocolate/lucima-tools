"""Build-time compatibility exports for the shared runtime table builder."""
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.resource_tables import BATTLE_PATH, TABLE_FIELDS, STATS, LEVEL_KEYS, read_rows, build_battle_metadata
