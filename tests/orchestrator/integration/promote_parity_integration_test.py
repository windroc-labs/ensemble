"""v1 promote bounces, each against v1's src/promote.py (`v1-runtime`), end to end:
bin/ensemble-orchestrator promote as a real process (BinFlow) over FakeLinear. A Handoff issue promote
cannot hand on goes back to In Review with a comment saying why, and no next-stage issue is made.
"""

import unittest
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine.promote import GRACE, NO_INSTRUCTIONS
from ensemble.orchestrator.trackers.linear import ops
from tests.orchestrator.integration.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    HUMAN,
    PROJECT,
    BinFlow,
)

SOURCE, TITLE = "TASK-12", "Session registries in agent harnesses"
INSTRUCTIONS = "Write the PRD for a session registry."


class Bounce(BinFlow):
    def handoff(self, *, ago, instructions=None):
        """SOURCE, the researcher's, moved to Handoff `ago` before now, after the human's
        `instructions` comment if any.
        """
        now, researcher, human = (
            datetime.now(UTC),
            self.ids[ACCOUNTS["researcher"]],
            self.ids[HUMAN],
        )
        self.fake.issue(
            TITLE, identifier=SOURCE, state="in_progress", delegate=researcher, project=PROJECT
        )
        self.fake.move(SOURCE, "in_review", actor=researcher, at=now - ago - timedelta(minutes=20))
        if instructions:
            self.fake.comment(SOURCE, human, instructions, at=now - ago - timedelta(minutes=10))
        self.fake.move(SOURCE, "handoff", actor=human, at=now - ago)
        with self.fake.lock:
            source = self.fake.find(SOURCE)
            return ops.client_id(source["id"], "pm", source["history"][-1]["createdAt"])

    def promote_tick(self):
        res = self.promote()
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""), self.said())

    def assert_bounced(self, child, comments):
        self.assertEqual(self.state(SOURCE), "in_review")
        self.assertEqual(self.history(SOURCE)[-1], ("handoff", "in_review", HARNESS_EMAIL))
        self.assertEqual(self.subscribers(SOURCE), [HUMAN])
        with self.fake.lock:
            self.assertIsNone(self.fake.find(child))
            self.assertEqual(self.fake.relations, [])
        self.assertEqual(self.comments(SOURCE)[:-1], comments)
        author, body = self.comments(SOURCE)[-1]
        self.assertEqual(author, HARNESS_EMAIL)
        return body

    def test_a_handoff_without_instructions_goes_back_to_in_review(self):
        """v1 promote.py: no user's or manager's comment since the last In Review, the role's
        require_instructions set → `handoff-bounce`: NO_INSTRUCTIONS, back to In Review.
        """
        child = self.handoff(ago=timedelta(minutes=20))
        self.promote_tick()
        body = self.assert_bounced(child, [])
        self.assertTrue(body.startswith(NO_INSTRUCTIONS.format(state="In Review")), body)
        self.assertEqual(
            self.said(), [f"promote handoff-bounce {SOURCE} to=in_review reason=no instructions"]
        )
        self.assertEqual(self.sent("promote.M_CREATE"), [])

    def test_a_handoff_failing_past_its_grace_goes_back_to_in_review(self):
        """v1 promote.py: a promotion that fails over GRACE after the move to Handoff →
        `handoff-failed`: a `Handoff failed:` comment, back to In Review.
        """
        child = self.handoff(ago=GRACE + timedelta(hours=1), instructions=INSTRUCTIONS)
        self.fake.fail("promote.M_CREATE", "error", account=HARNESS_EMAIL)
        self.promote_tick()
        body = self.assert_bounced(child, [(HUMAN, INSTRUCTIONS)])
        self.assertTrue(body.startswith("Handoff failed: "), body)
        self.assertIn("promote.M_CREATE: injected error", body)
        said = [line.split(" error=")[0] for line in self.said()]
        self.assertEqual(
            said,
            [f"promote handoff-error {SOURCE}", f"promote handoff-failed {SOURCE} to=in_review"],
        )


if __name__ == "__main__":
    unittest.main()
