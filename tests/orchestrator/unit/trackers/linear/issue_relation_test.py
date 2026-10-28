"""ensemble-orchestrator issue relation: one blocking edge, the same from either end, added or removed
once, every other relation left alone.
"""

from ensemble.orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LinearError,
    Unavailable,
    WriteUnknown,
)
from ensemble.orchestrator.issue.output import receipt_payload
from ensemble.orchestrator.trackers.linear import issue_read, issue_relation
from tests.orchestrator.case import Case
from tests.orchestrator.unit.issue_fixtures import RelationGql, SecondFails, receipt_of, uid
from tests.utils.process_fixtures import TimeoutExpired

BOARD = ("TASK-1", "TASK-2", "TASK-3")
# the same edge, TASK-1 blocking TASK-2, named from either end
SAME_EDGE = (("TASK-1", "blocks", "TASK-2"), ("TASK-2", "blocked_by", "TASK-1"))
OTHERS = (["r-back", "blocks", "TASK-2", "TASK-1"], ["r-rel", "related", "TASK-1", "TASK-2"])


class Add(Case):
    def test_either_end_creates_the_one_edge_and_prints_the_receipt(self):
        for ident, direction, other in SAME_EDGE:
            gql = RelationGql(BOARD)
            with self.subTest(direction=direction):
                receipt = issue_relation.relation(gql, "add", ident, direction, other)
                self.assertEqual(
                    gql.writes,
                    [(issue_relation.M_ADD, {"i": "id-TASK-1", "r": "id-TASK-2"})],
                )
                self.assertEqual(receipt_payload(receipt), receipt_of("relation add", ident))

    def test_an_existing_edge_from_either_end_is_unchanged(self):
        for ident, direction, other in SAME_EDGE:
            gql = RelationGql(BOARD, [["r-1", "blocks", "TASK-1", "TASK-2"]])
            with self.subTest(direction=direction):
                receipt = issue_relation.relation(gql, "add", ident, direction, other)
                self.assertEqual((receipt.result, gql.writes), ("unchanged", []))

    def test_the_reverse_edge_or_another_type_is_not_this_edge(self):
        gql = RelationGql(BOARD, OTHERS)
        issue_relation.relation(gql, "add", "TASK-1", "blocks", "TASK-2")
        self.assertEqual(len(gql.writes), 1)
        self.assertEqual(gql.relations[:2], [list(r) for r in OTHERS])
        self.assertEqual(gql.relations[2][1:], ["blocks", "TASK-1", "TASK-2"])

    def test_later_relation_pages_are_read(self):
        filler = [[f"r-{n}", "related", "TASK-1", "TASK-3"] for n in range(issue_read.PAGE)]
        gql = RelationGql(BOARD, [*filler, ["r-last", "blocks", "TASK-1", "TASK-2"]])
        receipt = issue_relation.relation(gql, "add", "TASK-1", "blocks", "TASK-2")
        self.assertEqual((receipt.result, gql.writes), ("unchanged", []))
        self.assertIn(
            (issue_relation.Q_MORE["blocks"], {"i": "id-TASK-1", "c": str(issue_read.PAGE)}),
            gql.calls,
        )


