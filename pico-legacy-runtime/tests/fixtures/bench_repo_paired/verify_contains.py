import sys
from pathlib import Path


text = Path(sys.argv[1]).read_text(encoding="utf-8")
missing = [expected for expected in sys.argv[2:] if expected not in text]
if missing:
    raise SystemExit("missing expected values: " + ", ".join(missing))
