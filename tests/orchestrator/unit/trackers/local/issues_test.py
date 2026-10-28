"""LocalIssues: the issue command's backend on a temp board."""

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.orchestrator.engine.flow.types import (
    BOTH_DIRECTIONS,
    IDENT_ERROR,
    ArchiveRefused,
    IssueState,
    Priority,
    SubIssue,
)
from ensemble.orchestrator.engine.types import (
    Blocking,
    IssueError,
    Receipt,
    RelationsFailed,
    TrackerFileError,
)
from ensemble.orchestrator.issue import output
from ensemble.orchestrator.trackers.local.issues import LocalIssues
from tests.orchestrator.unit.issue_fixtures import issue_change, list_query, new_issue
from tests.orchestrator.unit.trackers.local import board_fixtures
from tests.orchestrator.unit.trackers.local.board_fixtures import MTIME, text

NOW = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
ROLES = {
    "pm": SimpleNamespace(tasks=("plan", "build")),
    "engineer": SimpleNamespace(tasks=("build", "light-build")),
}
ACTOR = {"id": "other", "name": "other"}
READY = "- 2026-10-19 14:02 ready other\n"
NOTE = (
    "#### self · 2026-10-19 15:00 <!-- id=c1 key=archived:engineer -->\nArchived from engineer.\n"
)
CLIENT = "want a client name: no whitespace, control character or leading quote"


def rel(ident, state):
    return {"id": ident, "identifier": ident, "title": f"{ident} title", "state": state}


def by_id(issues, ident):
    return {
        "view": lambda: issues.view(ident, full=False),
        "edit": lambda: issues.edit(issue_change(ident, title="t")),
        "move": lambda: issues.move(ident, "done"),
        "comment": lambda: issues.comment(ident, "body"),
        "relation": lambda: issues.relation("add", ident, "blocks", "LOC-1"),
        "archive_plan": lambda: issues.archive_plan(ident),
        "archive": lambda: issues.archive(ident),
    }


def by_option(issues, **given):
    return {
        "create": lambda: issues.create(new_issue(**given)),
        "list": lambda: issues.page(list_query(**given), 20, None),
        "edit": lambda: issues.edit(issue_change("LOC-1", **given)),
    }


class Board(board_fixtures.Board):
    def setUp(self):
        super().setUp()
        os.mkdir(os.path.join(self.board, "pm"))

    def issues(self):
        return LocalIssues(self.board, ROLES, now=lambda: NOW, prefix="LOC")

    def read(self, path):
        with open(path, encoding="utf-8") as file:
            return file.read()

    def snapshot(self):
        found = {}
        for top, _, names in os.walk(self.board):
            for name in names:
                path = os.path.join(top, name)
                if name != ".lock":
                    found[path] = (self.read(path), os.stat(path).st_mtime_ns)
        return found

    def refused(self, call, message, error=IssueError):
        before = self.snapshot()
        with self.assertRaises(error) as cm:
            call()
        self.assertEqual(str(cm.exception), message)
        self.assertEqual(self.snapshot(), before)

    def receipt(self, ident, path, op, result, comment_id=None):
        return Receipt(ident, ident, Path(path).as_uri(), op, result, "other", "other", comment_id)


