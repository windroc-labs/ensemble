"""Shared orchestrator scenario fixtures for unit and sandboxed integration contracts."""

import os
import tempfile

from ensemble.delegate import Delegate
from ensemble.orchestrator.engine import config
from ensemble.utils import load_class
from tests.orchestrator.unit.board_ids import HEADER, PACKAGED_FLOW, role, workflow_line
from tests.utils.case import local_section

HARNESS_DELEGATE = load_class("ensemble.delegate:HarnessDelegate", Delegate)


PIPELINE = HEADER + role("researcher") + role("pm") + role("engineer")


# the packaged stages and a last one, dummy-tester (a core charter)
TESTER = workflow_line(PACKAGED_FLOW + '\n[[stage]]\nrole = "dummy-tester"\ninput = "research"\n')

# BASE, its dummy-tester stage without tables
BARE = HEADER + TESTER + role("researcher") + role("pm") + role("engineer")

BASE = BARE + role("dummy-tester")

CORE_TOP = '[delegate]\nlanguage = "Chinese"\n'


CORE_TABLES = """[delegate.roles.researcher.output]
type = "github"
repo = "windroc/team-docs"
branch = "main"
dir = "Research/"

[delegate.roles.pm.output]
type = "github"
repo = "windroc/team-docs"
branch = "main"
dir = "Product Design/"

[delegate.roles.verifier.output]
repo = "windroc/team-docs"
"""


CORE_LOCAL = CORE_TOP + "\n" + CORE_TABLES


SLUG = "acme/ensemble"


HTTPS = "https://github.com/acme/ensemble.git"


SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


STEPS = (
    "\n## Steps\n\n1. **Report progress:**\n   [ensemble-progress:start] the topic\n2. **Write.** Write it.\n\n"
    "## Resume\n\nRedo step 2.\n"
)


class ConfigFile:
    def setUp(self):
        write(os.path.join(self.ensemble, "config.local.toml"), CORE_LOCAL)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def local(self, text):
        """~/.ensemble/config.local.toml: `text` amid CORE_LOCAL (the delegate's keys)."""
        write(
            os.path.join(self.ensemble, "config.local.toml"),
            CORE_LOCAL + "\n" + local_section(text, "orchestrator"),
        )

    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        write(path, text)
        return config.load(path=path)

    def fails(self, message, text):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(text)
        self.assertEqual(str(cm.exception), message)
        return cm.exception
