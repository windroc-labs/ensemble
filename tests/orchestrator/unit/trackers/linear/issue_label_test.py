"""ensemble-orchestrator issue label swap, add and remove: one label replaced, added or removed in a
single update that names only those, every other label kept, a label group never holding two.
"""

from ensemble.orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LabelRefused,
    LinearError,
    Unavailable,
    WriteUnknown,
)
from ensemble.orchestrator.issue.output import receipt_payload
from ensemble.orchestrator.trackers.linear import issue_label, issue_read
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import TEAM
from tests.orchestrator.unit.issue_fixtures import OTHER_TEAM, LabelGql, label, receipt_of, uid

GROUP = uid(10)
TODO, DOING = label(1, "todo", group=GROUP), label(2, "doing", group=GROUP)
BUG, CHORE, KEEP = label(3, "bug"), label(4, "chore", None), label(5, "keep")
CATALOG = (label(10, "Status", is_group=True), TODO, DOING, BUG, CHORE, KEEP)


def gql_of(*carried, catalog=CATALOG, **options):
    return LabelGql(catalog, [n["id"] for n in carried], **options)


class Swap(Case):
    def test_replaces_only_the_old_label_and_prints_the_receipt(self):
        gql = gql_of(KEEP, BUG)
        receipt = issue_label.change(gql, "swap", "TASK-2", "bug", "chore")
        self.assertEqual(
            gql.writes,
            [(issue_label.M_SWAP, {"i": "id-TASK-2", "a": [CHORE["id"]], "r": [BUG["id"]]})],
        )
        self.assertNotIn("labelIds:", issue_label.M_SWAP)
        self.assertEqual(gql.carried, [KEEP["id"], CHORE["id"]])
        self.assertEqual(receipt_payload(receipt), receipt_of("label swap"))

    def test_a_new_label_already_carried_is_not_added_twice(self):
        gql = gql_of(BUG, CHORE, KEEP)
        self.assertEqual(
            issue_label.change(gql, "swap", "TASK-2", "bug", "chore").result, "changed"
        )
        self.assertEqual(gql.writes[0][1], {"i": "id-TASK-2", "a": [], "r": [BUG["id"]]})
        self.assertEqual(gql.carried, [CHORE["id"], KEEP["id"]])

    def test_old_missing_and_new_carried_is_unchanged(self):
        for old in ("bug", BUG["id"]):
            gql = gql_of(CHORE, KEEP)
            with self.subTest(old=old):
                receipt = issue_label.change(gql, "swap", "TASK-2", old, "chore")
                self.assertEqual((receipt.result, gql.writes), ("unchanged", []))

    def test_a_rerun_after_old_was_retired_is_unchanged(self):
        retired = {**BUG, "retiredAt": "2026-01-01T00:00:00.000Z"}
        catalog = (*[n for n in CATALOG if n is not BUG], retired)
        for old in ("bug", BUG["id"]):
            gql = gql_of(CHORE, KEEP, catalog=catalog)
            with self.subTest(old=old):
                receipt = issue_label.change(gql, "swap", "TASK-2", old, "chore")
                self.assertEqual((receipt.result, gql.writes), ("unchanged", []))
        with self.assertRaises(LabelRefused):
            issue_label.change(gql_of(KEEP, catalog=catalog), "swap", "TASK-2", "bug", "chore")

    def test_the_same_label_carried_is_unchanged(self):
        gql = gql_of(BUG)
        self.assertEqual(
            issue_label.change(gql, "swap", "TASK-2", "bug", BUG["id"]).result, "unchanged"
        )
        self.assertEqual(gql.writes, [])

    def test_within_one_group_the_old_label_makes_way(self):
        gql = gql_of(TODO, KEEP)
        self.assertEqual(
            issue_label.change(gql, "swap", "TASK-2", "todo", "doing").result, "changed"
        )
        self.assertEqual(gql.carried, [KEEP["id"], DOING["id"]])

    def test_ids_name_labels_and_later_label_pages_are_read(self):
        filler = [label(100 + n, f"x{n}") for n in range(issue_read.PAGE)]
        gql = gql_of(*filler, BUG, catalog=(*CATALOG, *filler))
        issue_label.change(gql, "swap", "TASK-2", BUG["id"], CHORE["id"])
        self.assertIn(
            (issue_label.Q_MORE, {"i": "id-TASK-2", "c": str(issue_read.PAGE)}), gql.calls
        )
        self.assertEqual(gql.writes[0][1]["r"], [BUG["id"]])

    def test_a_labels_page_repeating_its_cursor_raises(self):
        page = {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "c-1"}}
        node = {"id": "id-TASK-2", "labels": page}
        with self.assertRaisesRegex(LinearError, "hasNextPage without a new endCursor"):
            issue_label.carried_labels(lambda _q, **_v: {"issue": {"labels": page}}, "TASK-2", node)

    def test_the_old_label_is_looked_for_on_the_issue_first(self):
        # a workspace and a team label share the name; only the workspace one is carried
        twin = label(6, "chore")
        gql = gql_of(CHORE, catalog=(*CATALOG, twin))
        issue_label.change(gql, "swap", "TASK-2", "chore", "bug")
        self.assertEqual(gql.writes[0][1]["r"], [CHORE["id"]])


