"""Build-time compatibility exports for the shared game table parser."""
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.table_dsl import DSLParser, DSLParseError
