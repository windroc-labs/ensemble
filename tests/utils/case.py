"""
Case, every utils test's base; this tree's import roots (REPO, SRC, ROOTS) for its foreign check.
"""

import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import ensemble
from tests.utils.sandbox import project_roots

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = project_roots(REPO)
SRC = {
    name: root
    for root in ROOTS
    for name in os.listdir(os.path.join(root, "ensemble"))
    if os.path.isfile(os.path.join(root, "ensemble", name, "__init__.py"))
}
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)


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


class Case(unittest.TestCase):
    """Fails a test whose ensemble is not exactly this tree's, else runs it (whatever its setUp) in
    a fresh temp HOME, self.home (self.ensemble: its .ensemble).
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
            mock.patch.dict(os.environ, {"HOME": self.home, "ENSEMBLE_CONTROL_ROOT": self.home}),
        ):
            self.ensemble = os.path.join(self.home, ".ensemble")
            os.mkdir(self.ensemble)
            return super().run(result)


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