class View(Board):
    def test_fields_comments_and_relations(self):
        self.write("pm/done", "LOC-1")
        path = self.write(
            "engineer/review",
            "LOC-2",
            front="task: build\nclient: claude\npriority: 2\nblocked_by: [LOC-9, LOC-3]\n"
            "related: [LOC-1]\nparent: LOC-1\n",
            description="Do it.",
            comments="#### other · 2026-10-19 14:05\nRead LOC-7 first.\n\n"
            "#### self · 2026-10-19 16:10 <!-- id=c1 key=result:ab12 -->\nBuild ready.\n",
            history=READY + "- 2026-10-19 16:10 review self\n",
        )
        self.write("unassigned/backlog", "LOC-3")
        self.write("me/backlog", "LOC-10", front="blocked_by: [LOC-2]\n")
        self.write("pm/ready", "LOC-4", front="blocked_by: [LOC-2]\n")
        self.write("engineer/running", "LOC-5", front="parent: LOC-2\n")
        self.write(
            "archive", "LOC-6", front="parent: LOC-2\n", history="- 2026-10-19 14:02 done self\n"
        )
        comment = {"parent_id": None, "quoted_text": None, "on_behalf_of": None}
        self.assertEqual(
            self.issues().view("LOC-2", full=False),
            {
                "id": "LOC-2",
                "identifier": "LOC-2",
                "title": "LOC-2 title",
                "url": Path(path).as_uri(),
                "state": {"id": "review", "name": "review", "type": "started"},
                "priority": "high",
                "labels": [{"id": "build", "name": "build"}, {"id": "claude", "name": "claude"}],
                "project": None,
                "milestone": None,
                "assignee": {"id": "engineer", "name": "engineer", "email": None},
                "description": "Do it.",
                "comments": [
                    {
                        "id": "LOC-2#2",
                        "author": {"id": "self", "name": "self"},
                        "created_at": "2026-10-19T16:10:00.000Z",
                        "body": "Build ready.",
                        **comment,
                    },
                    {
                        "id": "LOC-2#1",
                        "author": {"id": "other", "name": "other"},
                        "created_at": "2026-10-19T14:05:00.000Z",
                        "body": "Read LOC-7 first.",
                        **comment,
                    },
                ],
                "blocks": [rel("LOC-4", "ready"), rel("LOC-10", "backlog")],
                "blocked_by": [
                    rel("LOC-3", "backlog"),
                    {"id": "LOC-9", "identifier": "LOC-9", "title": None, "state": None},
                ],
                "parent": rel("LOC-1", "done"),
                "children": [rel("LOC-5", "running")],
                "truncated": [],
                "continue": None,
            },
        )

    def test_equal_times_later_in_file_first(self):
        self.write(
            "pm/ready",
            "LOC-1",
            comments="#### other · 2026-10-19 14:05\nfirst\n#### self · 2026-10-19 14:05\nsecond\n"
            "#### other · 2026-10-19 13:00\nearliest\n",
        )
        view = self.issues().view("LOC-1", full=True)
        self.assertEqual(
            [(c["id"], c["body"]) for c in view["comments"]],
            [("LOC-1#2", "second"), ("LOC-1#1", "first"), ("LOC-1#3", "earliest")],
        )
        self.assertEqual(
            (view["parent"], view["children"], view["blocks"], view["blocked_by"]),
            (None, [], [], []),
        )
        self.assertEqual(view["priority"], "none")

    def test_archived_and_unassigned(self):
        path = self.write(
            "archive", "LOC-1", comments=NOTE, history="- 2026-10-19 14:02 done self\n"
        )
        self.write("archive", "LOC-2")
        self.write("unassigned/ready", "LOC-3")
        view = self.issues().view("LOC-1", full=False)
        self.assertEqual(view["url"], Path(path).as_uri())
        self.assertEqual(view["state"], {"id": "done", "name": "done", "type": "completed"})
        self.assertEqual(view["assignee"], {"id": "engineer", "name": "engineer", "email": None})
        view = self.issues().view("LOC-2", full=False)
        self.assertEqual((view["state"], view["assignee"]), (None, None))
        self.assertIsNone(self.issues().view("LOC-3", full=False)["assignee"])

    def test_archive_note_left_out(self):
        """The engine's archive note stays in the file, out of the view and of the ids' count."""
        self.write(
            "archive",
            "LOC-1",
            comments="#### other · 2026-10-19 14:05\nfirst\n"
            "#### self · 2026-10-19 14:06 <!-- id=c0 key=result:ab12 -->\nBuild ready.\n"
            + NOTE
            + "#### other · 2026-10-19 16:00\nlast\n",
            history="- 2026-10-19 14:02 done self\n",
        )
        view = self.issues().view("LOC-1", full=True)
        self.assertEqual(
            [(c["id"], c["author"]["id"], c["body"]) for c in view["comments"]],
            [
                ("LOC-1#3", "other", "last"),
                ("LOC-1#2", "self", "Build ready."),
                ("LOC-1#1", "other", "first"),
            ],
        )
        self.assertEqual(view["assignee"], {"id": "engineer", "name": "engineer", "email": None})

    def test_ids_stable_across_rearchive(self):
        """Archive, restore, comment, archive again from the same and from another assignee: every
        shown id stays; a restore goes to the latest archive's assignee."""
        path = self.write(
            "engineer/done",
            "LOC-1",
            comments="#### other · 2026-10-19 14:05\nfirst\n",
            history="- 2026-10-19 14:02 done self\n",
        )
        name = os.path.basename(path)
        issues = self.issues()

        def ids():
            return [(c["id"], c["body"]) for c in issues.view("LOC-1", full=True)["comments"]]

        issues.archive("LOC-1")
        self.assertEqual(ids(), [("LOC-1#1", "first")])
        issues.tracker.unarchive("LOC-1")
        self.assertEqual(issues.comment("LOC-1", "second").comment_id, "LOC-1#2")
        shown = [("LOC-1#2", "second"), ("LOC-1#1", "first")]
        self.assertEqual(ids(), shown)
        issues.archive("LOC-1")
        self.assertEqual(ids(), shown)
        issues.tracker.unarchive("LOC-1")
        os.makedirs(os.path.join(self.board, "pm", "done"))
        os.rename(path, os.path.join(self.board, "pm", "done", name))
        issues = self.issues()
        issues.archive("LOC-1")
        self.assertEqual(ids(), shown)
        issues.tracker.unarchive("LOC-1")
        self.assertTrue(os.path.isfile(os.path.join(self.board, "pm", "done", name)))
        self.assertEqual(ids(), shown)

    def test_state_types(self):
        types = {
            "backlog": "backlog",
            "ready": "unstarted",
            "running": "started",
            "review": "started",
            "approved": "started",
            "done": "completed",
            "canceled": "canceled",
        }
        for n, state in enumerate(types, 1):
            self.write(f"pm/{state}", f"LOC-{n}")
        issues = self.issues()
        for n, (state, kind) in enumerate(types.items(), 1):
            with self.subTest(state=state):
                self.assertEqual(
                    issues.view(f"LOC-{n}", full=False)["state"],
                    {"id": state, "name": state, "type": kind},
                )


