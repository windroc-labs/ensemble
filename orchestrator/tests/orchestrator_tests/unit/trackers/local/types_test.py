"""LocalState: the on-disk state dir names."""

from ensemble.orchestrator.trackers.local import types
from ensemble.orchestrator.trackers.local.types import LocalState
from orchestrator_tests.case import Case


class LocalStates(Case):
    def test_state_dirs_are_literals(self):
        """Not IssueState's values: renaming a state never renames a dir."""
        self.assertEqual(
            sorted(LocalState),
            ["approved", "backlog", "canceled", "done", "ready", "review", "running"],
        )
        self.assertNotIn("IssueState", vars(types))
