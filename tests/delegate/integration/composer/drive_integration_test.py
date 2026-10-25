"""A headless session's prompt and access intent, composed in a child process (child.Case) as its
caller would for a new run and a resume.
"""

import os
import unittest

from ensemble.delegate.composer.prompt import RESUME
from tests.delegate.case import REPO
from tests.delegate.integration.composer import child

TASKS = os.path.join(REPO, "src", "ensemble", "delegate", "team", "tasks")


class NewRun(child.Case):
    def test_a_new_runs_prompt_and_intent(self):
        got = self.compose("dummy-tester", cwd=self.proj)
        self.assertFalse(got["prompt"].startswith(RESUME))
        self.assertTrue(got["prompt"].endswith("\n# Input\n\nHello.\n"))
        self.assertEqual(
            got["trusted"], [], "no trusted_dirs: the caller loads no project settings"
        )
        self.assertEqual(
            got["intent"],
            {
                "dirs": [self.work],
                "reads": [TASKS],
                "writes": [],
                "commands": [],
                "gate": "",
                "env": {},
                "cwd": self.proj,
                "project": False,
            },
        )
        self.assertEqual(os.listdir(self.work), [])


class Resume(child.Case):
    def test_the_resume_prompt(self):
        got = self.compose("dummy-tester", input="Use B.", cwd=self.proj, resume=True)
        self.assertTrue(got["prompt"].startswith(RESUME))
        self.assertTrue(got["prompt"].startswith("Resumed agent run"))
        self.assertTrue(got["prompt"].endswith("\n# Input\n\nUse B.\n"))


if __name__ == "__main__":
    unittest.main()