class Pages(Board):
    def setUp(self):
        super().setUp()
        self.paths = {
            "LOC-1": self.write(
                "engineer/ready",
                "LOC-1",
                mtime=MTIME.replace(hour=1),
                front="task: build\nclient: claude\npriority: 2\n",
                description="Fix the Parser",
            ),
            "LOC-2": self.write(
                "pm/backlog", "LOC-2", mtime=MTIME.replace(hour=3), front="parent: LOC-1\n"
            ),
            "LOC-3": self.write("unassigned/backlog", "LOC-3", mtime=MTIME.replace(hour=2)),
            "LOC-4": self.write("me/review", "LOC-4", mtime=MTIME.replace(hour=2)),
        }
        self.write(
            "archive",
            "LOC-5",
            mtime=MTIME.replace(hour=5),
            comments=NOTE,
            history="- 2026-10-19 14:02 done self\n",
        )

    def ids(self, **given):
        return [item["identifier"] for item, _, _ in self.issues().walk(list_query(**given))]

    def test_order_and_archived(self):
        self.assertEqual(self.ids(), ["LOC-2", "LOC-4", "LOC-3", "LOC-1"])
        self.assertEqual(
            self.ids(include_archived=True), ["LOC-5", "LOC-2", "LOC-4", "LOC-3", "LOC-1"]
        )

    def test_each_filter(self):
        for given, want in (
            ({"query": "parser"}, ["LOC-1"]),
            ({"query": "loc-3 TITLE"}, ["LOC-3"]),
            ({"state": IssueState.BACKLOG}, ["LOC-2", "LOC-3"]),
            ({"state": IssueState.DONE}, []),
            ({"state": IssueState.DONE, "include_archived": True}, ["LOC-5"]),
            ({"role": "engineer"}, ["LOC-1"]),
            ({"role": "engineer", "include_archived": True}, ["LOC-5", "LOC-1"]),
            ({"role": "me"}, ["LOC-4"]),
            ({"role": "unassigned"}, ["LOC-3"]),
            ({"role": "pm"}, ["LOC-2"]),
            ({"task": "build"}, ["LOC-1"]),
            ({"task": "plan"}, []),
            ({"client": "claude"}, ["LOC-1"]),
            ({"priority": Priority.HIGH}, ["LOC-1"]),
            ({"priority": Priority.NONE}, ["LOC-2", "LOC-4", "LOC-3"]),
            ({"parent": "LOC-1"}, ["LOC-2"]),
            ({"state": IssueState.BACKLOG, "role": "pm"}, ["LOC-2"]),
        ):
            with self.subTest(given=given):
                self.assertEqual(self.ids(**given), want)

    def test_items(self):
        rows = self.issues().walk(list_query(include_archived=True))
        items = {item["identifier"]: item for item, _, _ in rows}
        self.assertEqual([(cut, cursor) for _, cut, cursor in rows][0], ([], "LOC-5"))
        self.assertEqual(
            items["LOC-1"],
            {
                "id": "LOC-1",
                "identifier": "LOC-1",
                "title": "LOC-1 title",
                "url": Path(self.paths["LOC-1"]).as_uri(),
                "state": {"id": "ready", "name": "ready"},
                "labels": [{"id": "build", "name": "build"}, {"id": "claude", "name": "claude"}],
                "assignee": {"id": "engineer", "name": "engineer"},
                "project": None,
                "parent_id": None,
            },
        )
        self.assertEqual(items["LOC-2"]["parent_id"], "LOC-1")
        self.assertIsNone(items["LOC-3"]["assignee"])
        self.assertEqual(
            (items["LOC-5"]["state"], items["LOC-5"]["assignee"]),
            ({"id": "done", "name": "done"}, {"id": "engineer", "name": "engineer"}),
        )

    def test_cursor_pages(self):
        issues, query = self.issues(), list_query()
        page, more, end = issues.page(query, 2, None)
        self.assertEqual(([c for _, _, c in page], more, end), (["LOC-2", "LOC-4"], True, "LOC-4"))
        page, more, end = issues.page(query, 2, end)
        self.assertEqual(([c for _, _, c in page], more, end), (["LOC-3", "LOC-1"], False, "LOC-1"))
        self.assertEqual(issues.page(query, 2, "LOC-1"), ([], False, None))
        self.assertEqual(issues.page(query, 2, "LOC-4")[0], page)
        self.refused(
            lambda: issues.page(query, 2, "LOC-5"), "--cursor LOC-5: not a cursor of this list"
        )


