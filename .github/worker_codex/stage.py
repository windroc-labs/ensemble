import runpy
import sys
from pathlib import Path

archive = Path(sys.argv[1])
try:
    with archive.open("rb") as source:
        runpy.run_path("/opt/ensemble/lib/codex-update.py")["update"]("stage", source)
finally:
    archive.unlink(missing_ok=True)
