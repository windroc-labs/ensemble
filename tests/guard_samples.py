"""Independent sample trees and discovery helpers for guard tests."""

import ast
import inspect
import os
import tempfile
import unittest
from pathlib import Path

from tests import guards
from tests.case import REPO, Case

PKG = "src/ensemble"
FIXTURES = ("setUpClass", "tearDownClass", "setUpModule", "tearDownModule")


def fixtures(root: str) -> list[str]:
    return sorted(
        f"{path.relative_to(root)}:{node.lineno}: {node.name}"
        for path in Path(root).rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in FIXTURES
    )


def flat(suite):
    for test in suite:
        yield from flat(test) if isinstance(test, unittest.TestSuite) else (test,)


def own_case(test, top):
    project = top.split("/")[0]
    trees = (
        [top]
        if project not in guards.LAYERS
        else [f"{p}/tests" for p in guards.LAYERS[: guards.LAYERS.index(project) + 1]]
    )
    roots = [os.path.realpath(os.path.join(REPO, tree)) for tree in trees]
    return any(
        c.__name__ == "Case"
        and any(os.path.commonpath([root, inspect.getfile(c)]) == root for root in roots)
        for c in type(test).__mro__
    )


class Sample(Case):
    def wrap_orchestrator(self, root):
        return self.tree(
            {
                f"orchestrator/{p.relative_to(root)}": p.read_text()
                for p in Path(root).rglob("*")
                if p.is_file()
            }
        )

    def tree(self, files: dict[str, str]) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for rel, text in files.items():
            path = os.path.join(tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return tmp.name


def test_suites(root, top):
    base = Path(root, top)
    owners = [base, *(p for p in sorted(base.iterdir()) if (p / "__init__.py").is_file())]
    return [
        (str(owner / kind), root if owner == base else str(base))
        for owner in owners
        for kind in ("unit", "integration")
        if (owner / kind / "__init__.py").is_file()
    ]
