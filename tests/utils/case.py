"""TreeCase, each case.py's Case base; Case, every utils test's base; import roots (REPO, SRC,
ROOTS). Imports without ensemble.
"""

import contextlib
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

from tests.utils.sandbox import project_roots

try:
    import ensemble
except ImportError:
    ensemble = None

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def packages(roots) -> dict[str, str]:
    return {
        name: root
        for root in roots
        for name in os.listdir(os.path.join(root, "ensemble"))
        if os.path.isfile(os.path.join(root, "ensemble", name, "__init__.py"))
    }


def run_with(roots) -> str:
    return os.pathsep.join(os.path.relpath(root, REPO) for root in roots)


ROOTS = project_roots(REPO)
SRC = packages(ROOTS)
RUN_WITH = run_with(ROOTS)


def local_section(text: str, section: str) -> str:
    """Place a layer's TOML fixture fragment under its local section."""
    header = re.search(r"(?m)^\[", text)
    top = text[: header.start()] if header else text
    prefix = (
        f"[{section}]\n"
        if any(line.strip() and not line.lstrip().startswith("#") for line in top.splitlines())
        else ""
    )
    return prefix + re.sub(r"(?m)^(\[\[?)", rf"\1{section}.", text)


def foreign(package, roots, src, run_with, modules=None) -> str | None:
    if package is None:
        return f"ensemble is not importable: run with PYTHONPATH={run_with}"
    own = {os.path.realpath(os.path.join(r, "ensemble")) for r in roots}
    for path in package.__path__:
        if os.path.realpath(path) not in own:
            return f"ensemble.__path__ holds {path}, not only this tree's: run with PYTHONPATH={run_with}"
    if missing := own - {os.path.realpath(p) for p in package.__path__}:
        return (
            f"ensemble.__path__ lacks {', '.join(sorted(missing))}: run with PYTHONPATH={run_with}"
        )
    for name, module in list((sys.modules if modules is None else modules).items()):
        top, _, sub = name.partition(".")
        where = getattr(module, "__file__", None)
        if top == "ensemble" and sub and "." not in sub and where:
            found = os.path.dirname(os.path.dirname(where))
            found = os.path.dirname(found) if os.path.basename(where) == "__init__.py" else found
            root = src.get(sub)
            if root is None or os.path.realpath(found) != os.path.realpath(root):
                return (
                    f"{name} is imported from {found}, not this tree's {root}: "
                    f"run with PYTHONPATH={run_with}"
                )
    return None


class TreeCase(unittest.TestCase):
    """Fails a test _case_foreign() names, else runs it (whatever its setUp) in a fresh temp HOME,
    self.home (self.ensemble: its .ensemble), env _case_env() (clear: _case_clear), _case_enter.
    """

    _case_clear = False

    def _case_foreign(self) -> str | None:
        raise NotImplementedError

    def _case_env(self) -> dict[str, str]:
        return {"HOME": self.home, "ENSEMBLE_CONTROL_ROOT": self.home}

    def _case_enter(self, stack: contextlib.ExitStack) -> None:
        pass

    def run(self, result=None):
        if foreign := self._case_foreign():
            result = self.defaultTestResult() if result is None else result
            result.startTest(self)
            try:
                self.fail(foreign)
            except self.failureException:
                result.addFailure(self, sys.exc_info())
            result.stopTest(self)
            return result
        with tempfile.TemporaryDirectory() as self.home, contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(os.environ, self._case_env(), clear=self._case_clear)
            )
            self.ensemble = os.path.join(self.home, ".ensemble")
            os.mkdir(self.ensemble)
            self._case_enter(stack)
            return super().run(result)


class Case(TreeCase):
    def _case_foreign(self) -> str | None:
        return _foreign()


def _foreign(modules=None) -> str | None:
    return foreign(ensemble, ROOTS, SRC, RUN_WITH, modules)
