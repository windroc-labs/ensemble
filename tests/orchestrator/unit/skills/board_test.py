"""skills/board/SKILL.md: every issue command it shows is one
ensemble-orchestrator issue accepts, it shows every one, it pre-approves none, and only its Linear
section names Linear.
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

    def test_only_its_linear_section_names_linear(self):
        general, linear = body().split("\n## Linear\n", 1)
        self.assertNotIn("linear", (front() + general).lower())
        self.assertNotIn("\n## ", linear)
        self.assertIn("lookup", linear)

    def test_the_commander_can_load_it(self):
        self.assertIn("name: board\n", front())
        self.assertNotIn("disable-model-invocation", front())
