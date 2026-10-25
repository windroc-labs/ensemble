"""A configured session class's default Host detaching on a private tmux server
(tests/session/live_tmux.py), os.environ that server's env: a child that reports its blocked
signals, unblocks them and runs until told to end; its session live then absent, its @opener and
@display, a taken name, a cwd it cannot enter, a script whose interpreter is missing.
"""

import errno
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

from ensemble.session import SIGNALS, Process
from ensemble.session.engine.tmux_cli import TmuxCli, TuiError
from ensemble.session.engine.types import Launch, SessionError
from tests.session import live_tmux
from tests.session.case import Case

MANAGER = "mgr"
# The child: writes its blocked signals to argv[1], unblocks them, then runs until argv[2] exists,
# at most 30 s.
CHILD = """import json, os, signal, sys, time
report, go = sys.argv[1:]
with open(report + ".tmp", "w") as f:
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    json.dump(sorted(int(s) for s in blocked), f)
os.replace(report + ".tmp", report)
signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGHUP, signal.SIGTERM, signal.SIGINT])
for _ in range(600):
    if os.path.exists(go): break
    time.sleep(0.05)
"""


class DetachTest(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.go = os.path.join(self.dir, "go")

    def argv(self, name: str) -> list[str]:
        return [sys.executable, "-I", "-c", CHILD, os.path.join(self.dir, f"{name}.json"), self.go]

    def detach(self, name: str, argv=None, *, display=None) -> Process:
        """`argv` (else the child) detached as `name` on the default Host, SIGNALS blocked."""
        launch = Launch(
            tuple(self.argv(name) if argv is None else argv),
            self.server.env(),
            cwd=self.dir,
            blocked_signals=SIGNALS,
        )
        return self.session_cls.host().detach(name, launch, display=display)

    def blocked(self, name: str) -> set[int]:
        """The signals child `name` started with blocked, once it reports them."""
        path = os.path.join(self.dir, f"{name}.json")
        live_tmux.wait(lambda: os.path.exists(path), what=f"{name}'s report")
        with open(path) as f:
            return set(json.load(f))

    def gone(self) -> bool:
        # tmux on Linux (CI: 3.4) can leave an exited pane process unreaped until another child of
        # the server exits
        self.server.tmux("run-shell", "-b", "true")
        return self.session_cls.list("d.*") == []

    def test_the_child_runs_with_the_signals_blocked_its_session_live_until_it_exits(self):
        self.detach("d1", display=MANAGER)
        self.assertEqual([r.name for r in self.session_cls.list("d1")], ["d1"])
        TmuxCli().detach("d0", Launch(tuple(self.argv("d0")), self.server.env(), cwd=self.dir))
        signals = {int(s) for s in SIGNALS}
        self.assertLessEqual(signals, self.blocked("d1"))
        self.assertEqual(signals & self.blocked("d0"), set())
        self.assertEqual(
            [self.server.option("d1", key) for key in ("@opener", "@display")], ["", MANAGER]
        )
        with self.assertRaises(SessionError) as caught:
            self.session_cls.attach("d1")
        self.assertTrue(str(caught.exception).startswith("no tui session d1: "), caught.exception)
        with open(self.go, "w"):
            pass
        live_tmux.wait(self.gone, what="d0 and d1 absent once their children exit")

    def test_a_taken_name_is_refused_and_its_session_left_alone(self):
        self.detach("d1")
        self.blocked("d1")
        pid = self.server.tmux("display-message", "-p", "-t", "=d1:", "#{pane_pid}").stdout
        for name in ("d1", MANAGER):
            with self.subTest(name=name), self.assertRaises(SessionError) as caught:
                self.detach(name, display="other")
            self.assertEqual(str(caught.exception), f"detach {name}: session {name} exists")
        self.assertEqual(
            self.server.tmux("display-message", "-p", "-t", "=d1:", "#{pane_pid}").stdout, pid
        )
        self.assertEqual([self.server.option(n, "@opener") for n in ("d1", MANAGER)], ["", ""])
        self.assertEqual([r.name for r in self.session_cls.list()], ["d1", MANAGER])

    def test_a_cwd_it_cannot_enter_is_refused_never_a_success(self):
        missing = os.path.join(self.dir, "missing")
        with self.assertRaisesRegex(TuiError, "^session d2 did not start$"):
            TmuxCli(sleep=lambda seconds: time.sleep(seconds / 10)).detach(
                "d2", Launch(tuple(self.argv("d2")), self.server.env(), cwd=missing)
            )
        live_tmux.wait(self.gone, what="no session left")

    def test_a_script_whose_interpreter_is_missing_fails_it_leaving_no_session(self):
        script = os.path.join(self.dir, "script")
        with open(script, "w") as f:
            f.write("#!/nonexistent/interpreter\n")
        os.chmod(script, 0o755)
        error = f"detach d1: cannot run {script}: {os.strerror(errno.ENOENT)}"
        with self.assertRaisesRegex(SessionError, "^" + re.escape(error) + "$"):
            self.detach("d1", [script])
        live_tmux.wait(self.gone, what="no session left")


if __name__ == "__main__":
    unittest.main()
