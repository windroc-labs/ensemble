"""Case, every manager test's base; child helpers (PY, child_env); with_ctl; status; own; this
tree's import roots (REPO, SRC, ROOTS, PYTHONPATH).
"""

import contextlib
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import replace
from unittest import mock

import ensemble
from ensemble.manager.engine.types import Spec
from ensemble.utils import Record, Status
from tests.session import harnesses
from tests.utils.case import REPO, ROOTS, RUN_WITH, SRC, TreeCase, foreign
from tests.utils.fakes import ctl
from tests.utils.sandbox import clean_env

PYTHONPATH = os.pathsep.join(ROOTS)
PY = (sys.executable, "-P", "-S")
HERE = ("ensemble-session", "ls", "--here", "--records")


class Case(TreeCase):
    """A TreeCase with no shared mutable session settings."""

    _case_clear = True

    def _case_foreign(self) -> str | None:
        return _foreign()

    def _case_env(self) -> dict[str, str]:
        return {**child_env(), **super()._case_env()}

    def _case_enter(self, stack) -> None:
        self.addCleanup(harnesses.end, self.home)

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
        os.symlink(os.path.join(REPO, "src"), os.path.join(root, "src"))
        return shutil.copy(os.path.join(REPO, "bin", "ensemble-crew"), os.path.join(root, "bin"))

    def fake_record(
        self, name: str, *, display: str | None = None, sid: str | None = None
    ) -> Record:
        return ctl.record(name, display, sid=sid or str(uuid.uuid4()))

    @contextlib.contextmanager
    def listed(self, records=()):
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) in (HERE, (*HERE, "--descendants")):
                return subprocess.CompletedProcess(argv, 0, ctl.lines(*records), "")
            return real_run(argv, **options)

        with mock.patch("subprocess.run", side_effect=run):
            yield


def _foreign(modules=None) -> str | None:
    return foreign(ensemble, ROOTS, SRC, RUN_WITH, modules)


def with_ctl(record: Record, command: str) -> Record:
    return replace(record, ctl=(command,), resume=(command, "resume", record.name))


def status(
    name, *, alive=True, state="working", outcome=None, attach=None, hidden=False, cwd=None
) -> Status:
    return Status(
        name=name, alive=alive, state=state, outcome=outcome, attach=attach, hidden=hidden, cwd=cwd
    )


def own(roster, record: Record, name=None, note=None) -> Record:
    """record committed under name (default its own), as if roster launched it."""
    name = name or record.name
    roster.begin(Spec(name, ("start",), note), sid=record.sid)
    roster.commit(name, record)
    return record


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)
