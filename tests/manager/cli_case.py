"""Manager CLI setup and captured invocation; the installed ensemble-crew run with executable fakes."""

import contextlib
import io
import os
import subprocess
from unittest import mock

from ensemble.manager.cli import main
from ensemble.manager.engine.roster import Roster
from tests.manager import fake_session
from tests.manager.case import Case, child_env

IDENTITY = (
    "ENSEMBLE_MANAGER",
    "ENSEMBLE_HOLDER_ID",
    "ENSEMBLE_HOLDER_GENERATION",
    "ENSEMBLE_HOLDER_PID",
)


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


class InstalledCrewCase(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.sessions = os.path.join(self.home, "session-list")
        self.crew_bin = self.plugin_root()
        fake_session.install(os.path.dirname(self.crew_bin), self.sessions)
        self.directory = os.path.join(self.ensemble, "managers", "mgr")
        env = {key: value for key, value in os.environ.items() if key not in IDENTITY}
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))

    def invoke(self, *argv, home=None, clean=False):
        env = dict(os.environ)
        if home is not None:
            env["HOME"] = home
        if clean:
            env.pop("PYTHONPATH", None)
        return subprocess.run(
            (self.crew_bin, *argv),
            cwd=self.home,
            env=child_env(env),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
