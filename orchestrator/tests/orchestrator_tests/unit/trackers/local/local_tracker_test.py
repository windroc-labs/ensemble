"""LocalTracker: the Tracker on a temp board, its reads and writes."""

import fcntl
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from ensemble.orchestrator.engine.flow.handoff import handoff, parse_handoff
from ensemble.orchestrator.engine.flow.types import (
    Approved,
    ArchivePlan,
    ArchiveRefused,
    Comment,
    Detail,
    Finished,
    Found,
    Group,
    IssueState,
    Link,
    Linked,
    Move,
    Note,
    Pick,
    Recheck,
    Ref,
    Snapshot,
    SubIssue,
)
from ensemble.orchestrator.engine.types import IssueError, TrackerFileError
from ensemble.orchestrator.trackers.local.local_tracker import ISSUE_STATES, LocalTracker
from ensemble.orchestrator.trackers.local.types import LocalState
from orchestrator_tests.case import Case

MTIME = datetime(2026, 10, 20, tzinfo=UTC)
RUN = "Run 0123abcd-0000-4000-8000-0123456789ab · started"


def at(hour, minute=0):
    return datetime(2026, 10, 19, hour, minute, tzinfo=UTC)


def text(ident, *, front="", description="", comments="", history=""):
    body = f"### Description\n{description}\n" if description else ""
    body += f"### Comments\n{comments}" if comments else ""
    body += f"### History\n{history}" if history else ""
    return f"---\nid: {ident}\ntitle: {ident} title\ncreated: 2026-10-19T14:02:00-04:00\n{front}---\n{body}"


def checked(role, assignee, task, client, *, ready):
    return Recheck(ready, role, None, assignee, None, task, client)


