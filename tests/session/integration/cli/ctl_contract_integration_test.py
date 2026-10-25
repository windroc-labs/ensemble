"""CtlContract (tests/utils/contracts/ctl.py) against ensemble-session and its workers, the same
assertions the fake-ctl Crew's tests use passes.
"""

import unittest

from ensemble.utils import Record
from tests.session import live_tmux
from tests.session.cli_support import CliCase
from tests.utils.contracts.ctl import NamedCtlContract


class SessionCtlContract(NamedCtlContract, CliCase):
    command = "ensemble-session"

    def call(self, *argv, stdin="", sid=None):
        return self.session(*argv, stdin=stdin, ENSEMBLE_SID=sid, **self.scene("any"))

    def start(self, name, sid=None) -> Record:
        record = self.record(CliCase.start(self, name, ENSEMBLE_SID=sid))
        self.wait_event(name, "done")
        return record

    def inventory(self):
        return self.server.tmux("list-sessions", "-F", "#{session_name}").stdout

    def refuse(self):
        return self.session("start", "refused", "--client", "no-such-client", "--prompt", "hi")

    def end(self, name):
        self.server.tmux("kill-session", "-t", f"={name}")
        live_tmux.wait(
            lambda: self.server.tmux("has-session", "-t", f"={name}").returncode,
            what=f"{name} gone",
        )


if __name__ == "__main__":
    unittest.main()