class Create(Board):
    def setUp(self):
        super().setUp()
        self.write("pm/done", "LOC-1", history=READY)
        self.write("pm/ready", "LOC-2", history=READY)
        self.blocked = self.write(
            "engineer/ready", "LOC-3", front="blocked_by: [LOC-2]\n", history=READY
        )

    def test_defaults(self):
        got = self.issues().create(new_issue(title="A: b/c", body="Body\n"))
        path = os.path.join(self.board, "unassigned/backlog/LOC-4 A: b-c.md")
        self.assertEqual(got, self.receipt("LOC-4", path, "create", "changed"))
        self.assertEqual(
            self.read(path),
            '---\nid: LOC-4\ntitle: "A: b/c"\npriority: 0\ncreated: 2026-10-20T09:30:00+00:00\n'
            "---\n\n### Description\nBody\n\n### History\n- 2026-10-20 09:30 backlog other\n",
        )

    def test_fields_and_blocks(self):
        issues = self.issues()
        new = new_issue(
            title="T",
            body="Body",
            role="engineer",
            state=IssueState.READY,
            task="light-build",
            client='ac"me',
            priority=Priority.HIGH,
            parent="LOC-1",
            blocked_by=("LOC-2",),
            blocks=("LOC-3", "LOC-3"),
        )
        got = issues.create(new)
        path = os.path.join(self.board, "engineer/ready/LOC-4 T.md")
        self.assertEqual(got, self.receipt("LOC-4", path, "create", "changed"))
        self.assertEqual(
            self.read(path),
            '---\nid: LOC-4\ntitle: "T"\ntask: "light-build"\nclient: "ac\\"me"\npriority: 2\n'
            "created: 2026-10-20T09:30:00+00:00\nblocked_by: [LOC-2]\nparent: LOC-1\n---\n\n"
            "### Description\nBody\n\n### History\n- 2026-10-20 09:30 ready other\n",
        )
        self.assertEqual(
            self.read(self.blocked),
            text("LOC-3", front="blocked_by: [LOC-2, LOC-4]\n", history=READY),
        )
        view = self.issues().view("LOC-4", full=False)
        self.assertEqual(
            (view["labels"][1], view["blocked_by"], view["blocks"]),
            ({"id": 'ac"me', "name": 'ac"me'}, [rel("LOC-2", "ready")], [rel("LOC-3", "ready")]),
        )

    def test_a_failing_blocks_write_reports_partial(self):
        real = os.replace
        failing = os.path.join(self.board, "engineer/ready/LOC-3 x.md")

        def replace(src, dst):
            if dst == failing:
                raise PermissionError(13, "Permission denied")
            real(src, dst)

        new = new_issue(title="T", body="Body", blocks=("LOC-1", "LOC-3"), blocked_by=("LOC-2",))
        with (
            mock.patch("os.replace", replace),
            self.assertRaises(RelationsFailed) as cm,
        ):
            self.issues().create(new)
        path = os.path.join(self.board, "unassigned/backlog/LOC-4 T.md")
        self.assertEqual(
            (cm.exception.receipt, cm.exception.done, cm.exception.failed),
            (
                self.receipt("LOC-4", path, "create", "changed"),
                (Blocking("blocks", "LOC-1"), Blocking("blocked_by", "LOC-2")),
                ((Blocking("blocks", "LOC-3"), "[Errno 13] Permission denied"),),
            ),
        )
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.issues().view("LOC-1", full=False)["blocked_by"][0]["id"], "LOC-4")
        self.assertEqual(
            self.issues().view("LOC-3", full=False)["blocked_by"], [rel("LOC-2", "ready")]
        )

    def test_one_issue_in_both_directions(self):
        new = new_issue(title="T", body="Body", blocks=("LOC-2",), blocked_by=("LOC-2",))
        self.refused(
            lambda: self.issues().create(new),
            BOTH_DIRECTIONS.format(identifier="LOC-2"),
        )


