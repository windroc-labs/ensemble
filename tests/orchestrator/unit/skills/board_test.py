"""skills/board/SKILL.md: every issue command it shows is one
ensemble-orchestrator issue accepts, it shows every one, it pre-approves none, a local board's
promote is its one other command, and only its Linear section names Linear.
"""

import contextlib
import io

from ensemble.orchestrator.issue import command as issue
from tests.orchestrator.case import Case
from tests.orchestrator.skills.board import body, examples, front, rules


def _commands(parser):
    """Every leaf command of parser, as its words after `issue`."""
    found = []
    for action in parser._subparsers._group_actions:
        for name, child in action.choices.items():
            leaves = _commands(child) if child._subparsers else [[]]
            found += [[name, *leaf] for leaf in leaves]
    return found


class BoardSkill(Case):
    def test_every_example_parses_without_a_usage_error(self):
        self.assertGreater(len(examples()), 15)
        for argv in examples():
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                args = issue._parser().parse_args(argv)
                self.assertIsNone(issue._usage(args))

    def test_it_shows_every_issue_command(self):
        shown = {tuple(argv[: len(c)]) for argv in examples() for c in _commands(issue._parser())}
        for command in _commands(issue._parser()):
            self.assertIn(tuple(command), shown, command)

    def test_it_pre_approves_nothing_since_even_reads_write_with_output(self):
        self.assertEqual(rules(), [])
        self.assertNotIn("allowed-tools", front())

    def test_it_states_the_relation_direction_partial_create_and_archive_rules(self):
        text = body()
        for phrase in (
            "--blocks B` is `ensemble-orchestrator issue relation add B --blocked-by A",
            "recover",
            "never run `create` again",
            "--full --output",
            "--body-file -",
            "completed, canceled or duplicate",
            "`create --parent`",
        ):
            self.assertIn(phrase, text)

    def test_an_unset_task_lets_the_role_pick_and_examples_name_no_configured_task(self):
        text = body()
        for phrase in (
            "An issue without a task label: its role's run picks a task from its own list",
            "its default when none fits better (a researcher's: `research`)",
            "`ensemble-crew start --list` gives each role's tasks and when each is chosen",
            "only to force",
        ):
            self.assertIn(phrase, text)
        tasks = [
            argv[i + 1] for argv in examples() for i, word in enumerate(argv) if word == "--task"
        ]
        self.assertTrue(tasks)
        self.assertEqual(set(tasks), {"<task>"})

    def test_it_names_no_delegate_and_promote_lives_in_the_local_board(self):
        self.assertNotIn("ensemble-delegate", front() + body())
        local = body().split("\n## Local board\n", 1)[1].split("\n## ", 1)[0]
        for phrase in (
            "No job runs promote on a local board",
            "`ensemble-orchestrator promote` (it prunes too)",
            "once an approved issue's 10-minute undo window has passed, or when the user asks",
        ):
            self.assertIn(phrase, local)
        self.assertIn("promote", front())
        self.assertIn("`ensemble-orchestrator promote`", body().split("\n## ", 1)[0])
        self.assertIn("${CLAUDE_SKILL_DIR}/../../bin/<command>", body())

    def test_only_its_linear_section_names_linear(self):
        general, linear = body().split("\n## Linear\n", 1)
        self.assertNotIn("linear", (front() + general).lower())
        self.assertNotIn("\n## ", linear)
        self.assertIn("lookup", linear)

    def test_the_manager_can_load_it(self):
        self.assertIn("name: board\n", front())
        self.assertNotIn("disable-model-invocation", front())
