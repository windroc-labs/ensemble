"""The issue command's output rules: a page fitted within the limit, a read cut to the default
limits, the archive dry run's plan, the archive refusal's text, a write's receipt and create's
partial-relations report and message.
"""

import json

from ensemble.orchestrator.engine.flow.types import ArchivePlan, ArchiveRefused
from ensemble.orchestrator.engine.types import Blocking, Receipt, RelationsFailed
from ensemble.orchestrator.issue import output
from ensemble.orchestrator.trackers.linear import issue_archive, issue_read
from ensemble.orchestrator.trackers.linear.linear_tracker import LinearTracker
from orchestrator_tests.case import Case
from orchestrator_tests.unit.issue_fixtures import (
    ArchiveGql,
    Gql,
    comment,
    conn,
    issue_node,
    rel,
    tree,
)


def relation(kind, field, ident):
    return {"type": kind, field: rel(ident)}


def many(n, prefix="SUB", kind="completed"):
    return [tree(f"{prefix}-{i}", kind) for i in range(1, n + 1)]


def archive(gql, ident="TASK-1"):
    return issue_archive.archive(LinearTracker(gql), gql, ident)


def dry_run(gql, ident="TASK-1"):
    return output.dry_run(issue_archive.plan(LinearTracker(gql), gql, ident))


def row(n, name="x", cut=()):
    return {"id": f"id-{n}", "name": name}, list(cut), f"cur-{n}"


class FitPage(Case):
    def test_within_the_limit_nothing_is_cut(self):
        page = json.loads(output.fit_page([row(1), row(2, cut=["team_ids"])], "end"))
        self.assertEqual(page["next_cursor"], "end")
        self.assertTrue(page["has_next_page"])
        self.assertEqual(page["truncated"], ["items.1.team_ids"])
        last = json.loads(output.fit_page([], None))
        self.assertEqual(
            last, {"items": [], "truncated": [], "has_next_page": False, "next_cursor": None}
        )

    def test_over_the_limit_trailing_items_drop_and_the_cursor_follows_the_last_kept(self):
        rows = [row(n, "名" * 400, cut=["team_ids"]) for n in range(50)]
        data = output.fit_page(rows, None)
        self.assertLessEqual(len(data), 8192)
        page = json.loads(data)
        kept = len(page["items"])
        self.assertLess(kept, 50)
        self.assertEqual((page["has_next_page"], page["next_cursor"]), (True, f"cur-{kept - 1}"))
        self.assertEqual(page["truncated"], [f"items.{n}.team_ids" for n in range(kept)])
        self.assertEqual(page["items"][-1]["name"], "名" * 400)

    def test_a_lone_item_shortens_its_text_but_keeps_its_references(self):
        item = {"id": "i" * 3000, "name": "n" * 9000, "labels": [{"id": "l1", "name": "m" * 3000}]}
        page = json.loads(output.fit_page([(item, [], "c")], None))
        got = page["items"][0]
        self.assertEqual((got["id"], got["labels"][0]["id"]), ("i" * 3000, "l1"))
        self.assertLess(len(got["name"]), 9000)
        self.assertTrue(("n" * 9000).startswith(got["name"]))
        self.assertIn("items.0.name", page["truncated"])
        self.assertIsNone(page["next_cursor"])

    def test_references_alone_over_the_limit_fail(self):
        item = {"id": "i" * 9000, "name": ""}
        with self.assertRaises(OverflowError):
            output.fit_page([(item, [], "c")], None)