class Edit(Board):
    def test_each_field(self):
        path = self.write("pm/ready", "LOC-1", description="Old.", history=READY)
        change = issue_change(
            "LOC-1",
            title="New",
            body="New text\n",
            task="build",
            client='x"y',
            priority=Priority.URGENT,
        )
        got = self.issues().edit(change)
        self.assertEqual(got, self.receipt("LOC-1", path, "edit", "changed"))
        self.assertEqual(
            self.read(path),
            '---\nid: LOC-1\ntitle: "New"\ncreated: 2026-10-19T14:02:00-04:00\ntask: "build"\n'
            'client: "x\\"y"\npriority: 1\n---\n### Description\nNew text\n### History\n' + READY,
        )
        view = self.issues().view("LOC-1", full=False)
        self.assertEqual(
            (view["title"], view["description"], view["labels"]),
            ("New", "New text", [{"id": "build", "name": "build"}, {"id": 'x"y', "name": 'x"y'}]),
        )

    def test_values_already_so_write_nothing(self):
        path = self.write(
            "pm/ready",
            "LOC-1",
            front="task: build\nclient: claude\npriority: 3\n",
            description="Old.",
            history=READY,
        )
        change = issue_change(
            "LOC-1",
            title="LOC-1 title",
            body="\nOld.\n",
            task="build",
            client="claude",
            priority=Priority.MEDIUM,
        )
        before = self.snapshot()
        got = self.issues().edit(change)
        self.assertEqual(got, self.receipt("LOC-1", path, "edit", "unchanged"))
        self.assertEqual(self.snapshot(), before)

    def test_an_archived_issue_stays_archived(self):
        path = self.write("archive", "LOC-1", history="- 2026-10-19 14:02 done self\n")
        got = self.issues().edit(issue_change("LOC-1", title="New"))
        self.assertEqual(got, self.receipt("LOC-1", path, "edit", "changed"))
        self.assertIn('title: "New"\n', self.read(path))

    def test_a_role_moves_the_file_to_its_folder_and_changes_nothing_else(self):
        for role in ("engineer", "me", "unassigned"):
            with self.subTest(role=role):
                old = self.write("pm/ready", "LOC-1", description="Old.", history=READY)
                before = self.read(old)
                got = self.issues().edit(issue_change("LOC-1", role=role))
                path = os.path.join(self.board, role, "ready", "LOC-1 x.md")
                self.assertEqual(got, self.receipt("LOC-1", path, "edit", "changed"))
                self.assertFalse(os.path.exists(old))
                self.assertEqual(self.read(path), before)
                os.remove(path)

    def test_a_role_with_another_field_is_one_write(self):
        self.write("pm/ready", "LOC-1", history=READY)
        got = self.issues().edit(issue_change("LOC-1", role="engineer", title="New"))
        path = os.path.join(self.board, "engineer/ready/LOC-1 x.md")
        self.assertEqual(got, self.receipt("LOC-1", path, "edit", "changed"))
        self.assertIn('title: "New"\n', self.read(path))

    def test_the_role_already_assigned_writes_nothing(self):
        path = self.write("engineer/ready", "LOC-1", history=READY)
        before = self.snapshot()
        got = self.issues().edit(issue_change("LOC-1", role="engineer"))
        self.assertEqual(got, self.receipt("LOC-1", path, "edit", "unchanged"))
        self.assertEqual(self.snapshot(), before)

    def test_a_role_on_an_archived_issue_is_refused(self):
        self.write("archive", "LOC-1", history="- 2026-10-19 14:02 done self\n")
        issues = self.issues()
        self.refused(lambda: issues.edit(issue_change("LOC-1", role="engineer")), "LOC-1: archived")


