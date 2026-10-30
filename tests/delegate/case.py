"""Case, every delegate test's base; FakeClientCase; RunReads; committed, packaged; PY,
child_env; SID; import roots.
"""

import os
import sys

import ensemble
from ensemble.delegate.engine.drivers import AgentDriver
from ensemble.session import Session
from ensemble.session.engine.config import read_config
from tests.delegate.workdir import run_log
from tests.session import fake_client, harnesses
from tests.session.case import configured, preserve_configuration
from tests.session.modules import client_loader
from tests.utils.case import REPO as REPO
from tests.utils.case import ROOTS, RUN_WITH, SRC, TreeCase, foreign
from tests.utils.jsonl import read_jsonl
from tests.utils.sandbox import CALLER_SESSION, clean_env, scratch

PYTHONPATH = os.pathsep.join(ROOTS)
PY = (sys.executable, "-P", "-S")
SID = "11111111-2222-3333-4444-555555555555"


def committed(local: str = os.devnull) -> dict:
    """The session config's [clients], `local` merged."""
    return read_config(local=local)["clients"]


def packaged(**dependencies) -> type[Session]:
    """The session config's session class."""
    config = read_config()
    return configured(config["clients"], config["hosts"]["tmux"], **dependencies)


class Case(TreeCase):
    """Minus CALLER_SESSION; self.session_cls, self.driver; harnesses run on joined threads."""

    def _case_foreign(self) -> str | None:
        return _foreign()

    def _case_enter(self, stack) -> None:
        preserve_configuration(self)
        stack.enter_context(harnesses.joined())
        for key in CALLER_SESSION:
            os.environ.pop(key, None)
        self.configure_session({})
        self.addCleanup(harnesses.end, self.home)

    def configure_session(self, clients, tmux=None) -> None:
        self.session_cls = configured(clients, tmux, **getattr(self, "session_dependencies", {}))
        self.driver = FakeDriver(self.session_cls)


class FakeClientCase(Case):
    """The fake client as `fake`, its files in scratch `tmp`."""

    def setUp(self):
        self.tmp = scratch(self)
        self.work, self.scenario, self.log = (
            os.path.join(self.tmp, n) for n in ("work", "scenario.json", "fake.log")
        )
        self.out = os.path.join(self.work, "out.md")
        self.session_dependencies = {
            "class_loader": client_loader({"ensemble.session.clients.fake": fake_client})
        }
        self.configure_fake()

    def configure_fake(self, *, tui=False):
        self._tui = tui
        self.configure_session(
            {
                "fake": {
                    "implementation": "ensemble.session.clients.fake:FakeClient",
                    "scenario": self.scenario,
                    "log": self.log,
                    "tui": tui,
                }
            }
        )


class RunReads:
    """Reads of `work`'s run log."""

    work: str

    def records(self) -> list[dict]:
        return read_jsonl(run_log(self.work))

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def last(self, kind) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]


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
