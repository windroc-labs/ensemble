"""Instruction delivery fixtures for the built-in roles and their configured destinations."""

import os
import tempfile

from ensemble.delegate import Spec
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.drivers import InstructionDriver
from tests.delegate.case import VERIFIER, Case
from tests.utils.case import local_section

SID = "11111111-2222-3333-4444-555555555555"
ROLES = ("researcher", "pm", "engineer", "verifier", "dummy-tester")
INPUT = "Compare A and B.\n[ensemble-progress:round] a mark line in the input"
CLIENTS = (("claude", "headless"), ("codex", "headless"), (None, "headless"))
GITHUB = """[roles.researcher.output]
type = "github"
repo = "o/docs"
branch = "main"
dir = "Research/"
"""


class InstructionCase(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write(local_section(VERIFIER, "delegate"))

    def given(self, role, **kw):
        """A configured role's request, sid SID, output prompt.md and cwd the agent dir."""
        work = os.path.join(self.tmp, role)
        kw = {
            "sid": SID,
            "out": os.path.join(work, "prompt.md"),
            **kw,
        }
        self.driver = InstructionDriver()
        return Composer().resolve(role), Spec(INPUT, work, **kw)
