"""Case, every manager test's base; child helpers (PY, child_env); with_ctl; this tree's import
roots (REPO, SRC, ROOTS, PYTHONPATH).
"""

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from unittest import mock

import ensemble
from ensemble.utils import Record
from tests.utils.fakes import ctl
from tests.utils.sandbox import clean_env, project_roots

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
HERE = ("ensemble-session", "ls", "--here", "--records")


class Case(unittest.TestCase):
    """Fails a test whose ensemble is not exactly this tree's, else runs it (whatever its setUp) in
    a fresh temp HOME, self.home (self.ensemble: its .ensemble), no shared mutable session settings.
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
                {**child_env(), "HOME": self.home, "ENSEMBLE_CONTROL_ROOT": self.home},
                clear=True,
            ),
        ):
            self.ensemble = os.path.join(self.home, ".ensemble")
            os.mkdir(self.ensemble)
            return super().run(result)

    def setup_ctl(self) -> str:
        self.ctl_dir = os.path.join(self.home, "ctl")
        self.bin_dir = os.path.join(self.home, "bin")
        os.mkdir(self.bin_dir)
        command = ctl.install(self.bin_dir, self.ctl_dir)
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "PATH": os.pathsep.join(
                        (self.bin_dir, os.path.dirname(sys.executable), os.defpath)
                    )
                },
            )
        )
        return command

    def plugin_root(self) -> str:
        """A plugin root's ensemble-crew, alone in its bin/: the launcher puts that bin/ first on
        PATH, so fakes beat <repo>/bin's layer commands.
        """
        root = os.path.join(self.home, "plugin")
        os.makedirs(os.path.join(root, "bin"))
        for project in ("utils", "session", "delegate", "manager"):
            os.symlink(os.path.join(REPO, project), os.path.join(root, project))
        return shutil.copy(os.path.join(REPO, "bin", "ensemble-crew"), os.path.join(root, "bin"))

    def fake_record(
        self, name: str, *, display: str | None = None, sid: str | None = None
    ) -> Record:
        return Record(
            1,
            "fake",
            name,
            (name,),
            sid or str(uuid.uuid4()),
            None,
            (ctl.COMMAND,),
            (ctl.COMMAND, "resume", name),
            display,
            None,
        )

    @contextlib.contextmanager
    def listed(self, records=()):
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) in (HERE, (*HERE, "--descendants")):
                return subprocess.CompletedProcess(
                    argv, 0, "".join(record.to_line() + "\n" for record in records), ""
                )
            return real_run(argv, **options)

        with mock.patch("subprocess.run", side_effect=run):
            yield


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


def with_ctl(record: Record, command: str) -> Record:
    return replace(record, ctl=(command,), resume=(command, "resume", record.name))


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)
