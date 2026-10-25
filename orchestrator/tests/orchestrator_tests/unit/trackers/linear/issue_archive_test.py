"""ensemble-orchestrator issue archive: every unarchived descendant read over every page at every level,
the archive only when all are finished, nothing written after a failed read; the plan alone for a
dry run.
"""

from ensemble.orchestrator.engine.flow.types import ArchiveRefused
from ensemble.orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LinearError,
    Unavailable,
    WriteUnknown,
)
from ensemble.orchestrator.issue.output import receipt_payload, refusal
from ensemble.orchestrator.trackers.linear import issue_archive, linear_tracker
from ensemble.orchestrator.trackers.linear.linear_tracker import LinearTracker
from orchestrator_tests.case import Case
from orchestrator_tests.unit.issue_fixtures import VIEWER, ArchiveGql, tree


def many(n, prefix="SUB", kind="completed"):
    return [tree(f"{prefix}-{i}", kind) for i in range(1, n + 1)]


def archive(gql, ident="TASK-1"):
    return issue_archive.archive(LinearTracker(gql), gql, ident)


def plan(gql, ident="TASK-1"):
    return issue_archive.plan(LinearTracker(gql), gql, ident)


class StuckGql(ArchiveGql):
    """Every children page claims another after it, at cursor (None: no cursor)."""

    def __init__(self, root, cursor):
        super().__init__(root)
        self.cursor = cursor

    def _page(self, node, offset):
        page = super()._page(node, 0)
        page["pageInfo"] = {"hasNextPage": True, "endCursor": self.cursor}
        return page


class GoneGql(ArchiveGql):
    """The issue the lookup found is gone when the archive read comes."""

    def _plan(self, key):
        return {"issue": None}