class Move(Board):
    def test_moves_with_an_other_history_line(self):
        old = self.write("pm/ready", "LOC-1", history=READY)
        got = self.issues().move("LOC-1", "review")
        path = os.path.join(self.board, "pm/review/LOC-1 x.md")
        self.assertEqual(got, self.receipt("LOC-1", path, "move", "changed"))
        self.assertFalse(os.path.exists(old))
        self.assertEqual(
            self.read(path), text("LOC-1", history=READY + "- 2026-10-20 09:30 review other\n")
        )

    def test_already_there(self):
        path = self.write("pm/ready", "LOC-1", history=READY)
        before = self.snapshot()
        got = self.issues().move("LOC-1", "ready")
        self.assertEqual(got, self.receipt("LOC-1", path, "move", "unchanged"))
        self.assertEqual(self.snapshot(), before)

    def test_archived_refused(self):
        self.write("archive", "LOC-1", history="- 2026-10-19 14:02 done self\n")
        issues = self.issues()
        self.refused(lambda: issues.move("LOC-1", "ready"), "LOC-1: archived")


class CommentOn(Board):
    def test_other_comment_with_reads_id(self):
        path = self.write(
            "pm/ready", "LOC-1", comments="#### other · 2026-10-19 14:05\nfirst\n", history=READY
        )
        issues = self.issues()
        got = issues.comment("LOC-1", "Hello\n")
        self.assertEqual(
            got, self.receipt("LOC-1", path, "comment", "changed", comment_id="LOC-1#2")
        )
        self.assertIn("#### other · 2026-10-20 09:30\nHello\n", self.read(path))
        latest = issues.view("LOC-1", full=False)["comments"][0]
        self.assertEqual((latest["id"], latest["body"]), ("LOC-1#2", "Hello"))
        self.assertEqual(latest["author"], ACTOR)

    def test_an_archived_issue_stays_archived(self):
        path = self.write("archive", "LOC-1", history="- 2026-10-19 14:02 done self\n")
        got = self.issues().comment("LOC-1", "Hello")
        self.assertEqual(
            got, self.receipt("LOC-1", path, "comment", "changed", comment_id="LOC-1#1")
        )
        self.assertIn("#### other · 2026-10-20 09:30\nHello\n", self.read(path))


