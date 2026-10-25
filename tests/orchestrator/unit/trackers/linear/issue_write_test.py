"""ensemble-orchestrator issue move and comment: an abstract state as the configured team's state id,
the body read verbatim, one write, the receipt.
"""

from ensemble.orchestrator.engine.flow.types import IssueState
from ensemble.orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LinearError,
    Unavailable,
    WriteUnknown,
)
from ensemble.orchestrator.issue.output import receipt_payload
from ensemble.orchestrator.trackers.linear import issue_write, ops
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import STATES
from tests.orchestrator.unit.issue_fixtures import (
    OTHER_TEAM,
    VIEWER,
    WriteGql,
    names_cfg,
    write_issue,
)

BODY = 'say "hi", `ls`, $HOME and ${x} \\n\nline two\n中文正文\n'
KEYS = {
    IssueState.BACKLOG: "backlog",
    IssueState.READY: "todo",
    IssueState.RUNNING: "in_progress",
    IssueState.REVIEW: "in_review",
    IssueState.APPROVED: "handoff",
    IssueState.DONE: "done",
    IssueState.CANCELED: "canceled",
}


def move(gql, state, ident="TASK-2"):
    return issue_write.move(gql, names_cfg(), ident, state)


class Move(Case):
    def test_each_abstract_state_writes_its_configured_id(self):
        for state, key in KEYS.items():
            gql = WriteGql(write_issue(state=STATES["backlog" if key != "backlog" else "todo"]))
            with self.subTest(state=state):
                receipt = move(gql, state)
                self.assertEqual(gql.writes, [(ops.M_STATE, {"i": "id-TASK-2", "s": STATES[key]})])
                self.assertEqual(
                    receipt_payload(receipt),
                    {
                        "issue": {
                            "id": "id-TASK-2",
                            "identifier": "TASK-2",
                            "url": "https://linear.app/t/issue/TASK-2",
                        },
                        "operation": "move",
                        "result": "changed",
                        "actor": VIEWER,
                    },
                )

    def test_already_in_the_state_is_unchanged_and_writes_nothing(self):
        gql = WriteGql(write_issue(state=STATES["handoff"]))
        receipt = move(gql, IssueState.APPROVED)
        self.assertEqual((receipt.result, gql.writes), ("unchanged", []))
        self.assertEqual(receipt_payload(receipt)["actor"], VIEWER)

    def test_an_issue_of_another_team_writes_nothing(self):
        gql = WriteGql(write_issue("OPS-3", team_id=OTHER_TEAM))
        with self.assertRaises(IssueError) as cm:
            move(gql, IssueState.DONE, "OPS-3")
        text = str(cm.exception)
        for phrase in ("OPS-3", "team OPS", "move", names_cfg().team):
            self.assertIn(phrase, text)
        self.assertEqual(gql.writes, [])

    def test_the_read_asks_for_the_team_and_state_ids(self):
        gql = WriteGql(write_issue())
        move(gql, IssueState.DONE)
        query, variables = gql.calls[0]
        self.assertEqual((query, variables), (issue_write.Q_MOVE, {"i": "TASK-2"}))
        self.assertIn("team { id key }", query)
        self.assertIn("state { id }", query)
        self.assertNotIn("states", query)

    def test_no_such_issue_writes_nothing(self):
        gql = WriteGql(None)
        with self.assertRaises(IssueError) as cm:
            move(gql, IssueState.DONE)
        self.assertIn("no such issue", str(cm.exception))
        self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_never_retried(self):
        gql = WriteGql(write_issue(), result=Unavailable("issueUpdate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            move(gql, IssueState.DONE)
        text = str(cm.exception)
        for phrase in ("move TASK-2", "result unknown", "timed out", "issue read TASK-2"):
            self.assertIn(phrase, text)
        self.assertEqual(len(gql.writes), 1)

    def test_a_refused_write_fails_and_is_not_unknown(self):
        for result in ({"success": False}, GraphQLErrors([{"message": "boom"}])):
            gql = WriteGql(write_issue(), result=result)
            with self.subTest(result=result), self.assertRaises(LinearError) as cm:
                move(gql, IssueState.DONE)
            self.assertNotIsInstance(cm.exception, WriteUnknown)
            self.assertEqual(len(gql.writes), 1)


class Comment(Case):
    def test_posts_the_body_verbatim_as_a_variable(self):
        gql = WriteGql(write_issue())
        receipt = issue_write.comment(gql, "TASK-2", BODY)
        self.assertEqual(gql.writes, [(issue_write.M_COMMENT, {"i": "id-TASK-2", "b": BODY})])
        self.assertEqual(
            receipt_payload(receipt),
            {
                "issue": {
                    "id": "id-TASK-2",
                    "identifier": "TASK-2",
                    "url": "https://linear.app/t/issue/TASK-2",
                },
                "operation": "comment",
                "result": "changed",
                "actor": VIEWER,
                "comment_id": "c-new",
            },
        )

    def test_no_such_issue_posts_nothing(self):
        gql = WriteGql(None)
        with self.assertRaises(IssueError):
            issue_write.comment(gql, "TASK-2", BODY)
        self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_never_retried(self):
        gql = WriteGql(write_issue(), result=Unavailable("commentCreate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            issue_write.comment(gql, "TASK-2", BODY)
        self.assertIn("comment TASK-2: result unknown", str(cm.exception))
        self.assertNotIn("中文", str(cm.exception))
        self.assertEqual(len(gql.writes), 1)

    def test_a_refused_write_fails_and_is_not_unknown(self):
        for result in ({"success": False}, GraphQLErrors([{"message": "boom"}])):
            gql = WriteGql(write_issue(), result=result)
            with self.subTest(result=result), self.assertRaises(LinearError) as cm:
                issue_write.comment(gql, "TASK-2", BODY)
            self.assertNotIsInstance(cm.exception, WriteUnknown)
            self.assertEqual(len(gql.writes), 1)
