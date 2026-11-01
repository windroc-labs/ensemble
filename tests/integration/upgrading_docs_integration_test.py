"""The upgrade doc's cleanup of old usage state, run in a temporary home."""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from tests.case import REPO, Case


def cleanup() -> str:
    """The shell block that deletes ~/.ensemble/usage."""
    text = Path(REPO, "docs", "upgrading.md").read_text()
    blocks = re.findall(r"```sh\n(.*?)```", text, re.S)
    (found,) = [block for block in blocks if "~/.ensemble/usage" in block]
    return found


class UsageCleanupTest(Case):
    def run_in(self, home):
        subprocess.run(
            ["sh", "-c", cleanup()],
            env={"HOME": home, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            check=False,
        )

    def test_only_old_readings_holds_and_their_locks_go(self):
        usage = ("claude.json", "claude.json.lock", "claude-holds.json", "claude-holds.json.lock")
        kept = (
            ".ensemble/usage/notes.txt",
            ".ensemble/config.local.toml",
            ".claude-work/.credentials.json",
        )
        for extra in (kept, ()):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as home:
                for rel in (*(f".ensemble/usage/{name}" for name in usage), *extra):
                    os.makedirs(os.path.dirname(os.path.join(home, rel)), exist_ok=True)
                    Path(home, rel).write_text("x")
                self.run_in(home)
                for name in usage:
                    self.assertFalse(Path(home, ".ensemble/usage", name).exists())
                for rel in extra:
                    self.assertTrue(Path(home, rel).exists())
                self.assertEqual(Path(home, ".ensemble/usage").exists(), bool(extra))