class Add(Case):
    def test_adds_only_the_label_and_prints_the_receipt(self):
        gql = gql_of(BUG, TODO)
        receipt = issue_label.change(gql, "add", "TASK-2", None, "keep")
        self.assertEqual(
            gql.writes, [(issue_label.M_SWAP, {"i": "id-TASK-2", "a": [KEEP["id"]], "r": []})]
        )
        self.assertEqual(gql.carried, [BUG["id"], TODO["id"], KEEP["id"]])
        self.assertEqual(receipt_payload(receipt), receipt_of("label add"))

    def test_a_label_already_carried_is_unchanged(self):
        for new in ("keep", KEEP["id"], "todo"):
            gql = gql_of(KEEP, TODO)
            with self.subTest(new=new):
                receipt = issue_label.change(gql, "add", "TASK-2", None, new)
                self.assertEqual((receipt.result, gql.writes), ("unchanged", []))

    def test_a_second_label_of_its_group_is_refused(self):
        gql = gql_of(TODO, BUG)
        with self.assertRaises(LabelRefused) as cm:
            issue_label.change(gql, "add", "TASK-2", None, "doing")
        text = str(cm.exception)
        for phrase in (
            "label add TASK-2 refused, nothing written",
            "LABEL 'doing' is in label group",
            GROUP,
            f"{TODO['id']} 'todo'",
            "swap that one instead",
        ):
            self.assertIn(phrase, text)
        self.assertEqual(gql.writes, [])

    def test_an_unsettable_or_unresolved_label_writes_nothing(self):
        catalog = (*CATALOG, label(7, "old", retired="2026-01-01T00:00:00.000Z"))
        for new, phrase in (
            ("Status", "is a label group"),
            ("old", "is retired"),
            ("nope", "no label of team"),
        ):
            gql = gql_of(BUG, catalog=catalog)
            with self.subTest(new=new), self.assertRaises(IssueError) as cm:
                issue_label.change(gql, "add", "TASK-2", None, new)
            self.assertIn(f"LABEL {new}", str(cm.exception).replace("'", ""))
            self.assertIn(phrase, str(cm.exception))
            self.assertIn(f"lookup labels --team {TEAM}", str(cm.exception))
            self.assertEqual(gql.writes, [])


