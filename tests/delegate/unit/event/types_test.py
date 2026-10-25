"""delegate/event/types.py: STATUSES uses the utils outcome vocabulary."""

from ensemble.delegate.event.types import STATUSES
from ensemble.utils import OutcomeStatus
from tests.delegate.case import Case


class Statuses(Case):
    def test_outcome_status_is_a_str_enum_of_the_three_statuses(self):
        self.assertEqual(list(OutcomeStatus), ["done", "needs_input", "failed"])
        self.assertEqual(STATUSES, tuple(OutcomeStatus))
        self.assertEqual(STATUSES, ("done", "needs_input", "failed"))