class Compact(Case):
    def test_default_limits_mark_every_cut(self):
        node = issue_node(
            description="d" * 601,
            comments=conn([comment(n, body="b" * 301 if n == 6 else "ok") for n in range(1, 7)]),
            children=conn([rel(f"TASK-{n}") for n in range(10, 21)]),
            relations=conn(
                [relation("blocks", "relatedIssue", f"TASK-{n}") for n in range(30, 41)]
            ),
            inverseRelations=conn(
                [relation("blocks", "issue", f"TASK-{n}") for n in range(50, 61)]
            ),
        )
        view = output.compact(issue_read.view(Gql(node), "TASK-2"))
        self.assertEqual(len(view["description"]), 600)
        self.assertEqual([c["id"] for c in view["comments"]], ["c6", "c5", "c4", "c3", "c2"])
        self.assertEqual(len(view["comments"][0]["body"]), 300)
        for column in ("children", "blocks", "blocked_by"):
            self.assertEqual(len(view[column]), 10, column)
        self.assertEqual(
            view["truncated"],
            ["description", "comments", "comments.0.body", "blocks", "blocked_by", "children"],
        )

    def test_within_limits_nothing_is_cut(self):
        node = issue_node(
            description="d" * 600,
            comments=conn([comment(n, body="b" * 300) for n in range(1, 6)]),
            children=conn([rel(f"TASK-{n}") for n in range(10, 20)]),
        )
        view = output.compact(issue_read.view(Gql(node), "TASK-2"))
        self.assertEqual(view["truncated"], [])
        self.assertEqual(len(view["description"]), 600)
        self.assertEqual(len(view["comments"]), 5)

    def test_a_page_mark_is_not_repeated(self):
        node = issue_node(children=conn([rel(f"TASK-{n}") for n in range(10, 21)], more=True))
        view = output.compact(issue_read.view(Gql(node), "TASK-2"))
        self.assertEqual(view["truncated"], ["children"])


class Refusal(Case):
    def test_the_refusal_text_and_its_plan(self):
        gql = ArchiveGql(
            tree("TASK-1", "completed", tree("TASK-2", "canceled", tree("TASK-3", "started")))
        )
        with self.assertRaises(ArchiveRefused) as cm:
            archive(gql)
        self.assertEqual(
            output.refusal("TASK-1", cm.exception.plan),
            "archive TASK-1 refused, nothing archived: 1 unfinished of 3 (it and its unarchived "
            "sub-issues): TASK-3 (Started); complete or cancel each first "
            "(ensemble-orchestrator issue archive TASK-1 --dry-run lists them)",
        )
        self.assertIsInstance(cm.exception.plan, ArchivePlan)
        self.assertEqual(cm.exception.plan.ident, "TASK-1")

    def test_a_long_refusal_names_the_first_ones_and_counts_the_rest(self):
        gql = ArchiveGql(tree("TASK-1", "completed", *many(300, kind="started")))
        with self.assertRaises(ArchiveRefused) as cm:
            archive(gql)
        text = output.refusal("TASK-1", cm.exception.plan)
        self.assertIn("300 unfinished", text)
        self.assertIn("SUB-1 (Started)", text)
        self.assertIn(f"and {300 - output.SHOWN} more", text)
        self.assertNotIn(f"SUB-{output.SHOWN + 1} ", text)


class DryRun(Case):
    def test_the_plan_as_json(self):
        root = tree("TASK-1", "started", tree("TASK-2", "canceled", tree("TASK-3", "unstarted")))
        self.assertEqual(
            dry_run(ArchiveGql(root)),
            {
                "issue": {
                    "id": "id-TASK-1",
                    "identifier": "TASK-1",
                    "url": "https://linear.app/t/issue/TASK-1",
                },
                "operation": "archive",
                "dry_run": True,
                "result": "refused",
                "descendant_count": 2,
                "descendants": [
                    {"id": "id-TASK-2", "identifier": "TASK-2", "state": "Canceled"},
                    {"id": "id-TASK-3", "identifier": "TASK-3", "state": "Unstarted"},
                ],
                "unfinished_count": 2,
                "unfinished": [
                    {"id": "id-TASK-1", "identifier": "TASK-1", "state": "Started"},
                    {"id": "id-TASK-3", "identifier": "TASK-3", "state": "Unstarted"},
                ],
            },
        )

    def test_lists_every_descendant_and_writes_nothing(self):
        root = tree("TASK-1", "completed", *many(251), tree("TASK-2", "canceled", tree("TASK-3")))
        gql = ArchiveGql(root)
        plan = dry_run(gql)
        self.assertEqual(gql.writes, [])
        self.assertEqual(
            (plan["operation"], plan["dry_run"], plan["result"]), ("archive", True, "changed")
        )
        self.assertEqual(plan["descendant_count"], 253)
        self.assertEqual(
            [d["identifier"] for d in plan["descendants"]][-3:], ["SUB-251", "TASK-2", "TASK-3"]
        )
        self.assertEqual(
            plan["descendants"][-1],
            {"id": "id-TASK-3", "identifier": "TASK-3", "state": "Completed"},
        )
        self.assertEqual((plan["unfinished_count"], plan["unfinished"]), (0, []))

    def test_names_what_would_refuse(self):
        root = tree("TASK-1", "started", *many(250), tree("TASK-9", "unstarted"))
        plan = dry_run(ArchiveGql(root))
        self.assertEqual((plan["result"], plan["unfinished_count"]), ("refused", 2))
        self.assertEqual([u["identifier"] for u in plan["unfinished"]], ["TASK-1", "TASK-9"])

    def test_an_archived_issue_is_unchanged(self):
        plan = dry_run(ArchiveGql(tree("TASK-1", "started", archived="2026-10-01")))
        self.assertEqual(
            (plan["result"], plan["descendants"], plan["unfinished"]), ("unchanged", [], [])
        )


