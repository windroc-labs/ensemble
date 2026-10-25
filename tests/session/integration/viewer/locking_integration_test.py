"""Real grid lockfiles reject non-regular files and isolate each socket and window."""

import os

from ensemble.session.engine.tmux_cli import TuiError
from ensemble.session.viewer import grid
from tests.session.case import Case


class LockingTest(Case):
    def setUp(self):
        self.socket = os.path.join(self.home, "socket")
        self.path = self.socket + "-3.lock"

    def test_lock_is_exclusive_per_socket_window_and_released_after_exit(self):
        with grid._grid_lock(self.socket, "@3", wait=False) as held:
            self.assertTrue(held)
            with grid._grid_lock(self.socket, "@3", wait=False) as other:
                self.assertFalse(other)
            with grid._grid_lock(self.socket, "@4", wait=False) as other:
                self.assertTrue(other)
            with grid._grid_lock(self.socket + "-other", "@3", wait=False) as other:
                self.assertTrue(other)
        with grid._grid_lock(self.socket, "@3", wait=False) as held:
            self.assertTrue(held)

    def test_fifo_is_rejected_without_waiting_for_another_endpoint(self):
        os.mkfifo(self.path, 0o600)
        with (
            self.assertRaisesRegex(TuiError, "not a regular file$"),
            grid._grid_lock(self.socket, "@3", wait=False),
        ):
            self.fail("a FIFO must not hold a lock")
