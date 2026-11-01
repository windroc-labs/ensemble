"""Role `dummy-tester` and its task `echo`, owned by the tests: a team dir (team), which a local
config names through `[delegate] team_dirs` (enabled, configure).
"""

import atexit
import functools
import os
import re
import shutil
import tempfile

from tests.utils.case import REPO

ROLE, TASK = "dummy-tester", "echo"
CHARTER = (
    "# Dummy Tester\n\nYou check that a role, a task and their output work end to end.\n\n"
    "## Tasks\n\nPick the task below that fits the input; unsure → `echo` (`<tasks>/echo.md`).\n\n"
    "- `echo` (`<tasks>/echo.md`): return the input unchanged; only to check that a role, a task "
    "and their output work end to end; light.\n"
)
ECHO = (
    "# Echo\n\nReturn the input unchanged.\n\n## Steps\n\n1. **Report progress:**\n"
    "   [ensemble-progress:start] the input's first line\n"
    "2. **Echo.** The deliverable is the input, verbatim.\n"
    "3. **Finish.** `status: done`; `title: echo`; `summary` the input's first line, cut to 300 "
    "characters.\n\n## Resume\n\nRedo steps 2–3.\n"
)
FILES = {f"roles/{ROLE}.md": CHARTER, f"tasks/{TASK}.md": ECHO}
DELEGATE = re.compile(r"(?m)^\[delegate\]\n")


@functools.cache
def team() -> str:
    """The team dir, written once per process outside the temp dirs, which a Codex run can
    write: a team dir there is refused.
    """
    root = os.environ.get("ENSEMBLE_TEST_TMP", os.path.join(REPO, "work"))
    os.makedirs(root, exist_ok=True)
    top = os.path.realpath(tempfile.mkdtemp(dir=root, prefix="tester-"))
    atexit.register(shutil.rmtree, top, ignore_errors=True)
    os.chmod(top, 0o755)
    for rel, text in FILES.items():
        path = os.path.join(top, rel)
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return top


def enabled(local: str) -> str:
    """The local config text `local` (sectioned), its `[delegate]` table naming the team dir
    unless it sets team_dirs itself.
    """
    if "team_dirs" in local:
        return local
    line = f'team_dirs = ["{team()}"]\n'
    if DELEGATE.search(local):
        return DELEGATE.sub(lambda m: m[0] + line, local, count=1)
    return "[delegate]\n" + line + local


def configure(home: str, local: str = "") -> None:
    """`home`'s local config: `local` (sectioned), the team enabled."""
    path = os.path.join(home, ".ensemble", "config.local.toml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(enabled(local))
