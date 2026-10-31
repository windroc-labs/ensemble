"""Case, every orchestrator test's base; child helpers (PY, child_env, ORCH_ROOT); SID;
zone(case, tz); this tree's import roots (REPO, SRC, ROOTS, PYTHONPATH).
"""

import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

import ensemble
from ensemble.orchestrator.engine import log
from ensemble.orchestrator.engine.flow import workflow
from ensemble.orchestrator.trackers.linear import client, worker
from tests.session import harnesses
from tests.utils.case import REPO, ROOTS, RUN_WITH, SRC, TreeCase, foreign
from tests.utils.sandbox import clean_env

PYTHONPATH = os.pathsep.join(ROOTS)
PY = (sys.executable, "-P", "-s")
ORCH_ROOT = os.path.join(REPO, "src")
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"


class Case(TreeCase):
    _case_clear = True

    def _case_foreign(self) -> str | None:
        return _foreign()

    def _case_env(self) -> dict[str, str]:
        return {**clean_env(), **super()._case_env()}

    def _case_enter(self, stack) -> None:
        if ".unit." in self.id():
            stack.enter_context(mock.patch("subprocess.run", system_probe))
        log.use_logs(None)
        client.use()  # a test's renewal policy and failed renewals must not reach the next
        # a worker container's mounted Linear access must not replace a test's injected board
        stack.enter_context(
            mock.patch.object(worker, "DIRECTORY", os.path.join(self.home, "worker", "linear"))
        )
        workflow.DEFAULT.load()  # a test's own workflow must not reach the next
        self.addCleanup(harnesses.end, self.home)


def _foreign(modules=None) -> str | None:
    return foreign(ensemble, ROOTS, SRC, RUN_WITH, modules)


def zone(case, tz="UTC0"):
    case.addCleanup(time.tzset)
    case.enterContext(mock.patch.dict(os.environ, {"TZ": tz}))
    time.tzset()


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)


def system_probe(argv, *_args, **_kwargs):
    if argv == ["getconf", "DARWIN_USER_TEMP_DIR"]:
        return SimpleNamespace(stdout="")
    if "rev-parse" in argv and "--show-toplevel" in argv:
        cwd = argv[argv.index("-C") + 1] if "-C" in argv else _kwargs.get("cwd", REPO)
        inside = os.path.commonpath(
            (os.path.realpath(cwd), os.path.realpath(REPO))
        ) == os.path.realpath(REPO)
        return subprocess.CompletedProcess(
            argv,
            0 if inside else 128,
            stdout="true\n" + REPO + "\n" if inside else "",
            stderr=""
            if inside
            else "fatal: not a git repository (or any of the parent directories): .git",
        )
    raise AssertionError(f"unexpected subprocess: {argv}")
