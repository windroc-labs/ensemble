import subprocess
import sys
from pathlib import Path

from tests.guards import UNIT_ISOLATION_ALLOWED

ROOT = Path(__file__).resolve().parents[1]
CONFIG = 'lint.flake8-tidy-imports.banned-api = {subprocess = {msg = "Unit tests cannot import subprocess"}, socket = {msg = "Unit tests cannot import socket"}, multiprocessing = {msg = "Unit tests cannot start multiprocessing"}, "concurrent.futures.ProcessPoolExecutor" = {msg = "Unit tests cannot start process pools"}}'


def _unit_files(root):
    return sorted(
        str(path)
        for path in root.rglob("*.py")
        if (
            {"unit", "tests"}.issubset(path.relative_to(root).parts)
            or path.parent == root / ".github"
            and path.name.endswith("_test.py")
        )
        and str(path.relative_to(root)) not in UNIT_ISOLATION_ALLOWED
    )


def main(root=ROOT):
    files = _unit_files(root)
    if not files:
        return 0
    return subprocess.run(
        [
            "ruff",
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            "TID251",
            "--config",
            CONFIG,
            "--output-format",
            "github",
            *files,
        ],
        timeout=60,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
