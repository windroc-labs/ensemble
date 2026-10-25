"""Worker state on the real filesystem: private modes, and a state file that is a FIFO,
directory or oversize, or a pointer that holds no sid, is refused promptly.
"""

import os
import stat
import unittest

from ensemble.session import SessionError
from ensemble.session.engine import worker
from ensemble.session.engine.worker import Worker, WorkerState

from session.tests.case import Case

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"


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
