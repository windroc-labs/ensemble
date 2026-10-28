"""NFR-2: the session layer copied alone, ensemble/session and ensemble/utils in an otherwise empty
directory (no ensemble/__init__.py), imports from there and passes its unit tests against it.
"""

import os
import subprocess
import sys

from tests.session.case import REPO, Case
from tests.session.integration.support import site_copy
from tests.utils.sandbox import clean_env, scratch


class SessionBundleTest(Case):
    def setUp(self):
        self.bundle = site_copy(scratch(self))

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
