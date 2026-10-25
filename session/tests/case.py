"""Case, every session test's base; session settings (HOSTS, configured, packaged_hosts); child
helpers (PY, child_env); this tree's import roots (REPO, SRC, ROOTS), PLUGIN_BIN (first on PATH);
SPAWNS, the calls that start a process.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import ensemble
from ensemble.session import Host
from ensemble.session.engine.config import read_config
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import Permission
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer.viewer import Viewer
from ensemble.utils import load_class

from utils.tests.sandbox import clean_env, project_roots

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUNDLE = os.environ.get("ENSEMBLE_SESSION_BUNDLE")
ROOTS = (BUNDLE,) if BUNDLE else project_roots(REPO)
SRC = (
    dict.fromkeys(("session", "utils"), BUNDLE)
    if BUNDLE
    else {os.path.basename(os.path.dirname(r)).replace("-", "_"): r for r in ROOTS}
)
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PLUGIN_BIN = os.path.join(REPO, "bin")
PY = (sys.executable, "-P", "-S")
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
    """A HostedSession class of `clients`, HOSTS with `tmux` in [hosts.tmux]; each TmuxHost and its
    Viewer hold TmuxCli `host` (None: their own); dependencies: class_loader, probe, clock,
    viewer_factory.
    """
    hosts = Host.parse_hosts({"tmux": {**HOSTS["tmux"], **(tmux or {})}, "default": "tmux"})

    def build(kind: type[Host], name: str, table) -> Host:
        return kind(name, table, tmux=host) if issubclass(kind, TmuxHost) else kind(name, table)

    dependencies.setdefault("viewer_factory", lambda settings: Viewer(settings, tmux=host))
    return HostedSession.configured(
        clients=clients, hosts=hosts, host_factory=build, **dependencies
    )


class Case(unittest.TestCase):
    """Fails a test whose ensemble is not exactly this tree's, else runs it (whatever its setUp) in
    a fresh temp HOME, self.home (self.ensemble: its .ensemble), a fresh session class
    (self.session_cls) configured with self.session_dependencies.
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
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as self.home,
            mock.patch.dict(
                os.environ,
                {
                    **clean_env(None, *ROOTS),
                    "HOME": self.home,
                    "ENSEMBLE_CONTROL_ROOT": self.home,
                    "TUI_ATTACH_PREFIX": "",
                    "PATH": os.pathsep.join((PLUGIN_BIN, os.environ.get("PATH", os.defpath))),
                },
                clear=True,
            ),
        ):
            self.ensemble = os.path.join(self.home, ".ensemble")
            os.mkdir(self.ensemble)
            self.configure_session({})
            return super().run(result)

    def configure_session(self, clients, tmux=None) -> None:
        self.session_cls = configured(clients, tmux, **getattr(self, "session_dependencies", {}))


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
