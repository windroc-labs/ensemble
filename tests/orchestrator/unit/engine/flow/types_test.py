"""The issue names and messages both trackers' issue commands share."""

from ensemble.orchestrator.engine.flow import types
from ensemble.orchestrator.engine.flow.types import Priority
from tests.orchestrator.case import Case


class Priorities(Case):
    def test_numbers_by_name(self):
        self.assertEqual(
            dict(types.PRIORITY_NUMBERS),
            {
                Priority.NONE: 0,
                Priority.URGENT: 1,
                Priority.HIGH: 2,
                Priority.MEDIUM: 3,
                Priority.LOW: 4,
            },
        )


class Messages(Case):
    def test_ident_error_names_the_value_and_the_forms(self):
        self.assertEqual(
            types.IDENT_ERROR.format(ident="task 1"),
            "'task 1': want an issue identifier such as TASK-1 or an issue id",
        )

    def test_both_directions_names_the_issue(self):
        self.assertEqual(
            types.BOTH_DIRECTIONS.format(identifier="TASK-2"),
            "TASK-2: given to both --blocks and --blocked-by; "
            "an issue cannot both block the new one and be blocked by it",
        )
