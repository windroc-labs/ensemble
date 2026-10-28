"""Case, every manager test's base; child helpers (PY, child_env); with_ctl; this tree's import
roots (REPO, SRC, ROOTS, PYTHONPATH).
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
from ensemble.utils import Record
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
    return foreign(ensemble, ROOTS, SRC, RUN_WITH, modules)


def with_ctl(record: Record, command: str) -> Record:
    return replace(record, ctl=(command,), resume=(command, "resume", record.name))


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)