class Archive(Case):
    def test_all_finished_archives_once_and_prints_the_receipt(self):
        root = tree("TASK-1", "completed", tree("TASK-2", "canceled", tree("TASK-3")))
        gql = ArchiveGql(root)
        self.assertEqual(
            receipt_payload(archive(gql)),
            {
                "issue": {
                    "id": "id-TASK-1",
                    "identifier": "TASK-1",
                    "url": "https://linear.app/t/issue/TASK-1",
                },
                "operation": "archive",
                "result": "changed",
                "actor": VIEWER,
            },
        )
        self.assertEqual(gql.writes, [(linear_tracker.M_ARCHIVE, {"i": "id-TASK-1"})])
        self.assertEqual(gql.calls[0], (issue_archive.Q_ISSUE, {"i": "TASK-1"}))

    def test_a_duplicate_is_finished_as_prune_counts_it(self):
        for name, root in (
            ("issue", tree("TASK-1", "duplicate")),
            ("sub-issue", tree("TASK-1", "completed", tree("TASK-2", "duplicate"))),
        ):
            with self.subTest(duplicate=name):
                found = plan(ArchiveGql(root))
                self.assertEqual((found.outcome, found.unfinished), ("changed", ()))
                gql = ArchiveGql(root)
                self.assertEqual(archive(gql).result, "changed")
                self.assertEqual(gql.writes, [(linear_tracker.M_ARCHIVE, {"i": "id-TASK-1"})])

    def test_any_unfinished_issue_refuses_and_writes_nothing(self):
        cases = {
            "root": (tree("TASK-1", "started", tree("TASK-2")), "TASK-1"),
            "grandchild": (
                tree("TASK-1", "completed", tree("TASK-2", "completed", tree("TASK-3", "backlog"))),
                "TASK-3",
            ),
            "251st child": (
                tree("TASK-1", "completed", *many(250), tree("TASK-9", "unstarted")),
                "TASK-9",
            ),
            "251st grandchild": (
                tree(
                    "TASK-1",
                    "completed",
                    tree("TASK-2", "completed", *many(250), tree("TASK-9", "triage")),
                ),
                "TASK-9",
            ),
        }
        for name, (root, unfinished) in cases.items():
            gql = ArchiveGql(root)
            with self.subTest(name=name), self.assertRaises(ArchiveRefused) as cm:
                archive(gql)
            text = refusal("TASK-1", cm.exception.plan)
            self.assertIn(unfinished, text)
            self.assertIn(unfinished, [s.ident for s in cm.exception.plan.unfinished])
            self.assertIn("ensemble-orchestrator issue archive TASK-1 --dry-run", text)
            self.assertEqual(gql.writes, [])

    def test_every_children_page_is_read_at_every_level(self):
        root = tree(
            "TASK-1", "completed", *many(300), tree("TASK-2", "completed", *many(260, "GC"))
        )
        gql = ArchiveGql(root)
        self.assertEqual(archive(gql).result, "changed")
        self.assertIn((linear_tracker.Q_CHILDREN, {"i": "id-TASK-1", "c": "300"}), gql.calls)
        self.assertIn((linear_tracker.Q_CHILDREN, {"i": "id-TASK-2", "c": None}), gql.calls)
        self.assertIn((linear_tracker.Q_CHILDREN, {"i": "id-TASK-2", "c": "250"}), gql.calls)

    def test_an_archived_issue_is_unchanged_without_reading_sub_issues(self):
        gql = ArchiveGql(
            tree("TASK-1", "started", tree("TASK-2", "started"), archived="2026-10-01")
        )
        receipt = archive(gql)
        self.assertEqual((receipt.result, receipt_payload(receipt)["actor"]), ("unchanged", VIEWER))
        self.assertEqual(
            [q for q, _ in gql.calls], [issue_archive.Q_ISSUE, linear_tracker.Q_ARCHIVE]
        )

    def test_a_missing_or_malformed_ident_is_an_issue_error_before_any_read(self):
        for gql, ident in ((ArchiveGql(None), "TASK-1"), (ArchiveGql(tree()), "task 1")):
            with self.subTest(ident=ident), self.assertRaises(IssueError):
                archive(gql, ident)
            self.assertNotIn(linear_tracker.Q_ARCHIVE, [q for q, _ in gql.calls])

    def test_an_issue_gone_before_the_archive_read_writes_nothing(self):
        gql = GoneGql(tree())
        with self.assertRaisesRegex(LinearError, "^id-TASK-1: issue not found$"):
            archive(gql)
        self.assertEqual(gql.writes, [])

    def test_a_failed_read_writes_nothing(self):
        root = tree("TASK-1", "completed", *many(300), tree("TASK-2", "completed", tree("TASK-3")))
        cases = (
            (("id-TASK-1", 50), LinearError("linear api error: boom"), LinearError),
            (("id-TASK-1", 250), GraphQLErrors([{"message": "boom"}]), GraphQLErrors),
            (("id-TASK-2", 0), Unavailable("issue", reason="timed out"), Unavailable),
            (("id-TASK-2", 0), Unavailable("issueArchive", reason="timed out"), Unavailable),
            (("id-TASK-2", 0), None, LinearError),
            (("id-TASK-1", 0), None, LinearError),
        )
        for page, error, raised in cases:
            gql = ArchiveGql(root, broken={page: error})
            with self.subTest(page=page, error=error), self.assertRaises(raised):
                archive(gql)
            self.assertEqual(gql.writes, [])

    def test_a_next_page_without_a_new_cursor_fails_and_writes_nothing(self):
        for cursor in (None, "0"):
            gql = StuckGql(tree("TASK-1", "completed", *many(2)), cursor)
            with self.subTest(cursor=cursor), self.assertRaisesRegex(LinearError, "endCursor"):
                archive(gql)
            self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_success_false_fails(self):
        for result, raised, phrase in (
            (
                Unavailable("issueArchive", reason="timed out"),
                WriteUnknown,
                "archive TASK-1: result unknown (issueArchive: timed out): check with "
                "ensemble-orchestrator issue read TASK-1 before retrying",
            ),
            (
                Unavailable("renamedArchive", reason="timed out"),
                WriteUnknown,
                "archive TASK-1: result unknown (renamedArchive: timed out)",
            ),
            ({"success": False}, LinearError, "success: false"),
        ):
            gql = ArchiveGql(tree("TASK-1"), result=result)
            with self.subTest(phrase=phrase), self.assertRaises(raised) as cm:
                archive(gql)
            self.assertIn(phrase, str(cm.exception))
            self.assertEqual(len(gql.writes), 1)
