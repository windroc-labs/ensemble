"""delegate/skills/act-as/SKILL.md as Claude Code runs it: its pre-approved rule is step 1's
command, the plugin's bin/ command listing the team; step 3 runs the same command, and step 4 puts a
PYTHONPATH of the delegate layer and the layers below it before each team tool command of the
prompt.
"""

import os
import re

from delegate.tests.case import REPO, SRC, Case
from delegate.tests.skills.act_as import SKILL_DIR, argv, command, head, prefix, text

RULE = re.compile(r"^allowed-tools:\n  - Bash\((.+)\)$", re.M)


class ActAs(Case):
    def test_the_rule_is_step_1s_command_and_step_3_runs_its_command(self):
        self.assertEqual(RULE.search(text())[1], command(1))
        self.assertEqual(command(1), "ensemble-delegate run --list")
        self.assertEqual(head(), "ensemble-delegate")

    def test_step_4s_prefix_holds_the_delegate_layer_and_those_below_only(self):
        [assignment] = argv(prefix())[1:]
        roots = assignment.removeprefix("PYTHONPATH=").split(":")
        self.assertEqual(argv(prefix())[0], "env")
        self.assertEqual(
            [os.path.realpath(r) for r in roots],
            [os.path.realpath(SRC[name]) for name in ("delegate", "session", "utils")],
        )

    def test_step_3_requests_instruction_without_client_options(self):
        self.assertIn("ensemble-delegate prompt --role", command(3))
        for option in ("--client", "--mode", "--detach", "--sid", "--resume"):
            self.assertNotIn(option, command(3))

    def test_a_client_without_bin_on_its_path_runs_this_plugins_command_by_its_path(self):
        fallback = "${CLAUDE_SKILL_DIR}/../../../bin/ensemble-delegate"
        self.assertIn(f"`{fallback}`", text())
        own = fallback.replace("${CLAUDE_SKILL_DIR}", SKILL_DIR)
        self.assertEqual(os.path.realpath(own), os.path.join(os.path.realpath(REPO), "bin", head()))
        self.assertTrue(os.access(own, os.X_OK))
