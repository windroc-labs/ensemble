"""Every module of ensemble (the five import roots) and tests imported alone in a fresh child, an
empty temp dir its HOME and cwd, with only the import roots of its layer and the lower ones (PATHS;
tests: all of them): it exits 0, leaving the dir empty and sys.path unchanged.
"""

import os
import subprocess
import sysconfig
import tempfile
import unittest

from tests.case import PY, REPO, ROOTS, SRC, Case, child_env
from tests.guards import LAYERS, PROJECTS

PATHS = {
    **{
        f"ensemble.{PROJECTS[p]}": tuple(SRC[PROJECTS[q]] for q in LAYERS[: i + 1])
        for i, p in enumerate(LAYERS)
    },
    "tests": (
        *ROOTS,
        REPO,
        os.path.join(REPO, ".github"),
        sysconfig.get_path("purelib"),
        sysconfig.get_path("platlib"),
    ),
}
CHILD = """import importlib, sys
path = list(sys.path)
importlib.import_module(sys.argv[1])
sys.exit(sys.path != path and f"sys.path {path} became {sys.path}")
"""


def modules(root: str, package: str) -> list[str]:
    found = []
    for top, dirs, files in os.walk(os.path.join(root, *package.split("."))):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(f for f in files if f.endswith(".py")):
            parts = os.path.relpath(os.path.join(top, name), root)[:-3].split(os.sep)
            found.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return found


def paths(name: str) -> tuple[str, ...]:
    return PATHS[max((p for p in PATHS if f"{name}.".startswith(f"{p}.")), key=len)]


class ImportsAlone(Case):
    def _check_modules(self, group):
        names = [
            n for sub, root in SRC.items() for n in modules(root, f"ensemble.{sub}")
        ] + modules(REPO, "tests")
        self.assertIn("ensemble.session.engine.hosted_session", names)
        self.assertIn("ensemble.orchestrator.engine.router", names)
        self.assertIn("tests.case", names)
        for name in names[group::4]:
            with self.subTest(name), tempfile.TemporaryDirectory() as home:
                res = subprocess.run(
                    [*PY, "-c", CHILD, name],
                    cwd=home,
                    env={**child_env(), "PYTHONPATH": os.pathsep.join(paths(name)), "HOME": home},
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual((res.returncode, os.listdir(home)), (0, []), res.stderr)

    def test_each_module_1(self):
        self._check_modules(0)

    def test_each_module_2(self):
        self._check_modules(1)

    def test_each_module_3(self):
        self._check_modules(2)

    def test_each_module_4(self):
        self._check_modules(3)


if __name__ == "__main__":
    unittest.main()
