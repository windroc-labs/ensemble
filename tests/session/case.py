"""Case, every session test's base; HOSTS, configured, packaged_hosts; PY, child_env; REPO, SRC,
ROOTS; PLUGIN_BIN; SID; SPAWNS, the process starts; no_spawn.
"""

import contextlib
import os
import sys
from unittest import mock

import ensemble
from ensemble.session import Host
from ensemble.session.engine.config import read_config
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import Permission
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer.viewer import Viewer
from ensemble.utils import load_class
from tests.session import harnesses
from tests.utils.case import REPO, TreeCase, foreign, packages, run_with
from tests.utils.sandbox import clean_env, project_roots

BUNDLE = os.environ.get("ENSEMBLE_SESSION_BUNDLE")
ROOTS = (BUNDLE,) if BUNDLE else project_roots(REPO)
SRC = dict.fromkeys(("session", "utils"), BUNDLE) if BUNDLE else packages(ROOTS)
RUN_WITH = run_with(ROOTS)
PLUGIN_BIN = os.path.join(REPO, "bin")
PY = (sys.executable, "-P", "-S")
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
SPAWNS = ("subprocess.Popen", "os.fork", "os.posix_spawn", "os.posix_spawnp")
HOSTS = {"tmux": {"implementation": "ensemble.session.hosts.tmux:TmuxHost"}}


def packaged_hosts() -> dict[str, Permission]:
    hosts = Host.parse_hosts(read_config(local=os.devnull)["hosts"])
    return {
        name: load_class(table["implementation"], Host)(
            name, {k: v for k, v in table.items() if k != "implementation"}
        )
        ._client_context()
        .permission
        for name, table in hosts.tables.items()
    }


def configured(clients, tmux=None, *, host=None, **dependencies) -> type[HostedSession]:
    """A HostedSession class of `clients`, `tmux` in [hosts.tmux]; TmuxHosts and Viewers hold
    TmuxCli `host` (None: own); dependencies: class_loader, probe, clock, viewer_factory.
    """
    hosts = Host.parse_hosts({"tmux": {**HOSTS["tmux"], **(tmux or {})}, "default": "tmux"})

    def build(kind: type[Host], name: str, table) -> Host:
        return kind(name, table, tmux=host) if issubclass(kind, TmuxHost) else kind(name, table)

    dependencies.setdefault("viewer_factory", lambda settings: Viewer(settings, tmux=host))
    return harnesses.Threaded.configured(
        clients=clients, hosts=hosts, host_factory=build, **dependencies
    )


class Case(TreeCase):
    """Also self.session_cls, of self.session_dependencies; starts run their harness on a thread
    (harnesses.Threaded), joined at its end.
    """

    _case_clear = True

    def _case_foreign(self) -> str | None:
        return _foreign()

    def _case_env(self) -> dict[str, str]:
        return {
            **clean_env(None, *ROOTS),
            **super()._case_env(),
            "TUI_ATTACH_PREFIX": "",
            "PATH": os.pathsep.join((PLUGIN_BIN, os.environ.get("PATH", os.defpath))),
        }

    def _case_enter(self, stack) -> None:
        stack.enter_context(harnesses.joined())
        self.configure_session({})
        self.addCleanup(harnesses.end, self.home)

    def configure_session(self, clients, tmux=None) -> None:
        self.session_cls = configured(clients, tmux, **getattr(self, "session_dependencies", {}))


def _foreign(modules=None) -> str | None:
    return foreign(ensemble, ROOTS, SRC, RUN_WITH, modules)


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)


@contextlib.contextmanager
def no_spawn(*more: str):
    with contextlib.ExitStack() as stack:
        calls = [
            stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
            for target in (*SPAWNS, *more)
        ]
        yield
    for call in calls:
        call.assert_not_called()
