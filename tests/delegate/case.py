"""Case, every delegate test's base; the committed [clients] (committed); child helpers (PY,
child_env); this tree's import roots (REPO, SRC, ROOTS, PYTHONPATH).
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import ensemble
from ensemble.delegate.engine.drivers import AgentDriver
from ensemble.session import Session
from ensemble.session.engine.config import read_config
from tests.session import harnesses
from tests.session.case import configured
from tests.utils.sandbox import CALLER_SESSION, clean_env, project_roots

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = project_roots(REPO)
SRC = {
    name: root
    for root in ROOTS
    for name in os.listdir(os.path.join(root, "ensemble"))
    if os.path.isfile(os.path.join(root, "ensemble", name, "__init__.py"))
}
PYTHONPATH = os.pathsep.join(ROOTS)
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PY = (sys.executable, "-P", "-S")


def committed(local: str = os.devnull) -> dict:
    """The session config's [clients], `local` merged (default none), even under
    Case.configure_session.
    """
    return read_config(local=local)["clients"]


def packaged(**dependencies) -> type[Session]:
    """The session config's session class."""
    config = read_config()
    return configured(config["clients"], config["hosts"]["tmux"], **dependencies)


class Case(unittest.TestCase):
    """Fails a test whose ensemble is not exactly this tree's, else runs it (whatever its setUp) in
    a fresh temp HOME, self.home (self.ensemble: its .ensemble), a fresh session class
    (self.session_cls) and its FakeDriver, minus CALLER_SESSION; a start of a class with injected
    callables runs its session harness on a thread (harnesses.Threaded), joined at its end.
    """

    def run(self, result=None):
        if foreign := _foreign():
            result = self.defaultTestResult() if result is None else result
            result.startTest(self)
            try:
                self.fail(foreign)
            except self.failureException:
                result.addFailure(self, sys.exc_info())
            result.stopTest(self)
            return result
        with (
            tempfile.TemporaryDirectory() as self.home,
            mock.patch.dict(os.environ, {"HOME": self.home, "ENSEMBLE_CONTROL_ROOT": self.home}),
            harnesses.joined(),
        ):
            for key in CALLER_SESSION:
                os.environ.pop(key, None)
            self.ensemble = os.path.join(self.home, ".ensemble")
            os.mkdir(self.ensemble)
            self.configure_session({})
            self.addCleanup(harnesses.end, self.home)
            return super().run(result)

    def configure_session(self, clients, tmux=None) -> None:
        self.session_cls = configured(clients, tmux, **getattr(self, "session_dependencies", {}))
        self.driver = FakeDriver(self.session_cls)


def _foreign(modules=None) -> str | None:
    own = {os.path.realpath(os.path.join(r, "ensemble")) for r in ROOTS}
    for path in ensemble.__path__:
        if os.path.realpath(path) not in own:
            return f"ensemble.__path__ holds {path}, not only this tree's: run with PYTHONPATH={RUN_WITH}"
    if missing := own - {os.path.realpath(p) for p in ensemble.__path__}:
        return (
            f"ensemble.__path__ lacks {', '.join(sorted(missing))}: run with PYTHONPATH={RUN_WITH}"
        )
    for name, module in list((sys.modules if modules is None else modules).items()):
        top, _, sub = name.partition(".")
        where = getattr(module, "__file__", None)
        if top == "ensemble" and sub and "." not in sub and where:
            found = os.path.dirname(os.path.dirname(where))
            found = os.path.dirname(found) if os.path.basename(where) == "__init__.py" else found
            root = SRC.get(sub)
            if root is None or os.path.realpath(found) != os.path.realpath(root):
                return (
                    f"{name} is imported from {found}, not this tree's {root}: "
                    f"run with PYTHONPATH={RUN_WITH}"
                )
    return None


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)


class FakeDriver:
    """An AgentDriver whose per-test methods may be replaced."""

    session_based = True
    session = None
    display = None
    docker = False

    def __init__(self, session_cls):
        self.session_cls = session_cls

    def policy(self, client):
        return AgentDriver(self.session_cls).policy(client)

    def start(self, spec, *, display=None, host=None, idle_timeout=None):
        self.session = AgentDriver(self.session_cls).start(
            spec, display=display, host=host, idle_timeout=idle_timeout
        )
        return self.session

    def writable_dirs(self, spec):
        return self.session_cls.writable_dirs(spec)

    def preflight(self, spec):
        AgentDriver(self.session_cls).preflight(spec)