class Board(Case):
    def setUp(self):
        zone = mock.patch.dict(os.environ, {"TZ": "UTC0"})
        zone.start()
        self.addCleanup(time.tzset)
        self.addCleanup(zone.stop)
        time.tzset()
        self.board = os.path.join(os.path.realpath(self.home), "board")
        os.mkdir(self.board)
        self.tracker = LocalTracker(self.board, ("pm", "engineer"), prefix="LOC")

    def write(self, rel, ident, **parts):
        path = os.path.join(self.board, rel, f"{ident} x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(text(ident, **parts))
        os.utime(path, (MTIME.timestamp(), MTIME.timestamp()))
        return path


class States(Case):
    def test_display_names_are_board_directories(self):
        tracker = LocalTracker("unused", (), prefix="LOC")
        self.assertEqual(tracker.state_name(IssueState.READY), "ready/")
        self.assertEqual(tracker.state_name(IssueState.REVIEW), "review/")
        self.assertEqual(tracker.state_name(IssueState.APPROVED), "approved/")

    def test_issue_states(self):
        self.assertEqual(
            ISSUE_STATES,
            {
                LocalState.BACKLOG: IssueState.BACKLOG,
                LocalState.READY: IssueState.READY,
                LocalState.RUNNING: IssueState.RUNNING,
                LocalState.REVIEW: IssueState.REVIEW,
                LocalState.APPROVED: IssueState.APPROVED,
                LocalState.DONE: IssueState.DONE,
                LocalState.CANCELED: IssueState.CANCELED,
            },
        )


class Listing(Board):
    def test_dependency_cycles_include_backlog_but_not_ended_or_missing_issues(self):
        self.write("pm/ready", "LOC-1", front="blocked_by: [LOC-1]\n")
        self.write("pm/ready", "LOC-2", front="blocked_by: [LOC-3]\n")
        self.write("me/backlog", "LOC-3", front="blocked_by: [LOC-4]\n")
        self.write("pm/review", "LOC-4", front="blocked_by: [LOC-2]\n")
        self.write("pm/ready", "LOC-5", front="blocked_by: [LOC-6, LOC-99]\n")
        self.write("pm/done", "LOC-6", front="blocked_by: [LOC-5]\n")
        self.assertEqual(
            self.tracker.dependency_cycles(),
            (("LOC-1", "LOC-1"), ("LOC-2", "LOC-3", "LOC-4", "LOC-2")),
        )

    def test_deep_dependencies_do_not_recurse(self):
        for number in range(1, 1050):
            self.write("pm/ready", f"LOC-{number}", front=f"blocked_by: [LOC-{number + 1}]\n")
        self.assertEqual(self.tracker.dependency_cycles(), ())

    def test_assigned_lists_role_dirs_only(self):
        path = self.write("pm/ready", "LOC-1", front="priority: 2\n")
        self.write("engineer/ready", "LOC-2")
        self.write("pm/running", "LOC-3")
        for rel, ident in (
            ("unassigned/ready", "LOC-4"),
            ("me/ready", "LOC-5"),
            ("stranger/ready", "LOC-6"),
            ("stranger/running", "LOC-7"),
            ("me/running", "LOC-8"),
            ("pm/review", "LOC-9"),
            (".state/pm/ready", "LOC-11"),
        ):
            self.write(rel, ident)
        self.write("archive", "LOC-10", history="- 2026-10-19 14:02 ready self\n")
        ready = self.tracker.assigned(IssueState.READY)
        self.assertEqual([r["identifier"] for r in ready], ["LOC-2", "LOC-1"])
        self.assertEqual(
            ready[1],
            {
                "id": "LOC-1",
                "identifier": "LOC-1",
                "url": Path(path).as_uri(),
                "title": "LOC-1 title",
                "priority": 2,
                "createdAt": "2026-10-19T18:02:00.000Z",
                "assignee": {"id": "pm"},
                "project": None,
            },
        )
        self.assertEqual(self.tracker.role(ready[1]), "pm")
        running = self.tracker.assigned(IssueState.RUNNING)
        self.assertEqual([r["identifier"] for r in running], ["LOC-3"])
        with self.assertRaises(ValueError):
            self.tracker.assigned(IssueState.REVIEW)

    def test_ready_blockers(self):
        blockers = "blocked_by: [LOC-2, LOC-3, LOC-4, LOC-5, LOC-6, LOC-7, LOC-8, "
        blockers += "LOC-9, LOC-10, LOC-11, LOC-12, LOC-99]\n"
        self.write("pm/ready", "LOC-1", front=blockers)
        self.write("pm/backlog", "LOC-2")
        self.write("engineer/ready", "LOC-3")
        self.write("pm/running", "LOC-4")
        self.write("pm/review", "LOC-5")
        self.write("pm/approved", "LOC-6")
        self.write("pm/done", "LOC-7")
        self.write("me/canceled", "LOC-8")
        self.write("archive", "LOC-9", history="- 2026-10-19 14:02 done self\n")
        self.write("archive", "LOC-10", history="- 2026-10-19 14:02 review self\n")
        self.write("archive", "LOC-11")
        self.write(
            "archive",
            "LOC-12",
            history="- 2026-10-19 14:02 review self\n- 2026-10-19 15:02 canceled other\n",
        )
        got = {r["identifier"]: found for r, found in self.tracker.ready()}
        self.assertEqual(
            got,
            {
                "LOC-3": (),
                "LOC-1": (
                    "LOC-2",
                    "LOC-3",
                    "LOC-4",
                    "LOC-5",
                    "LOC-6",
                    "LOC-10",
                    "LOC-11",
                    "LOC-99",
                ),
            },
        )
        self.assertEqual([r["identifier"] for r, _ in self.tracker.ready()], ["LOC-3", "LOC-1"])

    def test_scans_once(self):
        self.write("pm/ready", "LOC-1")
        self.assertEqual(len(self.tracker.assigned(IssueState.READY)), 1)
        self.write("pm/ready", "LOC-2")
        self.assertEqual(len(self.tracker.assigned(IssueState.READY)), 1)
        self.assertEqual(len(LocalTracker(self.board, ("pm",), prefix="LOC").ready()), 2)

    def test_scan_errors_raise_on_first_read(self):
        shutil.rmtree(self.board)
        tracker = LocalTracker(self.board, ("pm",), prefix="LOC")
        with self.assertRaises(TrackerFileError) as caught:
            tracker.ready()
        self.assertEqual(caught.exception.reason, "no such board directory")


class Recheck_(Board):
    def test_recheck_picks(self):
        path = self.write("pm/ready", "LOC-1", front="task: research\nclient: c1\n")
        self.write("pm/ready", "LOC-2")
        self.tracker.ready()
        self.assertEqual(
            self.tracker.recheck("LOC-1"),
            checked("pm", {"id": "pm"}, Pick("research"), Pick("c1"), ready=True),
        )
        self.assertEqual(
            self.tracker.recheck("LOC-2"),
            checked("pm", {"id": "pm"}, Pick(), Pick(), ready=True),
        )
        with open(path, "w", encoding="utf-8") as file:
            file.write(text("LOC-1", front="task: build\n"))
        self.assertEqual(self.tracker.recheck("LOC-1").task, Pick("build"))
        os.makedirs(os.path.join(self.board, "pm/running"))
        moved = os.path.join(self.board, "pm/running/LOC-1.md")
        os.rename(path, moved)
        self.assertEqual(
            self.tracker.recheck("LOC-1"),
            checked("pm", {"id": "pm"}, Pick("build"), Pick(), ready=False),
        )
        os.makedirs(os.path.join(self.board, "me/ready"))
        os.rename(moved, os.path.join(self.board, "me/ready/LOC-1.md"))
        self.assertEqual(
            self.tracker.recheck("LOC-1"),
            checked(None, {"id": "me"}, Pick("build"), Pick(), ready=False),
        )
        os.remove(os.path.join(self.board, "me/ready/LOC-1.md"))
        self.assertFalse(self.tracker.recheck("LOC-1").ready)
        self.assertFalse(self.tracker.recheck("LOC-404").ready)


class Moves(Board):
    def test_moved_at_by_origin_derived_move(self):
        history = "- 2026-10-19 14:02 ready other\n- 2026-10-19 14:30 running self\n"
        self.write("pm/ready", "LOC-1", history=history + "- 2026-10-19 16:10 review self\n")
        self.write(
            "pm/ready",
            "LOC-2",
            history="- 2026-10-19 14:02 ready other\n" + "- 2026-10-19 15:00 ready self\n",
        )
        self.write("pm/running", "LOC-3")
        moved = self.tracker.moved_at
        self.assertEqual(moved("LOC-1", IssueState.READY, by="other"), MTIME)
        self.assertEqual(moved("LOC-1", IssueState.RUNNING), at(14, 30))
        self.assertIsNone(moved("LOC-1", IssueState.RUNNING, by="other"))
        self.assertEqual(moved("LOC-2", IssueState.READY, by="other"), at(14, 2))
        self.assertEqual(moved("LOC-2", IssueState.READY), at(15))
        self.assertEqual(moved("LOC-2", IssueState.READY, by="self"), at(15))
        self.assertEqual(moved("LOC-3", IssueState.RUNNING, by="other"), MTIME)
        self.assertIsNone(moved("LOC-3", IssueState.READY))
        self.assertIsNone(moved("LOC-404", IssueState.READY))

    def test_detail_moves_and_comments(self):
        comments = (
            "\n#### other · 2026-10-19 14:05\nlook at LOC-7\n\n"
            f"#### self · 2026-10-19 14:31 <!-- id=c1 key=run:1 -->\n{RUN}\n\n"
            "#### self · 2026-10-19 16:10 <!-- id=c3 key=result:ab12 -->\nPRD ready\n\n"
            "###\napproved, over to engineer\n"
        )
        history = (
            "- 2026-10-19 14:02 ready other\n- 2026-10-19 14:30 running self\n"
            "- 2026-10-19 16:10 review self\n"
        )
        front = "related: [LOC-7]\nblocked_by: [LOC-10]\n"
        self.write("pm/approved", "LOC-1", front=front, comments=comments, history=history)
        self.write("engineer/ready", "LOC-20", front="related: [LOC-1]\n")
        self.write("archive", "LOC-21", front="blocked_by: [LOC-1]\n")
        self.write("pm/ready", "LOC-22", front="related: [LOC-2]\n")
        got = self.tracker.detail("LOC-1")
        self.assertEqual(
            got,
            Detail(
                IssueState.APPROVED,
                (
                    Move("2026-10-19T14:02:00.000Z", IssueState.READY, None),
                    Move("2026-10-19T14:30:00.000Z", IssueState.RUNNING, IssueState.READY),
                    Move("2026-10-19T16:10:00.000Z", IssueState.REVIEW, IssueState.RUNNING),
                    Move("2026-10-20T00:00:00.000Z", IssueState.APPROVED, IssueState.REVIEW),
                ),
                (
                    Comment(
                        "2026-10-19T14:05:00.000Z", "look at LOC-7", "other", "other", run=False
                    ),
                    Comment("2026-10-19T14:31:00.000Z", RUN, "self", "self", run=True),
                    Comment("2026-10-19T16:10:00.000Z", "PRD ready", "self", "self", run=False),
                    Comment(
                        "2026-10-20T00:00:00.000Z",
                        "approved, over to engineer",
                        "other",
                        "other",
                        run=False,
                    ),
                ),
                frozenset({"LOC-7", "LOC-10", "LOC-20", "LOC-21"}),
            ),
        )
        self.write("pm/backlog", "LOC-30")
        self.assertEqual(
            LocalTracker(self.board, ("pm",), prefix="LOC").detail("LOC-30").moves,
            (Move("2026-10-20T00:00:00.000Z", IssueState.BACKLOG, None),),
        )
        self.assertIsNone(self.tracker.detail("LOC-20").moves[0].frm)
        with self.assertRaises(IssueError):
            self.tracker.detail("LOC-404")

    def test_read(self):
        self.write("pm/review", "LOC-1", history="- 2026-10-19 16:10 review self\n")
        self.write("me/backlog", "LOC-2")
        self.write("archive", "LOC-3", history="- 2026-10-19 16:10 done self\n")
        self.write("archive", "LOC-4")
        self.write("unassigned/canceled", "LOC-5", history="- 2026-10-19 16:10 ready self\n")
        read = self.tracker.read
        self.assertEqual(read("LOC-1"), Snapshot(IssueState.REVIEW, at(16, 10)))
        self.assertEqual(read("LOC-2"), Snapshot(IssueState.BACKLOG, MTIME))
        self.assertEqual(read("LOC-3"), Snapshot(IssueState.DONE, at(16, 10)))
        self.assertEqual(read("LOC-4"), Snapshot(None, None))
        self.assertEqual(read("LOC-5"), Snapshot(IssueState.CANCELED, MTIME))
        self.assertIsNone(read("LOC-404"))


class Reads(Board):
    def test_issue(self):
        description = handoff("LOC-9", "https://x/9", sources=[], instructions=[], comments=[])
        comments = (
            "\n#### self · 2026-10-19 14:31\n" + RUN + "\n\n"
            "#### self · 2026-10-19 16:10\nPRD ready\n\n"
            "#### other · 2026-10-19 15:00\nlooks good\n"
        )
        front = "related: [LOC-2, LOC-99]\nblocked_by: [LOC-3]\nlinks:\n"
        front += '  - title: "the doc"\n    url: https://x/doc\n'
        path = self.write(
            "pm/ready", "LOC-1", front=front, description=description, comments=comments
        )
        self.write("pm/review", "LOC-2")
        self.write("archive", "LOC-3", history="- 2026-10-19 16:10 done self\n")
        self.write("archive", "LOC-5")
        self.write("engineer/ready", "LOC-4", front="related: [LOC-1, LOC-5]\n")
        got = self.tracker.issue("LOC-1")
        self.assertEqual(
            (got.id, got.identifier, got.url, got.title, got.description),
            ("LOC-1", "LOC-1", Path(path).as_uri(), "LOC-1 title", description),
        )
        self.assertEqual(
            (got.created_at, got.home, got.labels), ("2026-10-19T18:02:00.000Z", None, ())
        )
        self.assertEqual(
            got.notes,
            (
                Note("looks good", "2026-10-19T15:00:00.000Z", None, "other", "other"),
                Note("PRD ready", "2026-10-19T16:10:00.000Z", None, "self", "self"),
            ),
        )
        self.assertEqual(got.links, (Link("https://x/doc"),))
        self.assertEqual(
            got.linked,
            (
                Linked("LOC-2", "LOC-2 title", "review"),
                Linked("LOC-3", "LOC-3 title", "done"),
                Linked("LOC-4", "LOC-4 title", "ready"),
            ),
        )
        self.assertIsNotNone(got.handoff)
        self.assertEqual(got.handoff, parse_handoff(description))
        self.assertEqual(
            self.tracker.issue("LOC-5").linked, (Linked("LOC-4", "LOC-4 title", "ready"),)
        )
        self.assertEqual(
            self.tracker.issue("LOC-4").linked[1], Linked("LOC-5", "LOC-5 title", "archived")
        )
        with self.assertRaises(IssueError):
            self.tracker.issue("LOC-404")

    def test_search(self):
        canceled = "\n#### other · 2026-10-19 16:00\nlast\n\n#### other · 2026-10-19 15:00\nfirst\n"
        self.write(
            "pm/ready",
            "LOC-1",
            description="Fix the PARSER bug",
            front="links:\n  - title: d\n    url: u1\n",
        )
        self.write(
            "archive",
            "LOC-2",
            description="parser bug",
            comments=canceled,
            history="- 2026-10-19 16:10 canceled self\n",
        )
        self.write("pm/review", "LOC-3", description="nothing", comments=canceled)
        self.write("me/backlog", "LOC-4", description="parser")
        got = self.tracker.search(["parser", "Bug"], ["LOC-3"])
        self.assertEqual(
            got,
            (
                Found("LOC-3", "LOC-3 title", "review", Group.IN_REVIEW, (), "nothing", ""),
                Found("LOC-2", "LOC-2 title", "canceled", Group.CANCELED, (), "parser bug", "last"),
                Found(
                    "LOC-1", "LOC-1 title", "ready", Group.TODO, ("u1",), "Fix the PARSER bug", ""
                ),
            ),
        )
        self.assertEqual(self.tracker.search([], []), ())
        self.assertEqual(
            [(f.identifier, f.group) for f in self.tracker.search(["title"], [])],
            [
                ("LOC-2", Group.CANCELED),
                ("LOC-4", Group.BACKLOG),
                ("LOC-1", Group.TODO),
                ("LOC-3", Group.IN_REVIEW),
            ],
        )

    def test_search_caps_matches_at_ten(self):
        for n in range(1, 13):
            self.write("pm/ready", f"LOC-{n}")
        self.assertEqual(len(self.tracker.search(["title"], [])), 10)
        self.assertEqual(len(self.tracker.search(["title"], ["LOC-1", "LOC-2"])), 12)

    def test_search_groups(self):
        for n, rel in enumerate(("pm/running", "pm/approved", "pm/done", "pm/backlog"), 1):
            self.write(rel, f"LOC-{n}")
        self.write("archive", "LOC-5")
        got = {f.identifier: (f.state, f.group) for f in self.tracker.search(["title"], [])}
        self.assertEqual(
            got,
            {
                "LOC-1": ("running", Group.IN_PROGRESS),
                "LOC-2": ("approved", Group.IN_PROGRESS),
                "LOC-3": ("done", Group.DONE),
                "LOC-4": ("backlog", Group.BACKLOG),
                "LOC-5": ("archived", Group.BACKLOG),
            },
        )

    def test_description_links_on_board(self):
        self.write(
            "pm/ready",
            "LOC-1",
            description="the text",
            front="links:\n  - title: a\n    url: u1\n  - title: b\n    url: u2\n",
        )
        self.write("archive", "LOC-2")
        self.assertEqual(self.tracker.description("LOC-1"), "the text")
        self.assertEqual(self.tracker.description("LOC-2"), "")
        self.assertIsNone(self.tracker.description("LOC-404"))
        self.assertEqual(self.tracker.links("LOC-1"), {"u1", "u2"})
        self.assertEqual(self.tracker.on_board("LOC-2"), "LOC-2")
        self.assertIsNone(self.tracker.on_board("LOC-404"))

    def test_archive_plan_descendants_level_by_level(self):
        root = self.write("pm/ready", "LOC-1")
        self.write("pm/done", "LOC-2", front="parent: LOC-1\n")
        self.write("engineer/ready", "LOC-3", front="parent: LOC-1\n")
        self.write("archive", "LOC-4", front="parent: LOC-1\n")
        self.write("me/canceled", "LOC-5", front="parent: LOC-1\n")
        self.write("pm/ready", "LOC-6", front="parent: LOC-3\n")
        plan = self.tracker.archive_plan("LOC-1")
        mine, done = (
            SubIssue("LOC-1", "LOC-1", "ready", finished=False),
            SubIssue("LOC-2", "LOC-2", "done", finished=True),
        )
        level = (
            SubIssue("LOC-3", "LOC-3", "ready", finished=False),
            SubIssue("LOC-5", "LOC-5", "canceled", finished=True),
            done,
        )
        deeper = SubIssue("LOC-6", "LOC-6", "ready", finished=False)
        self.assertEqual(
            plan,
            ArchivePlan(
                "LOC-1",
                "LOC-1",
                Path(root).as_uri(),
                archived=False,
                descendants=(*level, deeper),
                unfinished=(mine, level[0], deeper),
            ),
        )
        self.assertEqual(plan.outcome, "refused")
        self.assertEqual(self.tracker.archive_plan("LOC-2").outcome, "changed")

    def test_archive_plan_archived_or_missing(self):
        self.write("archive", "LOC-4", history="- 2026-10-19 16:10 ready self\n")
        self.write("pm/ready", "LOC-5", front="parent: LOC-4\n")
        plan = self.tracker.archive_plan("LOC-4")
        self.assertEqual((plan.archived, plan.descendants, plan.unfinished), (True, (), ()))
        self.assertEqual(plan.outcome, "unchanged")
        with self.assertRaisesRegex(IssueError, "LOC-404: issue not found"):
            self.tracker.archive_plan("LOC-404")

    def test_archive_plan_survives_a_parent_cycle(self):
        self.write("pm/done", "LOC-7", front="parent: LOC-8\n")
        self.write("pm/done", "LOC-8", front="parent: LOC-7\n")
        plan = self.tracker.archive_plan("LOC-7")
        self.assertEqual(plan.descendants, (SubIssue("LOC-8", "LOC-8", "done", finished=True),))
        self.assertEqual(plan.outcome, "changed")

    def test_finished(self):
        self.write("pm/done", "LOC-1", history="- 2026-10-19 16:10 done self\n")
        self.write("pm/ready", "LOC-3", front="parent: LOC-1\n")
        self.write("engineer/done", "LOC-2")
        self.write("engineer/canceled", "LOC-4")
        for rel, ident in (
            ("me/done", "LOC-5"),
            ("me/canceled", "LOC-6"),
            ("stranger/canceled", "LOC-7"),
            ("unassigned/canceled", "LOC-8"),
        ):
            self.write(rel, ident)
        self.write("archive", "LOC-9", history="- 2026-10-19 16:10 done self\n")
        self.assertEqual(
            self.tracker.finished({"pm"}),
            [
                Finished("LOC-4", "LOC-4", MTIME),
                Finished("LOC-1", "LOC-1", at(16, 10)),
            ],
        )

    def test_approved(self):
        front = "priority: 2\nparent: LOC-5\nlinks:\n  - title: the doc\n    url: https://x/doc\n"
        self.write("pm/approved", "LOC-1", front=front)
        self.write("engineer/approved", "LOC-2")
        self.write("me/approved", "LOC-3")
        self.write("archive", "LOC-4", history="- 2026-10-19 16:10 approved self\n")
        self.assertEqual(
            self.tracker.approved({"pm"}),
            [
                Approved(
                    "LOC-1",
                    "LOC-1",
                    "",
                    "LOC-1 title",
                    2,
                    "2026-10-19T14:02:00-04:00",
                    "LOC-5",
                    "LOC-5",
                    "pm",
                    (("the doc", "https://x/doc"),),
                )
            ],
        )
        self.assertEqual(
            [(a.owner, a.home_key) for a in self.tracker.approved({"pm", "engineer"})],
            [("engineer", ""), ("pm", "LOC-5")],
        )

    def test_find_comment(self):
        comments = (
            "\n#### other · 2026-10-19 14:05 <!-- id=c0 key=result:ab12 -->\nnot mine\n\n"
            "#### self · 2026-10-19 16:10 <!-- id=c3 key=result:ab12 -->\nPRD ready\n"
        )
        self.write("pm/review", "LOC-1", comments=comments)
        self.assertEqual(self.tracker.find_comment("LOC-1", ("result", "ab12")), "PRD ready")
        self.assertIsNone(self.tracker.find_comment("LOC-1", ("result", "zz")))


NOW = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
STAMPED = "- 2026-10-20 00:00 {} other\n"
WROTE = "- 2026-10-20 09:30 {} self\n"


class Written(Board):
    def setUp(self):
        super().setUp()
        self.tracker = LocalTracker(self.board, ("pm", "engineer"), now=lambda: NOW, prefix="LOC")

    def read(self, path):
        with open(path, encoding="utf-8") as file:
            return file.read()

    def next_id(self, number):
        with open(os.path.join(self.board, ".next-id"), "w", encoding="utf-8") as file:
            file.write(f"{number}\n")

    def unchanged(self, path, call):
        before = (self.read(path), os.stat(path).st_mtime_ns)
        result = call()
        self.assertEqual((self.read(path), os.stat(path).st_mtime_ns), before)
        return result


class Writes(Written):
    def test_move_renames_and_appends_history(self):
        old = self.write("pm/ready", "LOC-1", history="- 2026-10-19 14:02 ready other\n")
        self.assertIsNone(self.tracker.move("LOC-1", IssueState.REVIEW, frm=IssueState.READY))
        new = os.path.join(self.board, "pm/review/LOC-1 x.md")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(
            self.read(new),
            text("LOC-1", history="- 2026-10-19 14:02 ready other\n" + WROTE.format("review")),
        )
        self.assertEqual(self.tracker.read("LOC-1").state, IssueState.REVIEW)
        self.assertEqual(
            LocalTracker(self.board, (), prefix="LOC").read("LOC-1").state, IssueState.REVIEW
        )

    def test_claim_moves_to_running(self):
        self.write("pm/ready", "LOC-1")
        self.tracker.claim("LOC-1")
        self.assertEqual(
            self.read(os.path.join(self.board, "pm/running/LOC-1 x.md")),
            text("LOC-1", history=STAMPED.format("ready") + WROTE.format("running")),
        )
        self.assertEqual(self.tracker.moved_at("LOC-1", IssueState.READY, by="other"), MTIME)

    def test_move_to_its_state_writes_nothing(self):
        path = self.write("pm/review", "LOC-1")
        self.assertIsNone(
            self.unchanged(path, lambda: self.tracker.move("LOC-1", IssueState.REVIEW))
        )

    def test_a_guarded_move_of_a_moved_issue_writes_nothing(self):
        path = self.write("pm/ready", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        left = self.unchanged(
            path, lambda: self.tracker.move("LOC-1", IssueState.REVIEW, frm=IssueState.RUNNING)
        )
        self.assertEqual(left, "ready")

    def test_comment_and_move_of_a_moved_issue_posts_nothing(self):
        path = self.write("pm/ready", "LOC-1")
        left = self.unchanged(
            path,
            lambda: self.tracker.comment_and_move(
                "LOC-1", "b", to=IssueState.REVIEW, frm=IssueState.RUNNING, notify=True
            ),
        )
        self.assertEqual(left, "ready")

    def test_comment_and_move(self):
        self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        left = self.tracker.comment_and_move(
            "LOC-1", "Stopped.", to=IssueState.REVIEW, frm=IssueState.RUNNING, notify=True
        )
        self.assertIsNone(left)
        self.assertEqual(
            self.read(os.path.join(self.board, "pm/review/LOC-1 x.md")),
            text(
                "LOC-1",
                comments="\n#### self · 2026-10-20 09:30\nStopped.\n\n",
                history="- 2026-10-19 14:30 running self\n" + WROTE.format("review"),
            ),
        )

    def test_comment_with_ref_has_id_and_key(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.comment("LOC-1", "Started.", ("sid", "start"))
        self.assertIn(
            "### Comments\n\n#### self · 2026-10-20 09:30 <!-- id=c1 key=sid:start -->\nStarted.\n",
            self.read(path),
        )
        self.assertEqual(self.tracker.find_comment("LOC-1", ("sid", "start")), "Started.")
        self.assertEqual(
            LocalTracker(self.board, (), prefix="LOC").find_comment("LOC-1", ("sid", "start")),
            "Started.",
        )

    def test_comment_twice_with_a_ref_writes_once(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.comment("LOC-1", "Started.", ("sid", "start"))
        self.unchanged(path, lambda: self.tracker.comment("LOC-1", "Again.", ("sid", "start")))

    def test_comment_without_ref(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.comment("LOC-1", "Note.")
        self.assertIn("\n#### self · 2026-10-20 09:30\nNote.\n", self.read(path))
        self.assertEqual(
            [(c.body, c.origin) for c in self.tracker.detail("LOC-1").comments], [("Note.", "self")]
        )

    def test_put_comment_edits_in_place(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.put_comment("LOC-1", "Run · running", ("sid", "session"))
        self.tracker.comment("LOC-1", "Started.", ("sid", "start"))
        before = self.read(path)
        self.assertTrue(self.tracker.put_comment("LOC-1", "Run · done", ("sid", "session")))
        self.assertEqual(self.read(path), before.replace("Run · running", "Run · done"))
        self.assertEqual(self.tracker.find_comment("LOC-1", ("sid", "session")), "Run · done")

    def test_put_comment_appends_when_absent(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.assertTrue(self.tracker.put_comment("LOC-1", "Run · running", ("sid", "session")))
        self.assertIn("<!-- id=c1 key=sid:session -->\nRun · running\n", self.read(path))

    def test_put_comment_escapes_the_body(self):
        self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.put_comment("LOC-1", "a", ("sid", "session"))
        self.tracker.put_comment("LOC-1", "b\r### History\u2028###", ("sid", "session"))
        self.assertEqual(
            LocalTracker(self.board, (), prefix="LOC").find_comment("LOC-1", ("sid", "session")),
            "b\n ### History\n ###",
        )

    def test_a_write_stamps_first(self):
        path = self.write(
            "pm/review",
            "LOC-1",
            comments="\n###\nplease redo\n",
            history="- 2026-10-19 14:30 running self\n",
        )
        self.tracker.comment("LOC-1", "Seen.")
        self.assertEqual(
            self.read(path),
            text(
                "LOC-1",
                comments="\n#### other · 2026-10-20 00:00\nplease redo\n\n#### self · 2026-10-20 09:30\nSeen.\n",
                history="- 2026-10-19 14:30 running self\n" + STAMPED.format("review"),
            ),
        )

    def test_link_adds_once(self):
        path = self.write("pm/review", "LOC-1", history="- 2026-10-19 14:30 review self\n")
        self.tracker.link("LOC-1", "https://x/pull/1", "PR")
        self.assertEqual(self.tracker.links("LOC-1"), {"https://x/pull/1"})
        self.unchanged(path, lambda: self.tracker.link("LOC-1", "https://x/pull/1", "PR again"))

    def test_retitle(self):
        self.write("pm/review", "LOC-1", history="- 2026-10-19 14:30 review self\n")
        self.tracker.retitle("LOC-1", "PRD: 新")
        self.assertEqual(self.tracker.issue("LOC-1").title, "PRD: 新")
        self.assertEqual(LocalTracker(self.board, (), prefix="LOC").issue("LOC-1").title, "PRD: 新")

    def test_notify_subscribes_no_one(self):
        self.write("pm/review", "LOC-1")
        self.assertEqual(self.tracker.notify("LOC-1", ("a@example.com",)), "")

    def test_unarchive_a_live_issue(self):
        path = self.write("pm/review", "LOC-1")
        self.unchanged(path, lambda: self.tracker.unarchive("LOC-1"))

    def test_missing_and_archived_issues(self):
        self.write("pm/done", "LOC-1")
        self.write("archive", "LOC-2", history="- 2026-10-19 14:30 done self\n")
        with self.assertRaises(IssueError) as caught:
            self.tracker.move("LOC-404", IssueState.DONE)
        self.assertEqual(str(caught.exception), "LOC-404: issue not found")
        with self.assertRaises(IssueError) as caught:
            self.tracker.move("LOC-2", IssueState.READY)
        self.assertEqual(str(caught.exception), "LOC-2: archived")
        self.tracker.comment("LOC-2", "Still readable.")
        self.assertEqual(self.tracker.detail("LOC-2").comments[-1].body, "Still readable.")

    def test_target_exists(self):
        path = self.write("pm/ready", "LOC-1")
        taken = self.write("pm/running", "LOC-9")
        os.rename(taken, os.path.join(self.board, "pm/running/LOC-1 x.md"))
        with self.assertRaises(TrackerFileError) as caught:
            self.tracker.claim("LOC-1")
        self.assertEqual(caught.exception.path, os.path.join(self.board, "pm/running/LOC-1 x.md"))
        self.assertEqual(caught.exception.reason, "file exists")
        self.assertTrue(os.path.exists(path))

    def test_symlinked_state_dir(self):
        path = self.write("pm/ready", "LOC-1")
        self.tracker.read("LOC-1")  # scanned before the link: a scan refuses it
        outside = os.path.join(self.home, "outside")
        os.mkdir(outside)
        os.symlink(outside, os.path.join(self.board, "pm/running"))
        with self.assertRaises(TrackerFileError) as caught:
            self.tracker.claim("LOC-1")
        self.assertEqual(
            (caught.exception.path, caught.exception.reason),
            (os.path.join(self.board, "pm/running"), "not a directory"),
        )
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.listdir(outside), [])

    def racing(self, path, edits):
        """os.fstat, which reading an issue file calls once it has its text, then a person's edit:
        edits' next line appended (None: none)."""
        real = os.fstat

        def fstat(fd):
            found = real(fd)
            if (line := next(edits, None)) is not None:
                with open(path, "a", encoding="utf-8") as file:
                    file.write(line)
            return found

        return mock.patch("os.fstat", side_effect=fstat)

    def test_a_changed_file_is_retried(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.read("LOC-1")
        edit = "- 2026-10-19 15:00 running other\n"
        with self.racing(path, iter([None, edit])):  # after the write's read, before its check
            self.tracker.comment("LOC-1", "Seen.")
        written = self.read(path)
        self.assertIn(f"running self\n{edit}", written)
        self.assertIn("Seen.", written)

    def test_a_file_changed_three_times_is_refused(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.read("LOC-1")
        edits = (f"- 2026-10-19 15:{n:02} running other\n" for n in range(60))
        with self.racing(path, edits), self.assertRaises(TrackerFileError) as caught:
            self.tracker.comment("LOC-1", "Seen.")
        self.assertEqual(
            (caught.exception.path, caught.exception.reason), (path, "changed while writing")
        )
        self.assertNotIn("Seen.", self.read(path))

    def test_a_file_moved_while_writing_is_found_again(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.read("LOC-1")
        moved = os.path.join(self.board, "pm/review/LOC-1 x.md")
        os.mkdir(os.path.dirname(moved))
        real, calls = os.fstat, iter(range(100))

        def fstat(fd):
            found = real(fd)
            if next(calls) == 1:  # the write's own read: a person moves it before the re-check
                os.rename(path, moved)
            return found

        with mock.patch("os.fstat", side_effect=fstat):
            self.tracker.comment("LOC-1", "Seen.")
        self.assertFalse(os.path.exists(path))
        self.assertIn("Seen.", self.read(moved))
        self.assertIn(STAMPED.format("review"), self.read(moved))

    def test_a_file_moved_before_its_rename_is_found_again(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.tracker.read("LOC-1")
        moved = os.path.join(self.board, "pm/ready/LOC-1 x.md")
        os.mkdir(os.path.dirname(moved))
        real, raced = os.rename, []

        def rename(src, dst):
            if not raced:
                raced.append(src)
                real(src, moved)
            return real(src, dst)

        with mock.patch("os.rename", side_effect=rename):
            left = self.tracker.move("LOC-1", IssueState.REVIEW, frm=IssueState.RUNNING)
        self.assertEqual((left, raced), ("ready", [path]))
        self.assertTrue(os.path.exists(moved))
        self.assertFalse(os.path.exists(os.path.join(self.board, "pm/review/LOC-1 x.md")))

    def test_a_file_deleted_while_writing(self):
        path = self.write("pm/running", "LOC-1", history="- 2026-10-19 14:30 running self\n")
        self.write("pm/done", "LOC-2")
        self.tracker.read("LOC-1")
        real, calls = os.fstat, iter(range(100))

        def fstat(fd):
            found = real(fd)
            if next(calls) == 1:
                os.unlink(path)
            return found

        with mock.patch("os.fstat", side_effect=fstat), self.assertRaises(IssueError) as caught:
            self.tracker.comment("LOC-1", "Seen.")
        self.assertEqual(str(caught.exception), "LOC-1: issue not found")


ONCE = ("LOC-1", "engineer", "2026-10-19T16:10:00.000Z")
KEY = "LOC-1:engineer:2026-10-19T16:10:00.000Z"
NOTE = f"#### self · 2026-10-19 16:10 <!-- id=c1 key={KEY} -->\nCreated by promote.\n"


class Create(Written):
    def create(self, once=ONCE, *, home="LOC-5", title="ENG: Queue", **given):
        fields = {"owner": "engineer", "priority": 2, "description": "Handoff from LOC-1: u\n\nGo."}
        return self.tracker.create(once, home=home, title=title, **(fields | given))

    def test_writes_the_next_issue(self):
        self.write("pm/approved", "LOC-1")
        self.next_id(10)
        self.assertIsNone(self.tracker.created(ONCE))
        self.assertEqual(self.create(), Ref("LOC-10", "LOC-10"))
        path = os.path.join(self.board, "engineer/ready/LOC-10 ENG: Queue.md")
        self.assertEqual(self.read(os.path.join(self.board, ".next-id")), "11\n")
        fresh = LocalTracker(self.board, ("pm", "engineer"), prefix="LOC")
        self.assertEqual(fresh.created(ONCE), Ref("LOC-10", "LOC-10"))
        [ready] = fresh.assigned(IssueState.READY)
        self.assertEqual(
            (ready["identifier"], ready["title"], ready["priority"], ready["url"]),
            ("LOC-10", "ENG: Queue", 2, Path(path).as_uri()),
        )
        found = fresh.issue("LOC-10")
        self.assertEqual(found.description, "Handoff from LOC-1: u\n\nGo.")
        self.assertEqual(found.created_at, "2026-10-20T09:30:00.000Z")
        self.assertEqual([n.body for n in found.notes], ["Created by promote."])
        self.assertEqual(
            fresh.detail("LOC-10").moves,
            (Move("2026-10-20T09:30:00.000Z", IssueState.READY, None),),
        )
        self.assertIn("parent: LOC-5\n", self.read(path))
        self.assertNotIn("related", self.read(path))
        self.assertEqual(self.tracker.created(ONCE), Ref("LOC-10", "LOC-10"))

    def test_no_home_no_parent(self):
        self.write("pm/approved", "LOC-1")
        self.create(home="")
        [path] = os.listdir(os.path.join(self.board, "engineer/ready"))
        self.assertNotIn("parent:", self.read(os.path.join(self.board, "engineer/ready", path)))

    def test_existing_once_key_is_reused(self):
        for rel in ("engineer/done", "archive"):
            with self.subTest(rel):
                shutil.rmtree(self.board)
                os.mkdir(self.board)
                self.write("pm/approved", "LOC-1")
                path = self.write(
                    rel, "LOC-7", comments=NOTE, history="- 2026-10-19 16:10 done self\n"
                )
                tracker = LocalTracker(
                    self.board, ("pm", "engineer"), now=lambda: NOW, prefix="LOC"
                )
                self.assertEqual(tracker.created(ONCE), Ref("LOC-7", "LOC-7"))
                self.tracker = tracker
                self.assertEqual(self.unchanged(path, self.create), Ref("LOC-7", "LOC-7"))
                self.assertFalse(os.path.exists(os.path.join(self.board, ".next-id")))
                self.assertFalse(os.path.exists(os.path.join(self.board, "engineer/ready")))

    def test_rechecks_under_the_lock(self):
        self.write("pm/approved", "LOC-1")
        self.assertIsNone(self.tracker.created(ONCE))
        self.write("engineer/ready", "LOC-7", comments=NOTE)  # another promote, meanwhile
        self.assertEqual(self.create(), Ref("LOC-7", "LOC-7"))
        self.assertEqual(os.listdir(os.path.join(self.board, "engineer/ready")), ["LOC-7 x.md"])

    def test_id_prefix_and_collision(self):
        self.write("pm/approved", "AB-3")
        self.write("pm/done", "LOC-12")
        self.write("me/backlog", "XY-2")
        self.next_id(12)
        taken = self.write("engineer/ready", "LOC-4")
        os.rename(taken, taken := os.path.join(self.board, "engineer/ready/LOC-13 ENG: Queue.md"))
        kept = self.read(taken)
        self.assertEqual(
            self.create(), Ref("LOC-14", "LOC-14")
        )  # LOC-12 on the board, LOC-13's name taken
        self.assertEqual(self.read(taken), kept)
        self.assertEqual(self.read(os.path.join(self.board, ".next-id")), "15\n")

    def test_makes_missing_role_dirs(self):
        self.write("pm/approved", "LOC-1")
        self.create()
        self.assertEqual(len(os.listdir(os.path.join(self.board, "engineer/ready"))), 1)

    def test_refuses_a_role_dir_that_is_no_directory(self):
        outside = os.path.join(self.home, "outside")
        os.mkdir(outside)
        for rel, make in (
            ("engineer", lambda p: open(p, "w", encoding="utf-8").close()),
            ("engineer", lambda p: os.symlink(outside, p)),
            ("engineer/ready", lambda p: os.symlink(outside, p)),
            ("engineer/ready", lambda p: os.symlink(os.path.join(self.home, "gone"), p)),
        ):
            with self.subTest(rel=rel):
                shutil.rmtree(self.board)
                os.makedirs(os.path.join(self.board, "engineer"))
                self.write("pm/approved", "LOC-1")
                if rel == "engineer":
                    os.rmdir(os.path.join(self.board, "engineer"))
                target = os.path.join(self.board, rel)
                make(target)
                self.tracker = LocalTracker(
                    self.board, ("pm", "engineer"), now=lambda: NOW, prefix="LOC"
                )
                with self.assertRaises(TrackerFileError) as caught:
                    self.create()
                self.assertEqual(
                    (caught.exception.path, caught.exception.reason), (target, "not a directory")
                )
                self.assertEqual(os.listdir(outside), [])

    def test_file_name(self):
        self.write("pm/approved", "LOC-1")
        cases = {
            "a/b\nc\x00d\x7fe\tf": "a-b-c-d-e-f",
            "🙂" * 75: "🙂" * 50,
        }
        for n, (title, name) in enumerate(cases.items()):
            with self.subTest(title=title[:8]):
                made = self.create(("LOC-1", "engineer", str(n)), title=title)
                path = os.path.join(self.board, "engineer/ready", f"{made.ident} {name}.md")
                self.assertTrue(os.path.isfile(path), os.listdir(os.path.dirname(path)))
                self.assertEqual(self.tracker.issue(made.key).title, title)


class Add(Written):
    def test_writes_the_rendered_issue_with_the_next_id(self):
        self.write("pm/approved", "AB-3")
        self.next_id(12)
        self.assertEqual(list(self.tracker.files()), ["AB-3"])
        self.write("pm/done", "LOC-12")  # meanwhile: add rescans
        rendered = []

        def render(ident):
            rendered.append(ident)
            return text(ident, description="Go.")

        made = self.tracker.add("engineer", LocalState.BACKLOG, "a/b: Queue", render)
        self.assertEqual((made, rendered), (Ref("LOC-13", "LOC-13"), ["LOC-13"]))
        path = os.path.join(self.board, "engineer/backlog/LOC-13 a-b: Queue.md")
        self.assertEqual(self.read(path), text("LOC-13", description="Go."))
        self.assertEqual(self.read(os.path.join(self.board, ".next-id")), "14\n")
        found = self.tracker.files()["LOC-13"]
        self.assertEqual(
            (found.path, found.assignee, found.state, found.description),
            (path, "engineer", LocalState.BACKLOG, "Go."),
        )

    def test_skips_a_taken_id_or_name(self):
        self.write("pm/approved", "LOC-3")
        taken = self.write("unassigned/ready", "LOC-1")
        os.rename(taken, taken := os.path.join(self.board, "unassigned/ready/LOC-4 Queue.md"))
        kept = self.read(taken)
        self.next_id(3)
        made = self.tracker.add("unassigned", LocalState.READY, "Queue", text)
        self.assertEqual(made, Ref("LOC-5", "LOC-5"))  # LOC-3 on the board, LOC-4's name taken
        self.assertEqual(self.read(taken), kept)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.board, "unassigned/ready"))),
            ["LOC-4 Queue.md", "LOC-5 Queue.md"],
        )

    def test_a_board_with_no_issue_uses_loc(self):
        os.mkdir(os.path.join(self.board, "unassigned"))
        made = self.tracker.add("pm", LocalState.READY, "Queue", text)
        self.assertEqual(made, Ref("LOC-1", "LOC-1"))
        self.assertEqual(self.tracker.issue("LOC-1").title, "LOC-1 title")

    def test_a_configured_prefix_is_only_for_an_empty_board(self):
        os.mkdir(os.path.join(self.board, "unassigned"))
        self.tracker = LocalTracker(self.board, ("pm",), prefix="AGE")
        self.assertEqual(
            self.tracker.add("pm", LocalState.READY, "Queue", text), Ref("AGE-1", "AGE-1")
        )
        self.write("pm/backlog", "OTHER-19")
        self.assertEqual(
            self.tracker.add("pm", LocalState.READY, "Next", text), Ref("OTHER-2", "OTHER-2")
        )

    def test_writes_under_the_board_lock(self):
        self.write("pm/approved", "LOC-1")

        def render(ident):
            fd = os.open(os.path.join(self.board, ".lock"), os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            return text(ident)

        self.assertEqual(
            self.tracker.add("pm", LocalState.READY, "Queue", render), Ref("LOC-2", "LOC-2")
        )


class Stamped(Written):
    def test_derived_times_survive_a_stamp(self):
        """promote's once key holds the derived move's time: a write's stamp must not change it."""
        path = self.write(
            "pm/approved",
            "LOC-1",
            comments="###\nGo on.\n",
            history="- 2026-10-19 16:10 review self\n",
        )
        self.write("engineer/ready", "LOC-2")
        late = MTIME.timestamp() + 27.5
        os.utime(path, (late, late))
        before = self.tracker.detail("LOC-1")
        self.assertEqual(
            (before.moves[-1].at, before.comments[0].at),
            ("2026-10-20T00:00:00.000Z", "2026-10-20T00:00:00.000Z"),
        )
        self.tracker.relate("LOC-1", "LOC-2")
        after = LocalTracker(self.board, ("pm", "engineer"), prefix="LOC").detail("LOC-1")
        self.assertEqual(after.moves, before.moves)
        self.assertEqual(after.comments[0].at, before.comments[0].at)

    def test_derived_times_survive_a_stamp_in_a_dst_fold(self):
        """01:30 New York time twice on 2026-11-01: a derived time is the one its History line reads."""
        zone = mock.patch.dict(os.environ, {"TZ": "America/New_York"})
        zone.start()
        self.addCleanup(time.tzset)
        self.addCleanup(zone.stop)
        time.tzset()
        for at in ("2026-11-01T06:30:20+00:00", "2026-11-01T05:30:20+00:00"):  # EST, then EDT
            with self.subTest(at=at):
                shutil.rmtree(self.board)
                os.mkdir(self.board)
                path = self.write(
                    "pm/approved",
                    "LOC-1",
                    comments="###\nGo on.\n",
                    history="- 2026-10-31 12:00 review self\n",
                )
                self.write("engineer/ready", "LOC-2")
                when = datetime.fromisoformat(at).timestamp()
                os.utime(path, (when, when))
                before = LocalTracker(self.board, ("pm", "engineer"), prefix="LOC").detail("LOC-1")
                LocalTracker(self.board, ("pm", "engineer"), prefix="LOC").relate("LOC-1", "LOC-2")
                after = LocalTracker(self.board, ("pm", "engineer"), prefix="LOC").detail("LOC-1")
                self.assertIn(" approved other\n", self.read(path))
                self.assertEqual(after.moves, before.moves)
                self.assertEqual(after.comments[0].at, before.comments[0].at)


class Relate(Written):
    def test_adds_once(self):
        path = self.write("pm/done", "LOC-1", front="related: [LOC-7]\n")
        self.write("engineer/ready", "LOC-2")
        self.write("engineer/ready", "LOC-3", front="related: [LOC-1]\n")
        self.tracker.relate("LOC-1", "LOC-2")
        self.assertIn("related: [LOC-7, LOC-2]\n", self.read(path))
        self.assertEqual(
            self.tracker.detail("LOC-1").related, frozenset({"LOC-7", "LOC-2", "LOC-3"})
        )
        for other in ("LOC-2", "LOC-3", "LOC-7"):
            with self.subTest(other):
                self.unchanged(path, lambda o=other: self.tracker.relate("LOC-1", o))


def archived_by(assignee, minute=10):
    return f"#### self · 2026-10-19 16:{minute} <!-- id=c1 key=archived:{assignee} -->\nArchived.\n"


class Archive(Written):
    def locks(self):
        """The board lock's exclusive takes, counted at fcntl.flock."""
        real, taken = fcntl.flock, []

        def counted(fd, operation):
            taken.append(operation)
            return real(fd, operation)

        return mock.patch("fcntl.flock", side_effect=counted), taken

    def test_moves_issue_and_descendants_flat(self):
        self.write("pm/done", "LOC-1", history="- 2026-10-19 14:30 done self\n")
        self.write("engineer/canceled", "LOC-2", front="parent: LOC-1\n")
        self.write(
            "engineer/done",
            "LOC-3",
            front="parent: LOC-2\n",
            history="- 2026-10-19 15:00 done self\n",
        )
        old = self.write(
            "archive", "LOC-4", front="parent: LOC-1\n", history="- 2026-10-19 15:00 done self\n"
        )
        before = self.read(old)
        patched, taken = self.locks()
        with patched:
            self.assertTrue(self.tracker.archive("LOC-1"))
        self.assertEqual(taken, [fcntl.LOCK_EX])
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.board, "archive"))),
            ["LOC-1 x.md", "LOC-2 x.md", "LOC-3 x.md", "LOC-4 x.md"],
        )
        self.assertEqual(self.read(old), before)
        self.assertEqual(os.listdir(os.path.join(self.board, "pm/done")), [])
        two = self.read(os.path.join(self.board, "archive/LOC-2 x.md"))
        self.assertEqual(
            two,
            text(
                "LOC-2",
                front="parent: LOC-1\n",
                comments="\n#### self · 2026-10-20 09:30 <!-- id=c1 key=archived:engineer -->\n"
                "Archived from engineer/canceled/.\n\n",
                history=STAMPED.format("canceled"),
            ),
        )
        fresh = LocalTracker(self.board, ("pm", "engineer"), prefix="LOC")
        self.assertEqual(
            [fresh.read(i).state for i in ("LOC-1", "LOC-2", "LOC-3")],
            [IssueState.DONE, IssueState.CANCELED, IssueState.DONE],
        )
        self.assertEqual(fresh.detail("LOC-1").comments[-1].body, "Archived from pm/done/.")
        self.assertFalse(self.tracker.archive("LOC-1"))

    def test_refuses_unfinished_descendant(self):
        path = self.write("pm/done", "LOC-1")
        self.write("engineer/ready", "LOC-2", front="parent: LOC-1\n")
        with self.assertRaises(ArchiveRefused) as caught:
            self.unchanged(path, lambda: self.tracker.archive("LOC-1"))
        self.assertEqual([s.ident for s in caught.exception.plan.unfinished], ["LOC-2"])
        self.assertFalse(os.path.exists(os.path.join(self.board, "archive")))

    def test_rechecks_under_the_lock(self):
        path = self.write("pm/done", "LOC-1")
        sub = self.write("engineer/done", "LOC-2", front="parent: LOC-1\n")
        plan = self.tracker.archive_plan("LOC-1")
        self.assertEqual(plan.outcome, "changed")
        os.makedirs(os.path.join(self.board, "engineer/ready"))
        os.rename(sub, os.path.join(self.board, "engineer/ready/LOC-2 x.md"))  # reopened meanwhile
        with self.assertRaises(ArchiveRefused):
            self.unchanged(path, lambda: self.tracker.archive("LOC-1", plan))
        self.assertFalse(os.path.exists(os.path.join(self.board, "archive")))

    def test_occupied_archive_target(self):
        path = self.write("pm/done", "LOC-1")
        taken = self.write("archive", "LOC-9", history="- 2026-10-19 15:00 done self\n")
        os.rename(taken, taken := os.path.join(self.board, "archive/LOC-1 x.md"))
        with self.assertRaises(TrackerFileError) as caught:
            self.tracker.archive("LOC-1")
        self.assertEqual((caught.exception.path, caught.exception.reason), (taken, "file exists"))
        self.assertTrue(os.path.exists(path))

    def test_archived_meanwhile_is_not_archived(self):
        path = self.write("pm/done", "LOC-1", history="- 2026-10-19 14:30 done self\n")
        plan = self.tracker.archive_plan("LOC-1")
        self.assertEqual(plan.outcome, "changed")
        os.mkdir(os.path.join(self.board, "archive"))
        os.rename(path, path := os.path.join(self.board, "archive/LOC-1 x.md"))  # by another hand
        self.assertFalse(self.unchanged(path, lambda: self.tracker.archive("LOC-1", plan)))

    def test_unarchive_follows_the_latest_archive(self):
        path = self.write("pm/done", "LOC-1", history="- 2026-10-19 14:30 done self\n")
        for owner in ("pm", "engineer", "pm"):
            with self.subTest(owner=owner):
                if not path.startswith(os.path.join(self.board, owner, "")):  # a person's move
                    os.makedirs(os.path.join(self.board, owner, "done"), exist_ok=True)
                    os.rename(path, path := os.path.join(self.board, owner, "done/LOC-1 x.md"))
                self.tracker = LocalTracker(
                    self.board, ("pm", "engineer"), now=lambda: NOW, prefix="LOC"
                )
                self.assertTrue(self.tracker.archive("LOC-1"))
                self.tracker.unarchive("LOC-1")
                self.assertTrue(os.path.exists(path), os.listdir(self.board))
        notes = [
            c.body for c in LocalTracker(self.board, (), prefix="LOC").detail("LOC-1").comments
        ]
        self.assertEqual(notes, ["Archived from engineer/done/.", "Archived from pm/done/."])

    def test_one_note_per_assignee(self):
        self.write("pm/done", "LOC-1", history="- 2026-10-19 14:30 done self\n")
        self.tracker.archive("LOC-1")
        self.tracker.unarchive("LOC-1")
        self.tracker.archive("LOC-1")
        notes = [
            c.body for c in LocalTracker(self.board, (), prefix="LOC").detail("LOC-1").comments
        ]
        self.assertEqual(notes, ["Archived from pm/done/."])


class Unarchive(Written):
    def setUp(self):
        super().setUp()
        os.mkdir(os.path.join(self.board, "pm"))  # a board holds an assignee dir

    def test_restores_recorded_assignee(self):
        notes = archived_by("pm", 10) + "\n" + archived_by("engineer", 20).replace("c1", "c2")
        path = self.write(
            "archive", "LOC-1", comments=notes, history="- 2026-10-19 16:00 done self\n"
        )
        before = self.read(path)
        self.tracker.unarchive("LOC-1")
        restored = os.path.join(self.board, "engineer/done/LOC-1 x.md")
        self.assertEqual(self.read(restored), before)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.tracker.read("LOC-1").state, IssueState.DONE)
        self.assertIsNone(self.tracker.move("LOC-1", IssueState.REVIEW, frm=IssueState.DONE))
        self.assertTrue(os.path.exists(os.path.join(self.board, "engineer/review/LOC-1 x.md")))

    def test_hand_archived_goes_to_unassigned(self):
        self.write("archive", "LOC-1", history="- 2026-10-19 16:00 review other\n")
        self.write("archive", "LOC-2")
        self.tracker.unarchive("LOC-1")
        self.tracker.unarchive("LOC-2")
        self.assertTrue(os.path.exists(os.path.join(self.board, "unassigned/review/LOC-1 x.md")))
        self.assertTrue(os.path.exists(os.path.join(self.board, "unassigned/backlog/LOC-2 x.md")))

    def test_bad_recorded_assignee(self):
        outside = os.listdir(self.home)
        for assignee in ("../..", "a/b", ".state", "archive", "%2E%2E", "a%2Fb", "a%00b", ""):
            with self.subTest(assignee=assignee):
                path = self.write(
                    "archive",
                    "LOC-1",
                    comments=archived_by(assignee),
                    history="- 2026-10-19 16:00 done self\n",
                )
                self.tracker = LocalTracker(self.board, ("pm",), now=lambda: NOW, prefix="LOC")
                self.tracker.unarchive("LOC-1")
                restored = os.path.join(self.board, "unassigned/done/LOC-1 x.md")
                self.assertTrue(os.path.exists(restored))
                os.unlink(restored)
                self.assertFalse(os.path.exists(path))
                self.assertEqual(
                    sorted(os.listdir(self.board)), [".lock", "archive", "pm", "unassigned"]
                )
                self.assertEqual(os.listdir(self.home), outside)

    def test_occupied_target(self):
        path = self.write(
            "archive", "LOC-1", comments=archived_by("pm"), history="- 2026-10-19 16:00 done self\n"
        )
        taken = self.write("pm/done", "LOC-9")
        os.rename(taken, taken := os.path.join(self.board, "pm/done/LOC-1 x.md"))
        with self.assertRaises(TrackerFileError) as caught:
            self.tracker.unarchive("LOC-1")
        self.assertEqual((caught.exception.path, caught.exception.reason), (taken, "file exists"))
        self.assertTrue(os.path.exists(path))
