"""Worker launch-turn exits through a real guard and tmux, and state on the real filesystem:
private modes, a refused FIFO, directory or oversize state, and a refused pointer with no sid.
"""

import os
import stat
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from ensemble.session import SessionError
from ensemble.session.engine import worker
from ensemble.session.engine.types import Exited, TurnEnd, TurnStarted
from ensemble.session.engine.worker import Worker, WorkerState
from tests.session import live_tmux
from tests.session.case import SID, Case
from tests.session.cli_support import MANAGER, WorkerCase


class LaunchTurnTest(WorkerCase):
    def test_exit_after_up_and_the_opening_still_fails_start(self):
        release = os.path.join(self.root, "release")
        scene = self.scene("w1", steps=[{"kind": "pause", "file": release}, {"kind": "exit"}])
        with mock.patch.dict(os.environ, self.env(**scene), clear=True):
            self.configure_session(
                {"claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"}}
            )
            state = WorkerState(
                name="w1", sid=SID, client="claude", cwd=self.cwd, host="tmux", display=MANAGER
            )
            # The observation deadline is tested with a virtual clock in unit tests.
            launch = Worker(state, session_cls=self.session_cls, clock=lambda: 0)

            def start():
                with launch.locked():
                    return launch.start(prompt="hi", resume=False)

            def ready():
                try:
                    owner = self.owner("w1")
                except FileNotFoundError:
                    return False
                status = owner.status()
                return (
                    status.get("up") is True
                    and status.get("opening") is None
                    and any(isinstance(row.event, TurnStarted) for row in owner.read(0)[0])
                )

            with ThreadPoolExecutor(max_workers=1) as pool:
                started = pool.submit(start)
                try:
                    live_tmux.wait(
                        ready, what="w1 up with its opening sent and launch turn started"
                    )
                    self.assertFalse(started.done(), self.harness_files("w1"))
                finally:
                    with open(release, "w", encoding="utf-8"):
                        pass
                with self.assertRaisesRegex(SessionError, "^w1: claude exited 0 while starting"):
                    started.result(timeout=40)

            owner = self.owner("w1")
            self.assertTrue(owner.status().get("up") is True)
            events = [row.event for row in owner.read(0)[0]]
            self.assertIn(Exited(0), events)
            self.assertFalse(any(isinstance(event, TurnEnd) for event in events), events)
            self.assertIsNone(self.dir_record("w1"))


class StateFileTest(Case):
    def setUp(self):
        super().setUp()
        self.state = WorkerState(name="w1", sid=SID, client="claude", cwd="/work", host="tmux")
        self.root = os.path.dirname(self.state.workdir())
        self.path = os.path.join(self.state.workdir(), worker.STATE_FILE)
        self.pointer = os.path.join(self.root, worker.BY_NAME, "w1")
        with Worker(self.state).locked():
            Worker(self.state)._save()

    def mode(self, path: str) -> int:
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_its_directories_are_0700_and_its_files_0600(self):
        for path in (self.root, self.state.workdir(), os.path.dirname(self.pointer)):
            with self.subTest(path=path):
                self.assertEqual(self.mode(path), 0o700)
        for path in (self.path, self.pointer):
            with self.subTest(path=path):
                self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(Worker.load("w1").state, self.state)

    def test_a_state_file_that_is_no_regular_file_is_refused(self):
        for make in (
            lambda: os.mkfifo(self.path, 0o600),
            lambda: os.mkdir(self.path, 0o700),
            lambda: self.write(self.path, "x" * (worker.STATE_MAX + 1)),
        ):
            with self.subTest(make=make):
                self.remove(self.path)
                make()
                with self.assertRaisesRegex(SessionError, "missing, not a regular file"):
                    Worker.load("w1")

    def test_a_pointer_that_holds_no_sid_is_refused(self):
        self.remove(self.pointer)
        self.write(self.pointer, "../../x\n")
        with self.assertRaisesRegex(SessionError, "holds no session id"):
            Worker.load("w1")

    @staticmethod
    def write(path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def remove(path: str) -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        elif os.path.lexists(path):
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
