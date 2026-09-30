import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for p in (_here, _here.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
