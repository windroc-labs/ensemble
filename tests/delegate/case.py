"""Case, every delegate test's base; committed, packaged; PY, child_env; import roots (REPO,
SRC, ROOTS, PYTHONPATH).
"""

import os
import sys

import ensemble
from ensemble.delegate.engine.drivers import AgentDriver
from ensemble.session import Session
from ensemble.session.engine.config import read_config
from tests.session import harnesses
from tests.session.case import configured
from tests.utils.case import REPO as REPO
from tests.utils.case import ROOTS, RUN_WITH, SRC, TreeCase, foreign
from tests.utils.sandbox import CALLER_SESSION, clean_env

PYTHONPATH = os.pathsep.join(ROOTS)
PY = (sys.executable, "-P", "-S")


def committed(local: str = os.devnull) -> dict:
    """The session config's [clients], `local` merged."""
    return read_config(local=local)["clients"]


def packaged(**dependencies) -> type[Session]:
    """The session config's session class."""
    config = read_config()
    return configured(config["clients"], config["hosts"]["tmux"], **dependencies)


class Case(TreeCase):
    """Minus CALLER_SESSION; also self.session_cls and its self.driver; starts run their harness
    on a thread (harnesses.Threaded), joined at its end.
    """

    def _case_foreign(self) -> str | None:
        return _foreign()

    def _case_enter(self, stack) -> None:
        stack.enter_context(harnesses.joined())
        for key in CALLER_SESSION:
            os.environ.pop(key, None)
        self.configure_session({})
        self.addCleanup(harnesses.end, self.home)

    def configure_session(self, clients, tmux=None) -> None:
        self.session_cls = configured(clients, tmux, **getattr(self, "session_dependencies", {}))
        self.driver = FakeDriver(self.session_cls)


def _foreign(modules=None) -> str | None:
    return foreign(ensemble, ROOTS, SRC, RUN_WITH, modules)


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
