"""HostContract against TmuxHost on a private tmux server (tests/session/live_tmux.py), os.environ
that server's env.
"""

import os
import unittest
from unittest import mock

from ensemble.session.hosts.tmux import TmuxHost
from tests.session import live_tmux
from tests.session.case import Case
from tests.session.contracts.host import HostContract

MANAGER = "mgr"


class TmuxHostContractTest(HostContract, Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def make_host(self):
        return TmuxHost("tmux")


if __name__ == "__main__":
    unittest.main()
