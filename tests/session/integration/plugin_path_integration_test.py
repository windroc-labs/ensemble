"""FR-32: a session's client runs the running plugin's commands: its PATH starts with the bin/ where
the starter's PATH finds ensemble-session, so ensemble-crew and ensemble-session resolve there and run,
ahead of a stale ensemble-crew earlier on the starter's PATH. The client carries the agent session
marker, so its ensemble-session start exits 2.
"""

import json
import os
import sys
from unittest import mock

from tests.session.case import REPO
from tests.session.hosted_support import Base

PLUGIN_BIN = os.path.join(REPO, "bin")


class PluginPathTest(Base):
    def test_a_client_runs_the_plugins_commands_from_the_bin_its_path_leads_with(self):
        stale = os.path.join(self.dir, "stale bin")
        os.mkdir(stale)
        with open(os.path.join(stale, "ensemble-crew"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(os.path.join(stale, "ensemble-crew"), 0o755)
        inherited = os.pathsep.join(
            (stale, os.path.dirname(sys.executable), "/usr/bin", PLUGIN_BIN, "/bin")
        )
        commands = {
            ("ensemble-crew", "--help"): 0,
            ("ensemble-session", "--help"): 0,
            ("ensemble-session", "start", "nested", "--client", "fake", "--", "x"): 2,
        }
        self.steps(*({"run": list(argv)} for argv in commands))
        with mock.patch.dict(os.environ, {"PATH": inherited}):
            self.drain(self.start())
        with open(self.log, encoding="utf-8") as f:
            ran = [entry for entry in map(json.loads, f) if "ran" in entry]
        self.assertEqual(
            ran,
            [
                {"ran": list(argv), "found": os.path.join(PLUGIN_BIN, argv[0]), "rc": rc}
                for argv, rc in commands.items()
            ],
        )