class Relation(Board):
    def setUp(self):
        super().setUp()
        self.one = self.write("pm/ready", "LOC-1", history=READY)
        self.two = self.write("pm/ready", "LOC-2", front="related: [LOC-1]\n", history=READY)
        self.write("pm/ready", "LOC-3", history=READY)

    def test_add_and_remove_both_directions(self):
        issues = self.issues()
        got = issues.relation("add", "LOC-1", "blocks", "LOC-2")
        self.assertEqual(got, self.receipt("LOC-1", self.one, "relation add", "changed"))
        got = issues.relation("add", "LOC-2", "blocked_by", "LOC-3")
        self.assertEqual(got, self.receipt("LOC-2", self.two, "relation add", "changed"))
        self.assertIn("blocked_by: [LOC-1, LOC-3]\n", self.read(self.two))
        before = self.snapshot()
        got = issues.relation("add", "LOC-2", "blocked_by", "LOC-1")
        self.assertEqual(got, self.receipt("LOC-2", self.two, "relation add", "unchanged"))
        self.assertEqual(self.snapshot(), before)
        got = issues.relation("remove", "LOC-1", "blocks", "LOC-2")
        self.assertEqual(got, self.receipt("LOC-1", self.one, "relation remove", "changed"))
        self.assertEqual(
            self.read(self.two),
            text("LOC-2", front="related: [LOC-1]\nblocked_by: [LOC-3]\n", history=READY),
        )
        before = self.snapshot()
        got = issues.relation("remove", "LOC-2", "blocked_by", "LOC-1")
        self.assertEqual(got, self.receipt("LOC-2", self.two, "relation remove", "unchanged"))
        self.assertEqual(self.snapshot(), before)

    def test_itself_refused(self):
        issues = self.issues()
        self.refused(
            lambda: issues.relation("add", "LOC-1", "blocks", "LOC-1"),
            "--blocks LOC-1: LOC-1 cannot block itself",
        )

    def test_an_archived_issue_stays_archived(self):
        path = self.write("archive", "LOC-4", history="- 2026-10-19 14:02 done self\n")
        got = self.issues().relation("add", "LOC-4", "blocked_by", "LOC-1")
        self.assertEqual(got, self.receipt("LOC-4", path, "relation add", "changed"))
        self.assertIn("blocked_by: [LOC-1]\n", self.read(path))


class Archive(Board):
    def test_refused_dry_run_then_archived(self):
        self.write("pm/done", "LOC-1", history=READY)
        child = self.write("pm/ready", "LOC-2", front="parent: LOC-1\n", history=READY)
        issues = self.issues()
        plan = issues.archive_plan("LOC-1")
        self.assertEqual(
            output.dry_run(plan),
            {
                "issue": {
                    "id": "LOC-1",
                    "identifier": "LOC-1",
                    "url": Path(self.board, "pm/done/LOC-1 x.md").as_uri(),
                },
                "operation": "archive",
                "dry_run": True,
                "result": "refused",
                "descendant_count": 1,
                "descendants": [{"id": "LOC-2", "identifier": "LOC-2", "state": "ready"}],
                "unfinished_count": 1,
                "unfinished": [{"id": "LOC-2", "identifier": "LOC-2", "state": "ready"}],
            },
        )
        before = self.snapshot()
        with self.assertRaises(ArchiveRefused) as cm:
            issues.archive("LOC-1")
        self.assertEqual(
            cm.exception.plan.unfinished, (SubIssue("LOC-2", "LOC-2", "ready", finished=False),)
        )
        self.assertEqual(self.snapshot(), before)
        os.remove(child)
        self.write("pm/done", "LOC-2", front="parent: LOC-1\n", history=READY)
        issues = self.issues()
        got = issues.archive("LOC-1")
        path = os.path.join(self.board, "archive/LOC-1 x.md")
        self.assertEqual(got, self.receipt("LOC-1", path, "archive", "changed"))
        self.assertTrue(os.path.exists(os.path.join(self.board, "archive/LOC-2 x.md")))
        self.assertEqual(
            issues.archive("LOC-1"), self.receipt("LOC-1", path, "archive", "unchanged")
        )
        self.assertEqual(output.dry_run(issues.archive_plan("LOC-1"))["result"], "unchanged")


