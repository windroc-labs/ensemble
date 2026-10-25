"""Manager CLI setup and captured invocation."""

import contextlib
import io
import os
from unittest import mock

from ensemble.manager.cli import main
from ensemble.manager.engine.roster import Roster

from manager.tests.case import Case


class CliCase(Case):
    def setUp(self):
        self.directory = os.path.join(self.ensemble, "managers", "mgr")
        self.roster = Roster(self.directory)
        env = {key: value for key, value in os.environ.items() if not key.startswith("ENSEMBLE_")}
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))

    def invoke(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(argv)
        return result, stdout.getvalue(), stderr.getvalue()
