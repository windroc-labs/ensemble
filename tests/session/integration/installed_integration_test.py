"""An installed session and utils: copies in a temp site with no .git at or above it, the fake
client copied in as module installed_client, its table in the copy's config.toml. A child
`python -I` with only that site on sys.path reads that config and runs the fake client headless.
"""

import json
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from tests.session.case import Case
from tests.session.integration.support import site_copy
from tests.utils.sandbox import clean_env, scratch

TIMEOUT = 60  # seconds
MODULE = "installed_client"
# The child (argv: site, workdir) prints the config read, the events and its packages' files.
CHILD = """import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
import ensemble.session, ensemble.utils
from ensemble.session import Access, Session, Spec
from ensemble.session.engine.config import read_config
config = read_config()
Session.configure_from(config)
handle = Session.start(Spec(workdir=sys.argv[2], access=Access(cwd=sys.argv[2]), client="fake",
                            mode="headless", prompt="hello"))
print(json.dumps({"config": config, "events": [type(e).__name__ for e in handle.events()],
                  "files": [importlib.util.find_spec("ensemble.session").origin,
                            importlib.util.find_spec("ensemble.utils").origin]}))
"""


class InstalledIntegration(Case):
    def setUp(self):
        self.root = Path(scratch(self))
        self.site = self.root / "site"
        site_copy(str(self.root), client=(str(self.site), MODULE))
        self.config = self.site / "ensemble" / "session" / "config.toml"
        for name in ("home", "tmp"):
            (self.root / name).mkdir()
        self.work = self.root / "work"

    def test_the_copy_reads_its_packaged_config_and_runs_a_headless_session(self):
        self.assertEqual(
            [str(p) for p in (self.root, *self.root.parents) if (p / ".git").exists()], []
        )
        child = subprocess.run(
            [sys.executable, "-I", "-c", CHILD, str(self.site), str(self.work)],
            cwd=self.root,
            env=clean_env(
                {**os.environ, "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp")}
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        data = json.loads(child.stdout)
        with self.config.open("rb") as f:
            self.assertEqual(data["config"], tomllib.load(f))
        for file in data["files"]:
            self.assertTrue(file.startswith(str(self.site / "ensemble") + os.sep), file)
        self.assertEqual(data["events"], ["Reply", "TurnEnd", "Exited"])


if __name__ == "__main__":
    unittest.main()
