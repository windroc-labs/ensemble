"""A bin/ wrapper run by absolute path from a temp cwd, no PYTHONPATH, a minimal PATH."""

import os
import shutil
import subprocess
import sys

MINIMAL = "/usr/bin:/bin"


def system_python_ok() -> bool:
    try:
        done = subprocess.run(
            ("/usr/bin/python3", "-c", "import sys; sys.exit(sys.version_info < (3, 11))"),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return False
    return done.returncode == 0


def minimal(root) -> str:
    """PATH is exactly /usr/bin:/bin where that python3 is 3.11+ (CI); elsewhere (macOS 3.9) a
    directory under root holding a python3 link to this interpreter comes first.
    """
    if system_python_ok():
        return MINIMAL
    own = os.path.join(root, "python")
    os.makedirs(own, exist_ok=True)
    if not os.path.lexists(link := os.path.join(own, "python3")):
        os.symlink(sys.executable, link)
    return f"{own}:{MINIMAL}"


def run(bin_path, path, *argv, cwd, home):
    return subprocess.run(
        (bin_path, *argv),
        cwd=cwd,
        env={"HOME": home, "PATH": path},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def probe_copy(root, bin_path):
    """Copies bin_path into root/repo/bin beside a python3 that prints its PATH, PYTHONPATH and
    arguments; returns the copy.
    """
    bin_dir = os.path.join(root, "repo", "bin")
    os.makedirs(bin_dir)
    copy = os.path.join(bin_dir, os.path.basename(bin_path))
    shutil.copy2(bin_path, copy)
    probe = os.path.join(bin_dir, "python3")
    with open(probe, "w", encoding="utf-8") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$PATH" "$PYTHONPATH" "$*"\n')
    os.chmod(probe, 0o755)
    return copy