ISSUE = {"id": "id-9", "identifier": "TASK-9", "url": "https://linear.app/t/issue/TASK-9"}


def made(op="create", result="changed", comment_id=None):
    return Receipt("id-9", "TASK-9", ISSUE["url"], op, result, "u-1", "Ann", comment_id=comment_id)


class ReceiptPayload(Case):
    def test_the_issue_operation_result_and_actor(self):
        for op in ("create", "edit", "move", "relation add", "archive", "label swap"):
            with self.subTest(op=op):
                self.assertEqual(
                    output.receipt_payload(made(op, "unchanged")),
                    {
                        "issue": ISSUE,
                        "operation": op,
                        "result": "unchanged",
                        "actor": {"id": "u-1", "name": "Ann"},
                    },
                )

    def test_a_comment_names_its_comment_id_even_when_unknown(self):
        for comment_id in ("c-1", None):
            with self.subTest(comment_id=comment_id):
                self.assertEqual(
                    output.receipt_payload(made("comment", comment_id=comment_id)),
                    {
                        "issue": ISSUE,
                        "operation": "comment",
                        "result": "changed",
                        "actor": {"id": "u-1", "name": "Ann"},
                        "comment_id": comment_id,
                    },
                )

    def test_the_printed_receipt_is_its_payload(self):
        sent = made("comment", comment_id="c-1")
        printed = json.loads(output.receipt(output.receipt_payload(sent)))
        self.assertEqual(printed, output.receipt_payload(sent))


def partial():
    done = (Blocking("blocks", "TASK-1"), Blocking("blocked_by", "TASK-2"))
    failed = (
        (Blocking("blocks", "TASK-3"), "x" * 250),
        (Blocking("blocked_by", "TASK-4"), "linear api error: denied"),
    )
    return RelationsFailed(made(), done, failed)


class PartialReport(Case):
    def test_done_failed_with_cut_errors_and_one_recover_command_each(self):
        self.assertEqual(
            output.partial_report(partial()),
            {
                "issue": ISSUE,
                "operation": "create",
                "result": "partial",
                "actor": {"id": "u-1", "name": "Ann"},
                "done": [{"blocks": "TASK-1"}, {"blocked_by": "TASK-2"}],
                "failed": [
                    {"blocks": "TASK-3", "error": "x" * 200},
                    {"blocked_by": "TASK-4", "error": "linear api error: denied"},
                ],
                "recover": [
                    "ensemble-orchestrator issue relation add TASK-9 --blocks TASK-3",
                    "ensemble-orchestrator issue relation add TASK-9 --blocked-by TASK-4",
                ],
            },
        )

    def test_an_error_at_the_limit_is_kept_whole(self):
        error = RelationsFailed(made(), (), ((Blocking("blocks", "TASK-3"), "y" * 200),))
        self.assertEqual(
            output.partial_report(error)["failed"], [{"blocks": "TASK-3", "error": "y" * 200}]
        )

    def test_the_message_counts_the_failed_and_names_only_their_commands(self):
        self.assertEqual(
            output.partial_message(partial()),
            "create TASK-9: created https://linear.app/t/issue/TASK-9, but 2 of 4 blocking "
            "relations failed; add only them: ensemble-orchestrator issue relation add TASK-9 "
            "--blocks TASK-3; ensemble-orchestrator issue relation add TASK-9 --blocked-by "
            "TASK-4; do not run create again",
        )
