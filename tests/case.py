"""Case, the repo-wide checks' base; child helpers (PY, child_env, ORCH_ROOT); this tree's import
roots (REPO, SRC, ROOTS).
"""

import os
import sys

from tests.utils.case import ROOTS, RUN_WITH, SRC, TreeCase, foreign
from tests.utils.sandbox import clean_env

try:
    import ensemble
except ImportError:
    ensemble = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (sys.executable, "-P", "-S")
ORCH_ROOT = os.path.join(REPO, "src")


class Case(TreeCase):
    def _case_foreign(self) -> str | None:
        return _foreign()


def _missing() -> str | None:
    return None if ensemble else foreign(None, ROOTS, SRC, RUN_WITH)


def _foreign(modules=None) -> str | None:
    return foreign(ensemble, ROOTS, SRC, RUN_WITH, modules)


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)