class Remove(Case):
    def test_removes_only_the_label_and_prints_the_receipt(self):
        gql = gql_of(KEEP, BUG, TODO)
        receipt = issue_label.change(gql, "remove", "TASK-2", "keep")
        self.assertEqual(
            gql.writes, [(issue_label.M_SWAP, {"i": "id-TASK-2", "a": [], "r": [KEEP["id"]]})]
        )
        self.assertEqual(gql.carried, [BUG["id"], TODO["id"]])
        self.assertEqual(receipt_payload(receipt), receipt_of("label remove"))

    def test_a_label_not_carried_is_unchanged(self):
        for old in ("keep", KEEP["id"]):
            gql = gql_of(BUG)
            with self.subTest(old=old):
                receipt = issue_label.change(gql, "remove", "TASK-2", old)
                self.assertEqual((receipt.result, gql.writes), ("unchanged", []))

    def test_a_retired_label_still_carried_is_removed(self):
        retired = {**KEEP, "retiredAt": "2026-01-01T00:00:00.000Z"}
        catalog = (*[n for n in CATALOG if n is not KEEP], retired)
        gql = gql_of(BUG, KEEP, catalog=catalog)
        self.assertEqual(issue_label.change(gql, "remove", "TASK-2", "keep").result, "changed")
        self.assertEqual(gql.carried, [BUG["id"]])

    def test_an_unresolved_or_ambiguous_label_writes_nothing(self):
        twins = (label(8, "dup"), label(9, "dup", None))
        for carried, old, phrase in (
            ((BUG,), "nope", "LABEL 'nope': no label of team"),
            (twins, "dup", "LABEL 'dup': the issue carries 2"),
        ):
            gql = gql_of(*carried, catalog=(*CATALOG, *twins))
            with self.subTest(old=old), self.assertRaises(IssueError) as cm:
                issue_label.change(gql, "remove", "TASK-2", old)
            self.assertIn(phrase, str(cm.exception))
            self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown(self):
        gql = gql_of(KEEP, result=Unavailable("issueUpdate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            issue_label.change(gql, "remove", "TASK-2", "keep")
        self.assertIn("label remove TASK-2: result unknown", str(cm.exception))
        self.assertEqual(len(gql.writes), 1)


class Refused(Case):
    def test_neither_label_carried_is_refused(self):
        gql = gql_of(KEEP)
        with self.assertRaises(LabelRefused) as cm:
            issue_label.change(gql, "swap", "TASK-2", "bug", "chore")
        text = str(cm.exception)
        for phrase in ("label swap TASK-2 refused", "nothing written", "'bug'", "'chore'"):
            self.assertIn(phrase, text)
        self.assertEqual(gql.writes, [])

    def test_a_second_label_of_the_new_group_is_refused(self):
        gql = gql_of(TODO, BUG)
        with self.assertRaises(LabelRefused) as cm:
            issue_label.change(gql, "swap", "TASK-2", "bug", "doing")
        text = str(cm.exception)
        for phrase in ("label group", GROUP, f"{TODO['id']} 'todo'"):
            self.assertIn(phrase, text)
        self.assertEqual(gql.writes, [])

    def test_an_unsettable_or_unresolved_new_label_writes_nothing(self):
        catalog = (
            *CATALOG,
            label(7, "old", retired="2026-01-01T00:00:00.000Z"),
            label(8, "dup"),
            label(9, "dup", None),
            label(11, "theirs", OTHER_TEAM),
        )
        cases = (
            ("Status", "is a label group"),
            ("old", "is retired"),
            ("dup", "2 match"),
            ("nope", "no label of team"),
            (uid(11), "is not a label of team"),
        )
        for new, phrase in cases:
            gql = gql_of(BUG, catalog=catalog)
            with self.subTest(new=new), self.assertRaises(IssueError) as cm:
                issue_label.change(gql, "swap", "TASK-2", "bug", new)
            self.assertIn(f"--to {new}", str(cm.exception).replace("'", ""))
            self.assertIn(phrase, str(cm.exception))
            self.assertIn(f"lookup labels --team {TEAM}", str(cm.exception))
            self.assertEqual(gql.writes, [])

    def test_an_unresolved_old_label_or_issue_writes_nothing(self):
        twins = (label(8, "dup"), label(9, "dup", None))
        cases = (
            ("TASK-2", "nope", "--from 'nope': no label of team", (BUG,)),
            ("TASK-2", "dup", "--from 'dup': the issue carries 2", twins),
            ("TASK-9", "bug", "TASK-9: no such issue", (BUG,)),
        )
        for ident, old, phrase, carried in cases:
            gql = gql_of(*carried, catalog=(*CATALOG, *twins))
            with self.subTest(phrase=phrase), self.assertRaises(IssueError) as cm:
                issue_label.change(gql, "swap", ident, old, "chore")
            self.assertIn(phrase, str(cm.exception))
            self.assertEqual(gql.writes, [])

    def test_two_carried_old_labels_of_one_name_list_both_and_the_lookup(self):
        twins = (label(8, "dup"), label(9, "dup", None))
        gql = gql_of(*twins, catalog=(*CATALOG, *twins))
        with self.assertRaises(IssueError) as cm:
            issue_label.change(gql, "swap", "TASK-2", "dup", "chore")
        text = str(cm.exception)
        for phrase in (
            uid(8),
            uid(9),
            f"list them: ensemble-orchestrator issue lookup labels --team {TEAM}",
        ):
            self.assertIn(phrase, text)
        self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_never_retried(self):
        gql = gql_of(BUG, result=Unavailable("issueUpdate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            issue_label.change(gql, "swap", "TASK-2", "bug", "chore")
        text = str(cm.exception)
        for phrase in ("label swap TASK-2", "result unknown", "issue read TASK-2"):
            self.assertIn(phrase, text)
        self.assertEqual(len(gql.writes), 1)

    def test_a_refused_write_fails_and_is_not_unknown(self):
        for result in ({"success": False}, GraphQLErrors([{"message": "boom"}])):
            gql = gql_of(BUG, result=result)
            with self.subTest(result=result), self.assertRaises(LinearError) as cm:
                issue_label.change(gql, "swap", "TASK-2", "bug", "chore")
            self.assertNotIsInstance(cm.exception, WriteUnknown)
            self.assertEqual(len(gql.writes), 1)