class Errors(Board):
    def setUp(self):
        super().setUp()
        self.write("pm/ready", "LOC-1", history=READY)
        self.write("pm/ready", "LOC-2", history=READY)

    def test_unresolved_ids(self):
        issues = self.issues()
        for ident, message in (
            ("bad", IDENT_ERROR.format(ident="bad")),
            ("LOC-9", "LOC-9: no such issue"),
            (
                "0123abcd-0000-4000-8000-0123456789ab",
                "0123abcd-0000-4000-8000-0123456789ab: no such issue",
            ),
        ):
            for name, call in by_id(issues, ident).items():
                with self.subTest(ident=ident, method=name):
                    self.refused(call, message)

    def test_unresolved_options(self):
        issues = self.issues()
        malformed = "'bad': want an issue identifier such as TASK-1 or an issue id"
        for call, message in (
            (
                lambda: issues.create(new_issue(parent="LOC-9")),
                "--parent LOC-9: no such issue",
            ),
            (
                lambda: issues.create(new_issue(blocks=("LOC-1", "LOC-9"))),
                "--blocks LOC-9: no such issue",
            ),
            (
                lambda: issues.create(new_issue(blocked_by=("bad",))),
                f"--blocked-by {malformed}",
            ),
            (
                lambda: issues.walk(list_query(parent="LOC-9")),
                "--parent LOC-9: no such issue",
            ),
            (
                lambda: issues.relation("add", "LOC-1", "blocked_by", "LOC-9"),
                "--blocked-by LOC-9: no such issue",
            ),
            (lambda: issues.relation("remove", "LOC-1", "blocks", "bad"), f"--blocks {malformed}"),
        ):
            with self.subTest(message=message):
                self.refused(call, message)

    def test_role_task_and_client(self):
        issues = self.issues()
        role = "--role ghost: not a role: pm, engineer, me, unassigned"
        task = "--task nope: not a configured task: plan, build, light-build"
        for given, message in (
            ({"role": "ghost"}, role),
            ({"task": "nope"}, task),
            *(
                ({"client": value}, f"--client {value!r}: {CLIENT}")
                for value in ("", "a b", "a\nb", '"a', "a\x07", "a\x85")
            ),
        ):
            for name, call in by_option(issues, **given).items():
                with self.subTest(given=given, method=name):
                    self.refused(call, message)

    def test_board_errors(self):
        self.refused(
            lambda: LocalIssues(os.path.join(self.board, "none"), ROLES, prefix="LOC"),
            f"{os.path.join(self.board, 'none')}: no such board directory",
            TrackerFileError,
        )
        path = os.path.join(self.board, "pm/ready/LOC-3 x.md")
        with open(path, "w", encoding="utf-8") as file:
            file.write("no front matter\n")
        self.refused(
            lambda: LocalIssues(self.board, ROLES, prefix="LOC"),
            f"{path}: no front matter",
            TrackerFileError,
        )

    def test_a_failing_write_raises_its_os_error(self):
        issues = self.issues()
        with mock.patch("os.replace", side_effect=PermissionError(13, "Permission denied")):
            self.refused(
                lambda: issues.comment("LOC-1", "body"),
                "[Errno 13] Permission denied",
                PermissionError,
            )
