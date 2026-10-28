"""NFR-2: the session layer copied alone, ensemble/session and ensemble/utils in an otherwise empty
directory (no ensemble/__init__.py), imports from there and passes its unit tests against it.
"""

import json
import os
import shutil
import subprocess
import sys

from tests.session.case import REPO, SRC, Case
from tests.utils.sandbox import clean_env, scratch

IMPORT = """import json, ensemble, ensemble.session, ensemble.utils
print(json.dumps([list(ensemble.__path__), ensemble.session.__file__, ensemble.utils.__file__]))
"""


class SessionBundleTest(Case):
    def setUp(self):
        self.bundle = scratch(self)
        for package in ("session", "utils"):
            shutil.copytree(
                os.path.join(SRC[package], "ensemble", package),
                os.path.join(self.bundle, "ensemble", package),
                ignore=shutil.ignore_patterns("__pycache__"),
            )

    def run_python(self, *args, **env):
        return subprocess.run(
            [sys.executable, "-P", *args],
            cwd=REPO,
            env={**clean_env(None), "HOME": self.home, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=600,
        )

    def test_the_copy_imports_on_its_own(self):
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.bundle, "ensemble"))), ["session", "utils"]
        )
        result = self.run_python("-c", IMPORT, PYTHONPATH=self.bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        path, session, utils = json.loads(result.stdout)
        own = os.path.join(self.bundle, "ensemble")
        self.assertEqual(path, [own])
        self.assertTrue(session.startswith(own + os.sep), session)
        self.assertTrue(utils.startswith(own + os.sep), utils)

    def test_the_session_unit_tests_pass_against_the_copy(self):
        result = self.run_python(
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/session/unit",
            "-t",
            ".",
            "-p",
            "*_test.py",
            PYTHONPATH=os.pathsep.join((self.bundle, REPO)),
            ENSEMBLE_SESSION_BUNDLE=self.bundle,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        self.assertRegex(result.stderr, r"\nOK")
