"""CtlContract (tests/utils/contracts/ctl.py) against ensemble-session and its workers, the same
assertions the fake-ctl Crew's tests use passes.
"""

import os
import signal
import unittest

from ensemble.session.hosts.tmux import TmuxHost
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
        """The tmux Host's live workers and the server's tmux sessions."""
        return sorted(TmuxHost("tmux").list()), self.server.sessions()

    def refuse(self):
        return self.session("start", "refused", "--client", "no-such-client", "--prompt", "hi")

    def end(self, name):
        channel = self.owner(name)
        os.kill(channel.status()["pid"], signal.SIGKILL)  # a crash: its guard ends the client
        live_tmux.wait(lambda: not channel.alive(), what="the killed harness to let go")
        live_tmux.wait(lambda: not live_tmux.running(name), what=f"{name} gone")
        live_tmux.wait(lambda: not self.server.pane_of(name), what=f"{name}'s worker pane closed")


if __name__ == "__main__":
    unittest.main()