class Remove(Case):
    def test_either_end_deletes_the_one_edge_and_nothing_else(self):
        for ident, direction, other in SAME_EDGE:
            gql = RelationGql(BOARD, [*OTHERS, ["r-1", "blocks", "TASK-1", "TASK-2"]])
            with self.subTest(direction=direction):
                receipt = issue_relation.relation(gql, "remove", ident, direction, other)
                self.assertEqual(gql.writes, [(issue_relation.M_REMOVE, {"i": "r-1"})])
                self.assertEqual(gql.relations, [list(r) for r in OTHERS])
                self.assertEqual(
                    (receipt.operation, receipt.result), ("relation remove", "changed")
                )

    def test_a_missing_edge_is_unchanged_and_the_reverse_one_stays(self):
        for ident, direction, other in SAME_EDGE:
            gql = RelationGql(BOARD, OTHERS)
            with self.subTest(direction=direction):
                receipt = issue_relation.relation(gql, "remove", ident, direction, other)
                self.assertEqual((receipt.result, gql.writes), ("unchanged", []))
                self.assertEqual(gql.relations, [list(r) for r in OTHERS])

    def test_every_record_of_the_edge_goes(self):
        twice = [["r-1", "blocks", "TASK-1", "TASK-2"], ["r-2", "blocks", "TASK-1", "TASK-2"]]
        gql = RelationGql(BOARD, twice)
        issue_relation.relation(gql, "remove", "TASK-2", "blocked_by", "TASK-1")
        self.assertEqual([v["i"] for _, v in gql.writes], ["r-1", "r-2"])
        self.assertEqual(gql.relations, [])

    def test_a_later_record_failing_says_which_ones_went(self):
        twice = [["r-1", "blocks", "TASK-1", "TASK-2"], ["r-2", "blocks", "TASK-1", "TASK-2"]]
        cases = (
            ({"success": False}, "issueRelationDelete: success: false"),
            (GraphQLErrors([{"message": "boom"}]), "boom"),
            (Unavailable("issueRelation", reason="timed out"), "result unknown"),
            (TimeoutExpired("security", 5), "TimeoutExpired"),
            (PermissionError("denied"), "PermissionError"),
        )
        for result, cause in cases:
            gql = SecondFails(BOARD, twice, later=result)
            with self.subTest(cause=cause), self.assertRaises(LinearError) as cm:
                issue_relation.relation(gql, "remove", "TASK-1", "blocks", "TASK-2")
            text = str(cm.exception)
            for phrase in ("relation remove TASK-1", "deleted 1 of 2", "r-1", cause, "read TASK-1"):
                self.assertIn(phrase, text)
            self.assertEqual(gql.relations, [twice[1]])

    def test_the_first_record_failing_is_the_plain_error(self):
        twice = [["r-1", "blocks", "TASK-1", "TASK-2"], ["r-2", "blocks", "TASK-1", "TASK-2"]]
        gql = RelationGql(BOARD, twice, result={"success": False})
        with self.assertRaises(LinearError) as cm:
            issue_relation.relation(gql, "remove", "TASK-1", "blocks", "TASK-2")
        self.assertEqual(str(cm.exception), "issueRelationDelete: success: false")
        self.assertEqual(len(gql.writes), 1)


class Refused(Case):
    def test_an_issue_never_relates_to_itself(self):
        for other in ("TASK-1", uid(1)):
            for direction in issue_relation.SIDES:
                gql = RelationGql(BOARD, ids={"TASK-1": uid(1)})
                with self.subTest(other=other, direction=direction):
                    with self.assertRaises(IssueError) as cm:
                        issue_relation.relation(gql, "add", "TASK-1", direction, other)
                    self.assertIn("itself", str(cm.exception))
                    self.assertEqual(gql.writes, [])

    def test_an_unresolved_issue_writes_nothing(self):
        cases = (
            ("TASK-9", "TASK-2", "blocks", "TASK-9: no such issue"),
            ("TASK-1", "TASK-9", "blocks", "--blocks TASK-9: no such issue"),
            ("TASK-1", "task-2", "blocked_by", "--blocked-by 'task-2'"),
        )
        for ident, other, direction, phrase in cases:
            gql = RelationGql(BOARD)
            with self.subTest(phrase=phrase), self.assertRaises(IssueError) as cm:
                issue_relation.relation(gql, "add", ident, direction, other)
            self.assertIn(phrase, str(cm.exception))
            self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_never_retried(self):
        for op, edges in (("add", []), ("remove", [["r-1", "blocks", "TASK-1", "TASK-2"]])):
            gql = RelationGql(BOARD, edges, result=Unavailable("issueRelation", reason="timed out"))
            with self.subTest(op=op), self.assertRaises(WriteUnknown) as cm:
                issue_relation.relation(gql, op, "TASK-1", "blocks", "TASK-2")
            text = str(cm.exception)
            for phrase in (f"relation {op} TASK-1", "result unknown", "issue read TASK-1"):
                self.assertIn(phrase, text)
            self.assertEqual(len(gql.writes), 1)

    def test_a_refused_write_fails_and_is_not_unknown(self):
        for result in ({"success": False}, GraphQLErrors([{"message": "boom"}])):
            gql = RelationGql(BOARD, result=result)
            with self.subTest(result=result), self.assertRaises(LinearError) as cm:
                issue_relation.relation(gql, "add", "TASK-1", "blocks", "TASK-2")
            self.assertNotIsInstance(cm.exception, WriteUnknown)
            self.assertEqual(len(gql.writes), 1)
