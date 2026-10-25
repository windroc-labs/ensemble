"""The prompts of a resume, composed in a child process (child.Case): an answer to needs_input, a
run whose role picks its task, a draft left at the deliverable's path.
"""

import os
import re
import unittest

from ensemble.delegate.composer.prompt import RESUME
from tests.delegate.case import REPO
from tests.delegate.integration.composer import child

TEAM = os.path.join(REPO, "src", "ensemble", "delegate", "team")
ANSWER = "Say hello."
PICK = "**Your task**: pick it from your charter's Tasks section"


class NeedsInput(child.Case):
    def test_the_answer_is_the_resume_prompts_input(self):
        first = self.compose("dummy-tester")["prompt"]
        second = self.compose("dummy-tester", input=ANSWER, resume=True)["prompt"]
        self.assertFalse(first.startswith(RESUME), "the first prompt")
        self.assertTrue(second.startswith(RESUME), "the resume prompt")
        self.assertTrue(second.endswith(f"\n# Input\n\n{ANSWER}\n"), "the answer as the input")


class RolePicks(child.Case):
    ROLE = "researcher"

    def setUp(self):
        super().setUp()
        with open(os.path.join(TEAM, "roles", f"{self.ROLE}.md"), encoding="utf-8") as f:
            self.index = re.search(r"^## Tasks\n.*?(?=^## |\Z)", f.read(), re.M | re.S)[0].strip()
        self.tasks = re.findall(r"^- `([\w-]+)` \(`<tasks>/", self.index, re.M)

    def picks(self, text: str) -> None:
        guide = text.split("\n# Principles\n", 1)[0]
        self.assertIn(PICK, guide)
        self.assertEqual([t for t in self.tasks if f"`{t}`" in guide], [], "tasks the guide names")

    def test_the_role_picks_and_its_resume_prompt_too(self):
        self.assertEqual(self.tasks, ["deep-research", "research", "light-research"])
        text = self.compose(self.ROLE)["prompt"]
        self.assertIn(self.index, text, "the task index")
        for t in self.tasks:
            with open(os.path.join(TEAM, "tasks", f"{t}.md"), encoding="utf-8") as f:
                for line in f:
                    if len(line.strip()) >= 30 and not line.startswith("#"):
                        self.assertNotIn(line.strip(), text, f"a line of {t}'s steps")
        self.picks(text)
        resumed = self.compose(self.ROLE, resume=True)["prompt"]
        self.assertTrue(resumed.startswith(RESUME), "the resume prompt")
        self.picks(resumed)


class Draft(child.Case):
    def test_a_new_run_and_its_resume_name_the_same_deliverable_file(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                prompt = self.compose("dummy-tester", resume=resume)["prompt"]
                self.assertIn(f"\n- `<Workdir>`: `{self.work}`\n", prompt)
                self.assertIn(
                    (
                        "\nWrite the deliverable to `<Workdir>/out.md` and name that file in the outcome's "
                        "`deliverable_file`;"
                    ),
                    prompt,
                )


if __name__ == "__main__":
    unittest.main()
