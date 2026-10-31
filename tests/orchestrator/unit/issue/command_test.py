"""ensemble-orchestrator issue: help, the shared stdout/stderr/exit rules, and read, move, edit, comment,
archive, relation and label swap, add and remove through main; on a local board, its backend choice and refusals.
"""

import io
import json
import os
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import config
from ensemble.orchestrator.engine.flow.types import IssueState, Priority
from ensemble.orchestrator.engine.types import (
    GraphQLErrors,
    IssueEdit,
    IssueError,
    LinearError,
    ListQuery,
    LookupQuery,
    NewIssue,
    Resource,
    Unavailable,
)
from ensemble.orchestrator.issue import command as issue
from ensemble.orchestrator.trackers.linear import client
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import (
    HARNESS_LINE,
    MANAGER_APP,
    MANAGER_IDENTITY,
    MANAGER_LINE,
    STATES,
    TASK_GROUP,
    role_id,
)
from tests.orchestrator.unit.issue_fixtures import (
    CLIENT_LABELS,
    CONFIG,
    TASK_LABELS,
    ArchiveGql,
    EditGql,
    Gql,
    IssueCase,
    LabelGql,
    RelationGql,
    Routes,
    SecondFails,
    WriteGql,
    comment,
    conn,
    create_gql,
    issue_change,
    issue_node,
    issue_row,
    label,
    list_query,
    new_issue,
    project,
    receipt_of,
    rel,
    tree,
    uid,
    write_issue,
)
from tests.orchestrator.unit.issue_fixtures import (
    edges as page_edges,
)
from tests.orchestrator.unit.keychain_fixtures import (
    NEW,
    OLD,
    SECRET,
    Endpoint,
    Keychain,
    leaks,
    record,
    renewed,
    token_http_error,
)
from tests.utils.cli_help import help_text
from tests.utils.process_fixtures import CalledProcessError, TimeoutExpired


# The manager app's Keychain with a token days from expiry and its client secret.
def manager_tokens(days=10.0):
    return Keychain(
        {
            MANAGER_IDENTITY.token_service: record(days=days),
            MANAGER_IDENTITY.secret_service: SECRET,
        }
    )


RAW_BODY = 'say "hi", `ls`, $HOME and ${x} \\n\nline two\n中文正文\n'


_UNSET_TASK = ("picks a task", "ensemble-delegate run --list", "only to force")


class Help(Case):
    def test_each_help_names_its_contract(self):
        shared = ("ensemble-orchestrator issue read TASK-1", "8192", "Exit 0", "--help")
        write = ("1024", "result unknown", "Exit 0")
        for command, phrases in (
            (
                "",
                (
                    *shared,
                    "backlog, ready, running, review, approved, done, canceled",
                    "me (the\n  [linear.manager] OAuth app) or unassigned",
                    "none, urgent, high, medium, low",
                    "Linear only (a local board exits 2): lookup, label swap, add and remove,\n"
                    "  --label, --project, --milestone.",
                    "a local board ($ENSEMBLE_BOARD_DIR) directly",
                    "create, edit, move, comment, archive, relation and label write",
                    "ensemble-orchestrator issue label add TASK-1 keep",
                    "ensemble-orchestrator issue label remove --help",
                    "ensemble-orchestrator issue edit TASK-1 --title 'Fix login' --task <task>",
                    *_UNSET_TASK,
                    "ensemble-orchestrator issue edit --help",
                    "ensemble-orchestrator issue move TASK-1 done",
                    "ensemble-orchestrator issue create --title 'Split login' --body-file note.md \\\n"
                    "    --parent TASK-1",
                ),
            ),
            ("read", shared),
            (
                "lookup",
                (
                    "teams, states, labels, projects, milestones, users",
                    "team_id",
                    "1-50 (default 20)",
                    "next_cursor",
                    "--full --output FILE",
                    "ensemble-orchestrator issue lookup milestones --project",
                    "8192",
                    "Exit 0",
                ),
            ),
            (
                "list",
                (
                    "--include-archived",
                    "parent_id",
                    "most\n  recently updated first",
                    "1-50 (default 20)",
                    "--role SPEC",
                    "unassigned (no delegate; a local board: no assignee), on Linear by the\n  delegate",
                    "ensemble-orchestrator issue list --role me",
                    "ensemble-orchestrator issue list --state ready --task <task> --priority high",
                    "ensemble-orchestrator issue lookup --help",
                    "8192",
                ),
            ),
            (
                "move",
                ("STATE", "backlog", "handoff", "configured team (linear.team)", "unchanged"),
            ),
            ("comment", ("--body-file FILE", "stdin", "comment_id", "verbatim")),
            (
                "edit",
                (
                    "--title TEXT",
                    "--body-file FILE",
                    "--task NAME",
                    "--client NAME",
                    "--priority NAME",
                    *_UNSET_TASK,
                    "every other label stays",
                    "at least one",
                    "configured team (linear.team)",
                    "unchanged",
                ),
            ),
            (
                "archive",
                ("--dry-run", "completed, canceled or duplicate", "every page", "no override"),
            ),
            (
                "create",
                (
                    "--title TEXT",
                    "--blocked-by",
                    "default backlog",
                    "--role SPEC",
                    "--task NAME",
                    "--client NAME",
                    "--priority NAME",
                    *_UNSET_TASK,
                    "--parent ID: an issue, the new one its sub-issue",
                    "relation add",
                    "never create again",
                ),
            ),
            ("relation add", ("--blocks OTHER", "--blocked-by OTHER", "same edge", "itself")),
            ("relation remove", ("--blocks OTHER", "other types", "unchanged")),
            ("label swap", ("--from OLD", "--to NEW", "label group", "every other label")),
            (
                "label add",
                ("LABEL", "label group", "every other label", "not retired", "unchanged"),
            ),
            ("label remove", ("LABEL", "every other label", "may be retired", "unchanged")),
        ):
            if command not in ("", "read", "lookup", "list"):
                example = (
                    "issue create --title" if command == "create" else f"issue {command} TASK-1"
                )
                phrases = (*phrases, *write, example)
            text = help_text(self, cli.main, ["issue", *command.split(), "--help"])
            for phrase in phrases:
                self.assertIn(phrase, text, command)

    def test_no_help_names_a_removed_option(self):
        for command in (
            "",
            "read",
            "list",
            "create",
            "edit",
            "move",
            "comment",
            "archive",
            "relation add",
            "label swap",
            "label add",
            "label remove",
        ):
            output = io.StringIO()
            with (
                self.subTest(command=command),
                redirect_stdout(output),
                self.assertRaises(SystemExit),
            ):
                cli.main(["issue", *command.split(), "--help"])
            text = output.getvalue()
            self.assertNotIn("--assignee", text)
            if command in ("", "list", "create", "edit", "move"):
                self.assertNotIn("--team", text)
            self.assertNotIn("move TASK-1 --state", text)
            self.assertNotIn("In Progress", text)

    def test_usage_errors_exit_two(self):
        for argv in (
            [],
            ["read"],
            ["read", "TASK-1", "TASK-2"],
            ["nope"],
            ["move", "TASK-1"],
            ["move", "TASK-1", "done", "ready"],
            ["move", "TASK-1", "Done"],
            ["move", "TASK-1", "in_progress"],
            ["move", "TASK-1", "--state", "done"],
            ["move", "TASK-1", "done", "--state", "done"],
            ["edit"],
            ["edit", "TASK-1", "--title", "a", "--title", "b"],
            ["edit", "TASK-1", "--body-file", "a.md", "--body-file", "b.md"],
            ["edit", "TASK-1", "--task", "a", "--task", "b"],
            ["edit", "TASK-1", "--client", "a", "--client", "b"],
            ["edit", "TASK-1", "--priority", "high", "--priority", "low"],
            ["edit", "TASK-1", "--priority", "2"],
            ["edit", "TASK-1", "--state", "done"],
            ["edit", "TASK-1", "--role", "me", "--role", "pm"],
            ["edit", "TASK-1", "--project", "a", "--project", "b"],
            ["edit", "TASK-1", "--label", "bug"],
            ["list", "--team", "TASK"],
            ["list", "--assignee", "me"],
            ["list", "--state", "Todo"],
            ["list", "--priority", "2"],
            ["create", "--title", "x", "--body-file", "a.md", "--team", "TASK"],
            ["create", "--title", "x", "--body-file", "a.md", "--assignee", "me"],
            ["create", "--title", "x", "--body-file", "a.md", "--state", "Backlog"],
            ["create", "--title", "x", "--body-file", "a.md", "--priority", "p1"],
            ["create", "--title", "x", "--body-file", "a.md", "--role", "me", "--role", "pm"],
            ["create", "--title", "x", "--body-file", "a.md", "--task", "a", "--task", "b"],
            ["create", "--title", "x", "--body-file", "a.md", "--client", "a", "--client", "b"],
            ["comment", "TASK-1"],
            ["comment", "TASK-1", "--body-file", "a.md", "--body-file", "b.md"],
            ["archive"],
            ["archive", "TASK-1", "--force"],
            ["archive", "TASK-1", "--dry-run=yes"],
            ["create", "--body-file", "a.md"],
            ["create", "--title", "x"],
            [
                "create",
                "--title",
                "x",
                "--body-file",
                "a.md",
                "--state",
                "done",
                "--state",
                "ready",
            ],
            ["create", "--title", "x", "--title", "y", "--body-file", "a.md"],
            ["create", "--title", "x", "--body-file", "a.md", "--body-file", "b.md"],
            ["create", "--title", "x", "--body-file", "a.md", "--blocks"],
            ["relation", "TASK-1", "--blocks", "TASK-2"],
            ["relation", "add", "TASK-1"],
            ["relation", "add", "TASK-1", "--blocks", "TASK-2", "--blocked-by", "TASK-3"],
            ["relation", "remove", "TASK-1", "--blocks", "TASK-2", "--blocks", "TASK-3"],
            ["relation", "add", "--blocks", "TASK-2"],
            ["label", "swap", "TASK-1", "--from", "bug"],
            ["label", "swap", "TASK-1", "--to", "bug"],
            ["label", "swap", "TASK-1", "--from", "a", "--from", "b", "--to", "c"],
            ["label", "TASK-1", "--from", "a", "--to", "b"],
            ["label", "add", "TASK-1"],
            ["label", "remove", "TASK-1"],
            ["label", "add", "TASK-1", "a", "b"],
            ["label", "remove", "TASK-1", "--from", "a"],
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as cm,
            ):
                issue.main(argv, gql=Gql(None))
            self.assertEqual(cm.exception.code, 2)


class Read(IssueCase):
    def test_reads_an_issue_without_a_run_record(self):
        gql = mock.Mock(wraps=Gql(issue_node("TASK-2", parent=rel("TASK-9"))))
        code, out, err = self.run_main(["read", "TASK-2"], gql)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith(b"\n"))
        view = json.loads(out)
        self.assertEqual(view["identifier"], "TASK-2")
        self.assertEqual(view["parent"]["identifier"], "TASK-9")
        self.assertIsNone(view["continue"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs", "runs.jsonl")))
        self.assertEqual(gql.call_args.kwargs["identity"], MANAGER_IDENTITY)

    def test_cut_fields_name_the_full_read(self):
        node = issue_node("TASK-2", comments=conn([comment(n) for n in range(1, 7)]))
        code, out, _ = self.run_main(["read", "TASK-2"], Gql(node))
        view = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(view["truncated"], ["comments"])
        self.assertEqual(
            view["continue"], "ensemble-orchestrator issue read TASK-2 --full --output FILE"
        )

    def test_stdout_stays_within_eight_kib_as_valid_json(self):
        wide = "界" * 255
        node = issue_node(
            "TASK-2",
            title=wide,
            description="述" * 5000,
            labels=conn([{"id": f"l{n}", "name": wide} for n in range(50)]),
            comments=conn([comment(n, body="评" * 400) for n in range(1, 6)]),
            children=conn([rel(f"TASK-{n}", title=wide) for n in range(10, 20)]),
            relations=conn(
                [
                    {"type": "blocks", "relatedIssue": rel(f"TASK-{n}", title=wide)}
                    for n in range(30, 40)
                ]
            ),
            inverseRelations=conn(
                [{"type": "blocks", "issue": rel(f"TASK-{n}", title=wide)} for n in range(50, 60)]
            ),
            parent=rel("TASK-9", title=wide),
        )
        code, out, _ = self.run_main(["read", "TASK-2"], Gql(node))
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out), 8192)
        view = json.loads(out.decode("utf-8"))
        self.assertEqual(view["title"], wide)
        self.assertIn("description", view["truncated"])
        self.assertIn("labels", view["truncated"])
        self.assertIsNotNone(view["continue"])
        kept = sum(len(view[f]) for f in ("labels", "comments", "children", "blocks", "blocked_by"))
        self.assertLess(kept, 50 + 5 + 30)

    def test_a_lone_surrogate_still_encodes_as_json(self):
        code, out, _ = self.run_main(["read", "TASK-2"], Gql(issue_node(description="a\ud800b")))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.decode("utf-8"))["description"], "a\ud800b")

    def test_truncated_never_names_a_dropped_comment(self):
        node = issue_node(
            "TASK-2",
            description="述" * 700,
            comments=conn([comment(n, body="评" * 400) for n in range(1, 6)]),
            relations=conn(
                [
                    {"type": "blocks", "relatedIssue": rel(f"TASK-{n}", title="界" * 200)}
                    for n in range(30, 40)
                ]
            ),
        )
        code, out, _ = self.run_main(["read", "TASK-2"], Gql(node))
        self.assertEqual(code, 0)
        view = json.loads(out)
        self.assertLess(len(view["comments"]), 5)
        for field in view["truncated"]:
            parts = field.split(".")
            if parts[0] == "comments" and len(parts) == 3:
                self.assertLess(int(parts[1]), len(view["comments"]), field)
            self.assertIn(parts[0], view)

    def test_text_too_large_even_without_lists_is_shortened(self):
        node = issue_node(
            "TASK-2",
            title="t" * 9000,
            parent=rel("TASK-9", title="p" * 3000),
            project={"id": "p1", "name": "n" * 3000},
            description="d" * 600,
        )
        code, out, err = self.run_main(["read", "TASK-2"], Gql(node))
        self.assertEqual((code, err), (0, ""))
        self.assertLessEqual(len(out), 8192)
        view = json.loads(out)
        self.assertIn("title", view["truncated"])
        self.assertLess(len(view["title"]), 9000)
        self.assertTrue(view["title"].startswith("ttt"))
        self.assertEqual(
            (view["id"], view["identifier"], view["url"], view["parent"]["identifier"]),
            ("id-TASK-2", "TASK-2", "https://linear.app/t/issue/TASK-2", "TASK-9"),
        )
        self.assertEqual(
            view["continue"], "ensemble-orchestrator issue read TASK-2 --full --output FILE"
        )
        for path, value in (
            ("parent.title", view["parent"]["title"]),
            ("project.name", view["project"]["name"]),
        ):
            if path in view["truncated"]:
                self.assertLess(len(value), 3000, path)

    def test_only_the_cut_text_is_named(self):
        code, out, _ = self.run_main(["read", "TASK-2"], Gql(issue_node(title="t" * 9000)))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["truncated"], ["title"])


class Move(IssueCase):
    def test_prints_the_receipt(self):
        gql = mock.Mock(wraps=WriteGql(write_issue()))
        code, out, err = self.run_main(["move", "TASK-2", "approved"], gql)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), receipt_of("move"))
        self.assertTrue(out.endswith(b"\n"))
        self.assertEqual(gql.call_args.kwargs["identity"], MANAGER_IDENTITY)
        self.assertEqual(gql.call_args.args[0].split("(")[0], "mutation")
        self.assertEqual(gql.call_args.kwargs["s"], STATES["handoff"])

    def test_already_there_exits_zero_unchanged(self):
        gql = WriteGql(write_issue(state=STATES["done"]))
        code, out, _ = self.run_main(["move", "TASK-2", "done"], gql)
        self.assertEqual((code, json.loads(out)["result"], gql.writes), (0, "unchanged", []))

    def test_a_long_actor_name_still_fits_one_kib(self):
        gql = WriteGql(write_issue(), viewer={"id": "u-me", "name": "名" * 2000})
        code, out, _ = self.run_main(["move", "TASK-2", "done"], gql)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out), 1024)
        receipt = json.loads(out.decode("utf-8"))
        self.assertTrue(receipt["actor"]["name"].startswith("名"))
        self.assertEqual(receipt["issue"]["identifier"], "TASK-2")

    def test_failures_print_no_receipt(self):
        node = write_issue()
        cases = (
            (WriteGql(None), 2, "no such issue"),
            (WriteGql(node, result={"success": False}), 1, "success: false"),
            (
                WriteGql(node, result=Unavailable("issueUpdate", reason="timed out")),
                1,
                "result unknown",
            ),
        )
        for gql, want, phrase in cases:
            with self.subTest(phrase=phrase):
                code, out, err = self.run_main(["move", "TASK-2", "done"], gql)
                self.assertEqual((code, out), (want, b""))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: "), err)
                self.assertIn(phrase, err)
                self.assertLessEqual(len(gql.writes), 1)


BODY = 'say "hi", `ls`, $HOME and ${x}\nline two\n中文正文\n'


class Edit(IssueCase):
    BUILD = label(0xB1, "build", group=TASK_GROUP)

    def test_prints_the_receipt(self):
        path = os.path.join(self.root, "body.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(BODY)
        labels = {"addedLabelIds": [TASK_LABELS["light-build"]], "removedLabelIds": [uid(0xB1)]}
        for argv, stdin, want in (
            (
                ["--title", "New", "--body-file", path, "--task", "light-build"],
                b"",
                {"title": "New", "description": BODY, **labels},
            ),
            (
                ["--body-file", "-", "--priority", "urgent"],
                BODY.encode(),
                {"description": BODY, "priority": 1},
            ),
        ):
            gql = mock.Mock(wraps=EditGql((self.BUILD,), [self.BUILD["id"]]))
            with self.subTest(argv=argv):
                code, out, err = self.run_main(["edit", "TASK-2", *argv], gql, stdin)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(json.loads(out), receipt_of("edit"))
                self.assertEqual(gql.call_args.kwargs["in"], want)
                self.assertNotIn("中文".encode(), out)
                self.assertEqual(gql.call_args.kwargs["identity"], MANAGER_IDENTITY)

    def test_a_role_and_a_project_reach_one_edit(self):
        other = {"id": "u-other"}
        gql = EditGql(
            (self.BUILD,),
            [],
            fields={"assignee": other, "delegate": other},
            projects=(project(4, "Ensemble"),),
        )
        code, out, err = self.run_main(
            ["edit", "TASK-2", "--role", "me", "--project", "Ensemble"], gql
        )
        self.assertEqual((code, err, json.loads(out)["result"]), (0, "", "changed"))
        want = {"delegateId": MANAGER_IDENTITY.user_id, "assigneeId": None, "projectId": uid(4)}
        self.assertEqual([w[1]["in"] for w in gql.writes], [want])

    def test_unchanged_exits_zero(self):
        gql = EditGql((self.BUILD,), [], fields={"title": "Same"})
        code, out, _ = self.run_main(["edit", "TASK-2", "--title", "Same"], gql)
        self.assertEqual((code, json.loads(out)["result"], gql.writes), (0, "unchanged", []))

    def test_usage_errors_exit_two_before_any_call(self):
        blank = os.path.join(self.root, "blank.md")
        with open(blank, "w", encoding="utf-8") as f:
            f.write("\n  \n")
        for argv, phrase in (
            (
                [],
                "edit: give at least one of --title, --body-file, --role, --project, --task, "
                "--client, --priority",
            ),
            (["--title", " \n"], "--title: empty title"),
            (["--body-file", blank], "--body-file"),
            (["--body-file", os.path.join(self.root, "nope.md")], "--body-file"),
        ):
            gql = EditGql((self.BUILD,), [])
            with self.subTest(argv=argv):
                code, out, err = self.run_main(["edit", "TASK-2", *argv], gql)
                self.assertEqual((code, out, gql.calls), (2, b"", []))
                self.assertIn(phrase, err)

    def test_failures_print_no_receipt(self):
        cases = (
            (EditGql((), []), ["--role", "nope"], 2, "--role nope: not a role"),
            (EditGql((), [], result={"success": False}), ["--title", "New"], 1, "success: false"),
            (
                EditGql((), [], result=Unavailable("issueUpdate", reason="timed out")),
                ["--title", "New"],
                1,
                "issue read TASK-2",
            ),
        )
        for gql, argv, want, phrase in cases:
            with self.subTest(phrase=phrase):
                code, out, err = self.run_main(["edit", "TASK-2", *argv], gql)
                self.assertEqual((code, out), (want, b""))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: "), err)
                self.assertIn(phrase, err)
        self.assertEqual([len(gql.writes) for gql, *_ in cases], [0, 1, 1])


class Comment(IssueCase):
    def test_posts_a_file_or_stdin_and_prints_a_receipt_without_the_body(self):
        path = os.path.join(self.root, "body.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(BODY)
        for argv, stdin in (
            (["comment", "TASK-2", "--body-file", path], b""),
            (["comment", "TASK-2", "--body-file", "-"], BODY.encode("utf-8")),
        ):
            gql = WriteGql(write_issue())
            with self.subTest(argv=argv):
                code, out, err = self.run_main(argv, gql, stdin)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(gql.writes[0][1]["b"], BODY)
                receipt = json.loads(out)
                self.assertEqual(
                    (receipt["operation"], receipt["result"], receipt["comment_id"]),
                    ("comment", "changed", "c-new"),
                )
                self.assertNotIn("中文".encode(), out)
                self.assertLessEqual(len(out), 1024)

    def test_an_unusable_body_exits_two_before_any_call(self):
        blank = os.path.join(self.root, "blank.md")
        with open(blank, "w", encoding="utf-8") as f:
            f.write("\n  \n")
        for path, stdin in ((blank, b""), (os.path.join(self.root, "nope.md"), b""), ("-", b"")):
            gql = WriteGql(write_issue())
            with self.subTest(path=path):
                code, out, err = self.run_main(
                    ["comment", "TASK-2", "--body-file", path], gql, stdin
                )
                self.assertEqual((code, out, gql.calls), (2, b"", []))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: --body-file"), err)

    def test_a_file_body_needs_no_stdin(self):
        path = os.path.join(self.root, "body.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(BODY)
        gql, out, err = WriteGql(write_issue()), io.BytesIO(), io.StringIO()
        with mock.patch("sys.stdin", None):
            code = issue.main(
                ["comment", "TASK-2", "--body-file", path],
                gql=gql,
                config_path=self.config,
                out=out,
                err=err,
            )
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(gql.writes[0][1]["b"], BODY)

    def test_a_write_outage_exits_one_as_result_unknown(self):
        gql = WriteGql(write_issue(), result=Unavailable("commentCreate", reason="timed out"))
        code, out, err = self.run_main(["comment", "TASK-2", "--body-file", "-"], gql, b"hi")
        self.assertEqual((code, out, len(gql.writes)), (1, b"", 1))
        self.assertIn("result unknown", err)
        self.assertIn("ensemble-orchestrator issue read TASK-2", err)


class Create(IssueCase):
    def body(self):
        path = os.path.join(self.root, "body.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(BODY)
        return path

    def test_prints_the_receipt_without_the_body(self):
        for argv, stdin in (
            (["--body-file", self.body(), "--blocks", "TASK-3", "TASK-4"], b""),
            (["--body-file", "-", "--label", "bug", "--blocked-by", "TASK-3"], BODY.encode()),
        ):
            gql = create_gql()
            with self.subTest(argv=argv):
                code, out, err = self.run_main(["create", "--title", "Fix", *argv], gql, stdin)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(gql.writes[0][1]["in"]["description"], BODY)
                receipt = json.loads(out)
                self.assertEqual(
                    (receipt["issue"]["identifier"], receipt["operation"], receipt["result"]),
                    ("TASK-9", "create", "changed"),
                )
                self.assertNotIn("中文".encode(), out)
                self.assertLessEqual(len(out), 1024)

    def test_a_blank_title_or_body_exits_two_before_any_call(self):
        for title, stdin in ((" \n", b"body"), ("Fix", b"\xef\xbb\xbf\n")):
            gql = create_gql()
            with self.subTest(title=title, stdin=stdin):
                code, out, err = self.run_main(
                    ["create", "--title", title, "--body-file", "-"], gql, stdin
                )
                self.assertEqual((code, out, gql.calls), (2, b"", []))
                self.assertIn("empty", err)

    def test_abstract_names_resolve_from_the_configuration(self):
        gql = create_gql()
        argv = ["create", "--title", "Fix", "--body-file", "-", "--state", "running"]
        argv += ["--role", "engineer", "--task", "build", "--client", "codex", "--priority", "low"]
        code, _, err = self.run_main(argv, gql, b"body")
        self.assertEqual((code, err), (0, ""))
        fields = gql.writes[0][1]["in"]
        self.assertEqual(
            (
                fields["stateId"],
                fields["labelIds"],
                fields["delegateId"],
                fields["assigneeId"],
                fields["priority"],
            ),
            (
                STATES["in_progress"],
                [TASK_LABELS["build"], CLIENT_LABELS["codex"]],
                role_id("engineer"),
                None,
                4,
            ),
        )

    def test_an_unresolved_reference_exits_two_and_creates_nothing(self):
        for argv, phrase in (
            (["--task", "nope"], "build, light-build"),
            (["--role", "nope"], "me, unassigned"),
            (["--label", "nope"], "lookup labels"),
        ):
            gql = create_gql()
            with self.subTest(argv=argv):
                code, out, err = self.run_main(
                    ["create", "--title", "Fix", "--body-file", "-", *argv], gql, b"body"
                )
                self.assertEqual((code, out, gql.writes), (2, b"", []))
                self.assertIn(phrase, err)

    def test_a_failed_relation_exits_one_with_the_report_and_its_commands(self):
        gql = create_gql(relations=[GraphQLErrors([{"message": "boom"}])])
        argv = ["create", "--title", "Fix", "--body-file", "-", "--blocks", "TASK-3"]
        code, out, err = self.run_main(argv, gql, b"body")
        self.assertEqual(code, 1)
        recover = "ensemble-orchestrator issue relation add TASK-9 --blocks TASK-3"
        self.assertEqual(
            json.loads(out),
            {
                **receipt_of("create", "TASK-9"),
                "result": "partial",
                "done": [],
                "failed": [{"blocks": "TASK-3", "error": "boom"}],
                "recover": [recover],
                "truncated": [],
            },
        )
        self.assertEqual(
            err,
            "ensemble-orchestrator issue: create TASK-9: created https://linear.app/t/issue/TASK-9,"
            f" but 1 of 1 blocking relations failed; add only them: {recover}; do not run create"
            " again\n",
        )

    def test_many_failed_relations_stay_within_eight_kib(self):
        many = [f"TASK-{n}" for n in range(100, 400)]
        gql = create_gql(relations=[LinearError("x" * 300)] * len(many))
        argv = ["create", "--title", "Fix", "--body-file", "-", "--blocks", *many]
        code, out, err = self.run_main(argv, gql, b"body")
        self.assertEqual(code, 1)
        self.assertLessEqual(len(out), 8192)
        self.assertLessEqual(len(err.encode()), 8192)
        self.assertTrue(json.loads(out)["truncated"])

    def test_done_and_failed_give_way_before_any_recover_command(self):
        many = [f"TASK-{n}" for n in range(100, 205)]
        failed = [LinearError("x" * 300)] * (len(many) - 2)
        gql = create_gql(relations=[{"success": True}] * 2 + failed)
        argv = ["create", "--title", "Fix", "--body-file", "-", "--blocks", *many]
        code, out, _ = self.run_main(argv, gql, b"body")
        self.assertEqual(code, 1)
        self.assertLessEqual(len(out), 8192)
        report = json.loads(out)
        self.assertEqual(
            report["recover"],
            [f"ensemble-orchestrator issue relation add TASK-9 --blocks {n}" for n in many[2:]],
        )
        self.assertEqual(report["done"], [{"blocks": "TASK-100"}, {"blocks": "TASK-101"}])
        self.assertLess(len(report["failed"]), len(failed))
        self.assertEqual(report["truncated"], ["failed"])

    def test_a_create_outage_exits_one_as_result_unknown(self):
        gql = create_gql(created=Unavailable("issueCreate", reason="timed out"))
        code, out, err = self.run_main(
            ["create", "--title", "Fix", "--body-file", "-"], gql, b"body"
        )
        self.assertEqual((code, out, len(gql.writes)), (1, b"", 1))
        self.assertIn("result unknown", err)
        self.assertIn("ensemble-orchestrator issue list --query=Fix before", err)


class InWorkerContainer(IssueCase):
    def test_worker_secrets_never_replace_the_injected_board(self):
        secrets, touched = "/run/ensemble/secrets", []
        original_stat, original_open = os.stat, open

        def stat(path, *args, **kwargs):
            if not isinstance(path, int) and os.fsdecode(path).startswith(secrets):
                touched.append(os.fsdecode(path))
                return original_stat(self.home)
            return original_stat(path, *args, **kwargs)

        def opened(path, *args, **kwargs):
            if not isinstance(path, int) and os.fsdecode(path).startswith(secrets):
                touched.append(os.fsdecode(path))
                raise FileNotFoundError(path)
            return original_open(path, *args, **kwargs)

        gql = create_gql()
        with mock.patch("os.stat", stat), mock.patch("builtins.open", opened):
            code, out, err = self.run_main(
                ["create", "--title", "Fix", "--body-file", "-"], gql, b"body"
            )
        self.assertEqual((code, err, touched), (0, "", []))
        self.assertEqual(json.loads(out)["issue"]["identifier"], "TASK-9")
        self.assertEqual(len(gql.writes), 1)


class Archive(IssueCase):
    def test_prints_the_receipt(self):
        gql = ArchiveGql(tree("TASK-1", "completed", tree("TASK-2", "canceled")))
        code, out, err = self.run_main(["archive", "TASK-1"], gql)
        self.assertEqual((code, err, len(gql.writes)), (0, "", 1))
        receipt = json.loads(out)
        self.assertEqual(
            (receipt["operation"], receipt["result"], receipt["issue"]["identifier"]),
            ("archive", "changed", "TASK-1"),
        )
        self.assertLessEqual(len(out), 1024)

    def test_already_archived_exits_zero_unchanged(self):
        gql = ArchiveGql(tree("TASK-1", "started", archived="2026-10-01"))
        code, out, _ = self.run_main(["archive", "TASK-1"], gql)
        self.assertEqual((code, json.loads(out)["result"], gql.writes), (0, "unchanged", []))

    def test_failures_print_no_receipt(self):
        refused = (
            "archive TASK-1 refused, nothing archived: 1 unfinished of 2 (it and its unarchived"
            " sub-issues): TASK-9 (Started); complete or cancel each first"
            " (ensemble-orchestrator issue archive TASK-1 --dry-run lists them)"
        )
        cases = (
            (ArchiveGql(tree("TASK-1", "completed", tree("TASK-9", "started"))), 1, refused),
            (ArchiveGql(None), 2, "no such issue"),
            (
                ArchiveGql(tree(), result=Unavailable("issueArchive", reason="timed out")),
                1,
                "result unknown",
            ),
            (ArchiveGql(tree(), result={"success": False}), 1, "success: false"),
        )
        for gql, want, phrase in cases:
            with self.subTest(phrase=phrase):
                code, out, err = self.run_main(["archive", "TASK-1"], gql)
                self.assertEqual((code, out), (want, b""))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: "), err)
                self.assertIn(phrase, err)
                self.assertLessEqual(len(err.encode("utf-8")), 8192)
        self.assertEqual([len(gql.writes) for gql, *_ in cases], [0, 0, 1, 1])

    def test_dry_run_reports_and_writes_nothing(self):
        for root, result in (
            (tree("TASK-1", "completed", tree("TASK-2", "canceled")), "changed"),
            (tree("TASK-1", "completed", tree("TASK-2", "started")), "refused"),
        ):
            gql = ArchiveGql(root)
            with self.subTest(result=result):
                code, out, err = self.run_main(["archive", "TASK-1", "--dry-run"], gql)
                self.assertEqual((code, err, gql.writes), (0, "", []))
                plan = json.loads(out)
                self.assertEqual((plan["result"], plan["descendant_count"]), (result, 1))
                self.assertEqual(plan["descendants"][0]["identifier"], "TASK-2")
                self.assertNotIn("continue", plan)
                self.assertEqual(plan["truncated"], [])

    def test_a_long_dry_run_stays_within_eight_kib_with_its_totals(self):
        kids = [tree(f"LONGTEAM-{i}", "started") for i in range(400)]
        code, out, _ = self.run_main(
            ["archive", "TASK-1", "--dry-run"], ArchiveGql(tree("TASK-1", "completed", *kids))
        )
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out), 8192)
        plan = json.loads(out)
        self.assertEqual((plan["descendant_count"], plan["unfinished_count"]), (400, 400))
        self.assertEqual(sorted(plan["truncated"]), ["descendants", "unfinished"])
        self.assertLess(len(plan["descendants"]) + len(plan["unfinished"]), 800)
        self.assertEqual(plan["unfinished"][0]["identifier"], "LONGTEAM-0")


class Relation(IssueCase):
    def test_prints_the_receipt(self):
        for op, edges in (("add", []), ("remove", [["r-1", "blocks", "TASK-1", "TASK-2"]])):
            gql = RelationGql(("TASK-1", "TASK-2"), edges)
            with self.subTest(op=op):
                code, out, err = self.run_main(
                    ["relation", op, "TASK-2", "--blocked-by", "TASK-1"], gql
                )
                self.assertEqual((code, err, len(gql.writes)), (0, "", 1))
                self.assertEqual(json.loads(out), receipt_of(f"relation {op}"))
                self.assertLessEqual(len(out), 1024)

    def test_unchanged_exits_zero(self):
        gql = RelationGql(("TASK-1", "TASK-2"))
        code, out, _ = self.run_main(["relation", "remove", "TASK-1", "--blocks", "TASK-2"], gql)
        self.assertEqual((code, json.loads(out)["result"], gql.writes), (0, "unchanged", []))

    def test_failures_print_no_receipt(self):
        board = ("TASK-1", "TASK-2")
        cases = (
            (RelationGql(board), "TASK-9", 2, "--blocks TASK-9: no such issue"),
            (RelationGql(board, result={"success": False}), "TASK-2", 1, "success: false"),
            (
                RelationGql(board, result=Unavailable("issueRelation", reason="timed out")),
                "TASK-2",
                1,
                "result unknown",
            ),
        )
        for gql, other, want, phrase in cases:
            with self.subTest(phrase=phrase):
                code, out, err = self.run_main(
                    ["relation", "add", "TASK-1", "--blocks", other], gql
                )
                self.assertEqual((code, out), (want, b""))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: "), err)
                self.assertIn(phrase, err)
        self.assertEqual([len(gql.writes) for gql, *_ in cases], [0, 1, 1])

    def test_a_keychain_failure_after_a_delete_exits_one_naming_it(self):
        twice = [["r-1", "blocks", "TASK-1", "TASK-2"], ["r-2", "blocks", "TASK-1", "TASK-2"]]
        gql = SecondFails(("TASK-1", "TASK-2"), twice, later=OSError("keychain"))
        code, out, err = self.run_main(["relation", "remove", "TASK-1", "--blocks", "TASK-2"], gql)
        self.assertEqual((code, out), (1, b""))
        for phrase in ("deleted 1 of 2", "r-1", "OSError", "issue read TASK-1"):
            self.assertIn(phrase, err)


class LabelSwap(IssueCase):
    BUG, CHORE, KEEP = label(1, "bug"), label(2, "chore"), label(3, "keep")

    def test_prints_the_receipt(self):
        gql = LabelGql((self.BUG, self.CHORE, self.KEEP), [self.BUG["id"], self.KEEP["id"]])
        code, out, err = self.run_main(
            ["label", "swap", "TASK-2", "--from", "bug", "--to", "chore"], gql
        )
        self.assertEqual((code, err, len(gql.writes)), (0, "", 1))
        self.assertEqual(json.loads(out), receipt_of("label swap"))
        self.assertLessEqual(len(out), 1024)

    def test_failures_print_no_receipt(self):
        catalog = (self.BUG, self.CHORE, self.KEEP)
        cases = (
            (LabelGql(catalog, [self.KEEP["id"]]), "chore", 1, "refused"),
            (LabelGql(catalog, [self.BUG["id"]]), "nope", 2, "--to 'nope'"),
            (
                LabelGql(
                    catalog,
                    [self.BUG["id"]],
                    result=Unavailable("issueUpdate", reason="timed out"),
                ),
                "chore",
                1,
                "result unknown",
            ),
        )
        for gql, new, want, phrase in cases:
            with self.subTest(phrase=phrase):
                code, out, err = self.run_main(
                    ["label", "swap", "TASK-2", "--from", "bug", "--to", new], gql
                )
                self.assertEqual((code, out), (want, b""))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: "), err)
                self.assertIn(phrase, err)
        self.assertEqual([len(gql.writes) for gql, *_ in cases], [0, 0, 1])


class LabelAddRemove(IssueCase):
    BUG, KEEP = label(1, "bug"), label(3, "keep")

    def test_prints_the_receipt(self):
        for op, carried, want in (
            ("add", [self.BUG["id"]], [self.BUG["id"], self.KEEP["id"]]),
            ("remove", [self.KEEP["id"], self.BUG["id"]], [self.BUG["id"]]),
        ):
            gql = LabelGql((self.BUG, self.KEEP), carried)
            with self.subTest(op=op):
                code, out, err = self.run_main(["label", op, "TASK-2", "keep"], gql)
                self.assertEqual((code, err, len(gql.writes), gql.carried), (0, "", 1, want))
                self.assertEqual(json.loads(out), receipt_of(f"label {op}"))
                self.assertLessEqual(len(out), 1024)

    def test_an_add_already_there_or_a_remove_already_gone_is_unchanged(self):
        for op, carried in (("add", [self.KEEP["id"]]), ("remove", [self.BUG["id"]])):
            gql = LabelGql((self.BUG, self.KEEP), carried)
            with self.subTest(op=op):
                code, out, err = self.run_main(["label", op, "TASK-2", "keep"], gql)
                self.assertEqual((code, err, gql.writes), (0, "", []))
                self.assertEqual(json.loads(out), receipt_of(f"label {op}", result="unchanged"))

    def test_failures_print_no_receipt(self):
        group = uid(10)
        todo, doing = label(4, "todo", group=group), label(5, "doing", group=group)
        catalog = (self.BUG, self.KEEP, todo, doing)
        cases = (
            ("add", LabelGql(catalog, [todo["id"]]), "doing", 1, "refused"),
            ("add", LabelGql(catalog, []), "nope", 2, "LABEL 'nope'"),
            ("remove", LabelGql(catalog, []), "nope", 2, "LABEL 'nope'"),
            (
                "remove",
                LabelGql(catalog, [self.KEEP["id"]], result=GraphQLErrors([{"message": "boom"}])),
                "keep",
                1,
                "boom",
            ),
        )
        for op, gql, value, want, phrase in cases:
            with self.subTest(op=op, phrase=phrase):
                code, out, err = self.run_main(["label", op, "TASK-2", value], gql)
                self.assertEqual((code, out), (want, b""))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: "), err)
                self.assertIn(phrase, err)
        self.assertEqual([len(gql.writes) for _, gql, *_ in cases], [0, 0, 0, 1])


class Failures(IssueCase):
    def test_exit_codes_and_stderr(self):
        cases = (
            (["read", "task-2"], Gql(None), 2, "task-2"),
            (["read", "TASK-2"], Gql(None), 2, "no such issue"),
            (["read", "TASK-2"], Gql(None, fail=LinearError("linear api error: boom")), 1, "boom"),
            (
                ["read", "TASK-2"],
                Gql(None, fail=Unavailable("issue", reason="timed out")),
                1,
                "timed out",
            ),
            (
                ["read", "TASK-2"],
                Gql(None, fail=CalledProcessError(44, ["security"])),
                2,
                "Linear manager app: Keychain (CalledProcessError)",
            ),
            (["read", "TASK-2"], Gql(None, fail=FileNotFoundError("security")), 2, "Keychain"),
            (["read", "TASK-2"], Gql(None, fail=TimeoutExpired("security", 30)), 2, "Keychain"),
        )
        for argv, gql, want, phrase in cases:
            with self.subTest(phrase=phrase):
                code, out, err = self.run_main(argv, gql)
                self.assertEqual((code, out), (want, b""))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: "), err)
                self.assertIn(phrase, err)

    def test_configuration_errors_exit_two(self):
        with open(self.config, "w") as f:
            f.write(CONFIG.replace(HARNESS_LINE, ""))
        code, out, err = self.run_main(["read", "TASK-2"], Gql(issue_node()))
        self.assertEqual((code, out), (2, b""))
        self.assertIn(
            "required, unset: linear.harness.client_id, linear.harness.secret_service", err
        )

    def test_an_incomplete_manager_app_exits_two_before_any_call(self):
        for drop in ([], ["client_id"], ["secret_service", "token_service"], list(MANAGER_APP)):
            with self.subTest(drop=drop):
                kept = {k: v for k, v in MANAGER_APP.items() if k not in drop}
                line = "linear.manager = {{ {} }}\n".format(
                    ", ".join(f'{k} = "{v}"' for k, v in kept.items())
                )
                with open(self.config, "w") as f:
                    f.write(CONFIG.replace(MANAGER_LINE, line))
                gql = Gql(issue_node())
                code, out, err = self.run_main(["read", "TASK-2"], gql)
                if not drop:
                    self.assertEqual(code, 0, err)
                    continue
                self.assertEqual((code, out, gql.calls), (2, b"", []))
                self.assertIn(
                    "ensemble-orchestrator issue needs the manager's Linear OAuth app: required, "
                    "unset: " + ", ".join(f"linear.manager.{k}" for k in drop),
                    err,
                )

    def test_an_unknown_linear_key_exits_two(self):
        with open(self.config, "w") as f:
            f.write('linear.typo = "x"\n' + CONFIG)
        gql = Gql(issue_node())
        code, out, err = self.run_main(["read", "TASK-2"], gql)
        self.assertEqual((code, out, gql.calls), (2, b"", []))
        self.assertIn("unknown keys in [linear]: typo", err)

    def test_diagnostics_stay_within_eight_kib(self):
        gql = Gql(None, fail=LinearError("linear api error: " + "错" * 9000))
        code, _, err = self.run_main(["read", "TASK-2"], gql)
        self.assertEqual(code, 1)
        self.assertLessEqual(len(err.encode("utf-8")), 8192)
        self.assertIn("bytes omitted", err)

    def test_graphql_diagnostics_print_only_messages_and_codes(self):
        raw = [
            {
                "message": "boom",
                "extensions": {"code": "INPUT_ERROR", "userPresentableMessage": "hidden"},
                "path": ["issue"],
                "locations": [{"line": 1, "column": 2}],
            },
            {"message": "two"},
        ]
        code, out, err = self.run_main(["read", "TASK-2"], Gql(None, fail=GraphQLErrors(raw)))
        self.assertEqual((code, out), (1, b""))
        self.assertEqual(
            err, "ensemble-orchestrator issue: linear api error: boom (INPUT_ERROR); two\n"
        )

    def test_many_graphql_messages_stay_capped(self):
        raw = [{"message": "错" * 1000, "extensions": {"code": "E"}} for _ in range(50)]
        code, _, err = self.run_main(["read", "TASK-2"], Gql(None, fail=GraphQLErrors(raw)))
        self.assertEqual(code, 1)
        self.assertLessEqual(len(err.encode("utf-8")), 8192)
        self.assertNotIn("[{", err)

    def test_an_unusable_token_without_a_secret_is_configuration(self):
        for value in ("", "lin api"):
            keychain = Keychain({MANAGER_IDENTITY.token_service: value})
            with self.subTest(value=value), mock.patch("subprocess.run", keychain):
                code, out, err = self.run_main(["read", "TASK-2"], client.linear_gql)
            self.assertEqual((code, out), (2, b""))
            self.assertIn("Linear manager app has no token and renewal failed", err)
            if value:
                self.assertNotIn(value, err)


class ManagerToken(IssueCase):
    """The issue commands call as the manager app over the real transport, renewing its token."""

    def run_issue(self, days, argv, answer=None, data=None):
        self.keychain = manager_tokens(days)
        routes = data or Routes(issues=page_edges([issue_row("TASK-1")]))
        self.endpoint = Endpoint(answer, data=lambda q, v: routes(q, **v))
        with (
            mock.patch("subprocess.run", self.keychain),
            mock.patch.object(urllib.request, "urlopen", side_effect=self.endpoint),
        ):
            return self.run_main(argv, client.linear_gql)

    def stored(self):
        return json.loads(self.keychain.value(MANAGER_IDENTITY.token_service))

    def test_4_days_left_renews_first(self):
        since = datetime.now(UTC)
        code, out, err = self.run_issue(4, ["list"])
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(json.loads(out)["items"][0]["identifier"], "TASK-1")
        [token] = self.endpoint.tokens
        self.assertEqual(
            (token["grant_type"], token["client_id"], token["scope"]),
            ("client_credentials", "client-manager", "read,write,app:assignable"),
        )
        renewed(self, self.keychain.value(MANAGER_IDENTITY.token_service), since)
        self.assertEqual(
            {r.get_header("Authorization") for r in self.endpoint.graphql}, {f"Bearer {NEW}"}
        )

    def test_a_failed_renewal_of_a_usable_token_reports_on_stderr(self):
        two_pages = Routes(
            issues=[
                page_edges([issue_row("TASK-1")], more=True, cursor="c1"),
                page_edges([issue_row("TASK-2")]),
            ]
        )
        path = os.path.join(self.root, "list.json")
        for failure in (token_http_error(401), urllib.error.URLError("down")):
            with self.subTest(failure=type(failure).__name__):
                code, out, err = self.run_issue(
                    3, ["list", "--full", "--output", path], failure, two_pages
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(len(self.endpoint.graphql), 2)
                self.assertEqual(len(self.endpoint.tokens), 1)
                self.assertEqual(self.stored()["access_token"], OLD)
                self.assertEqual(err.count("\n"), 1)
                self.assertTrue(
                    err.startswith("ensemble-orchestrator issue: token-error: manager: "), err
                )
                self.assertEqual(leaks([out.decode(), err]), [])
                two_pages.routes["issues"] = [
                    page_edges([issue_row("TASK-1")], more=True, cursor="c1"),
                    page_edges([issue_row("TASK-2")]),
                ]

    def test_an_expired_token_whose_renewal_fails_exits_2(self):
        for days, state in ((-1, "token expired"), (None, "has no token")):
            with self.subTest(state=state):
                if days is None:
                    self.keychain = Keychain({MANAGER_IDENTITY.secret_service: SECRET})
                    self.endpoint = Endpoint(token_http_error(401))
                    with (
                        mock.patch("subprocess.run", self.keychain),
                        mock.patch.object(urllib.request, "urlopen", side_effect=self.endpoint),
                    ):
                        code, out, err = self.run_main(["list"], client.linear_gql)
                else:
                    code, out, err = self.run_issue(days, ["list"], token_http_error(401))
                self.assertEqual((code, out), (2, b""))
                self.assertEqual(self.endpoint.graphql, [])
                self.assertEqual(
                    err,
                    f"ensemble-orchestrator issue: Linear manager app {state} and renewal "
                    "failed: HTTP 401; fix: check linear.manager.client_id and the client secret "
                    "in Keychain service linear-app-manager-secret (rotated: store the new "
                    "secret), then rerun\n",
                )


LOCAL = CONFIG.replace('tracker = "linear"\n', 'tracker = "local"\n')
LINEAR_ONLY = 'Linear only, not on a local board ([orchestrator] tracker = "local")'


class LocalCase(IssueCase):
    """IssueCase on a local board in a temp $ENSEMBLE_BOARD_DIR; its issue files by put."""

    def setUp(self):
        super().setUp()
        self.board = os.path.join(os.path.realpath(self.home), "board")
        os.environ[config.BOARD_ENV] = self.board
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(LOCAL)

    def put(self, rel_dir, ident, *, mtime=1_790_000_000, description="", comments="", parent=None):
        path = os.path.join(self.board, rel_dir, f"{ident} x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = f"### Description\n{description}\n" if description else ""
        body += f"### Comments\n{comments}" if comments else ""
        parent_line = f"parent: {parent}\n" if parent else ""
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                f"---\nid: {ident}\ntitle: {ident} title\n{parent_line}"
                f"created: 2026-10-19T14:02:00-04:00\n---\n{body}"
            )
        os.utime(path, (mtime, mtime))
        return path

    def snapshot(self):
        found = {}
        for top, _, names in os.walk(self.board):
            for name in names:
                if name == ".lock":
                    continue
                with open(os.path.join(top, name), "rb") as f:
                    found[os.path.join(top, name)] = f.read()
        return found

    def run_local(self, argv, stdin=b""):
        return self.run_main(argv, Gql(None), stdin)


class LinearOnlyOnLocal(LocalCase):
    FORMS = (
        (["lookup", "teams"], "lookup"),
        (["lookup", "milestones", "--project", "P"], "lookup"),
        (["label", "swap", "LOC-1", "--from", "a", "--to", "b"], "label swap"),
        (["label", "add", "LOC-1", "a"], "label add"),
        (["label", "remove", "LOC-1", "a"], "label remove"),
        (["list", "--label", "bug"], "--label"),
        (["list", "--project", "P"], "--project"),
        (["list", "--project", "P", "--label", "bug"], "--label"),
        (["list", "--label", "", "--full", "--output", "out.json"], "--label"),
        (["create", "--title", "t", "--body-file", "-", "--label", "bug"], "--label"),
        (["create", "--title", "t", "--body-file", "-", "--project", "P"], "--project"),
        (["edit", "LOC-1", "--project", "P"], "--project"),
        (["edit", "LOC-1", "--title", "t", "--project", "P"], "--project"),
        (["create", "--title", "t", "--body-file", "-", "--milestone", "M"], "--milestone"),
        (
            ["create", "--title", "t", "--body-file", "-", "--milestone", "M", "--label", "a"],
            "--label",
        ),
    )

    def test_each_linear_only_form_exits_two_before_reading_the_board(self):
        for argv, what in self.FORMS:
            with self.subTest(argv=argv):
                gql = Gql(None)
                code, out, err = self.run_main(argv, gql, b"body")
                self.assertEqual(
                    (code, out, err),
                    (2, b"", f"ensemble-orchestrator issue: {what}: {LINEAR_ONLY}\n"),
                )
                self.assertEqual(gql.calls, [])
        self.assertFalse(os.path.exists(self.board))

    def test_the_local_options_still_run(self):
        self.put("pm/ready", "LOC-1")
        code, out, err = self.run_local(["list", "--state", "ready", "--role", "pm"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([item["id"] for item in json.loads(out)["items"]], ["LOC-1"])


class LocalRead(LocalCase):
    def test_a_read_is_cut_like_linears(self):
        notes = "".join(f"#### other · 2026-10-19 14:0{n}\nnote {n}\n\n" for n in range(6))
        self.put("pm/ready", "LOC-1", description="d" * 700, comments=notes)
        code, out, err = self.run_local(["read", "LOC-1"])
        self.assertEqual((code, err), (0, ""))
        view = json.loads(out)
        self.assertEqual(view["identifier"], "LOC-1")
        self.assertEqual(view["state"], {"id": "ready", "name": "ready", "type": "unstarted"})
        self.assertEqual(sorted(view["truncated"]), ["comments", "description"])
        self.assertEqual(len(view["comments"]), 5)
        self.assertEqual(view["comments"][0]["body"], "note 5")
        self.assertEqual(
            view["continue"], "ensemble-orchestrator issue read LOC-1 --full --output FILE"
        )

    def test_full_writes_the_uncut_view(self):
        notes = "".join(f"#### other · 2026-10-19 14:0{n}\nnote {n}\n\n" for n in range(6))
        self.put("pm/ready", "LOC-1", description="d" * 700, comments=notes)
        path = os.path.join(self.root, "LOC-1.json")
        code, out, err = self.run_local(["read", "LOC-1", "--full", "--output", path])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"identifier": "LOC-1", "output": path})
        with open(path, encoding="utf-8") as f:
            view = json.load(f)
        self.assertEqual((view["description"], len(view["comments"])), ("d" * 700, 6))


class LocalList(LocalCase):
    def test_pages_most_recently_modified_first(self):
        for n in range(1, 4):
            self.put("pm/ready", f"LOC-{n}", mtime=1_790_000_000 + n)
        code, out, err = self.run_local(["list", "--limit", "2"])
        self.assertEqual((code, err), (0, ""))
        page = json.loads(out)
        self.assertEqual([item["id"] for item in page["items"]], ["LOC-3", "LOC-2"])
        self.assertEqual((page["has_next_page"], page["next_cursor"]), (True, "LOC-2"))
        code, out, err = self.run_local(["list", "--limit", "2", "--cursor", "LOC-2"])
        self.assertEqual((code, err), (0, ""))
        page = json.loads(out)
        self.assertEqual([item["id"] for item in page["items"]], ["LOC-1"])
        self.assertEqual((page["has_next_page"], page["next_cursor"]), (False, None))


class LocalErrors(LocalCase):
    def test_a_board_that_cannot_be_scanned_exits_two(self):
        code, out, err = self.run_local(["read", "LOC-1"])
        self.assertEqual(
            (code, out, err),
            (2, b"", f"ensemble-orchestrator issue: {self.board}: no such board directory\n"),
        )
        path = self.put("pm/ready", "LOC-1")
        with open(path, "w", encoding="utf-8") as f:
            f.write("no front matter\n")
        code, out, err = self.run_local(["read", "LOC-1"])
        self.assertEqual(
            (code, out, err), (2, b"", f"ensemble-orchestrator issue: {path}: no front matter\n")
        )

    def test_an_unresolved_value_exits_two(self):
        self.put("pm/ready", "LOC-1")
        before = self.snapshot()
        for argv, message in (
            (["read", "LOC-9"], "LOC-9: no such issue"),
            (["move", "LOC-9", "done"], "LOC-9: no such issue"),
            (["list", "--role", "ghost"], "--role ghost: not a role"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.run_local(argv)
                self.assertEqual((code, out), (2, b""))
                self.assertTrue(err.startswith(f"ensemble-orchestrator issue: {message}"), err)
        self.assertEqual(self.snapshot(), before)

    def test_a_failed_write_exits_one_with_its_own_message(self):
        self.put("pm/ready", "LOC-1")
        before = self.snapshot()
        with mock.patch("os.replace", side_effect=PermissionError(13, "Permission denied")):
            code, out, err = self.run_local(["comment", "LOC-1", "--body-file", "-"], b"hi")
        self.assertEqual(
            (code, out, err),
            (1, b"", "ensemble-orchestrator issue: [Errno 13] Permission denied\n"),
        )
        self.assertNotIn("Keychain", err)
        self.assertEqual(self.snapshot(), before)

    def test_a_file_changed_while_writing_exits_one(self):
        path = self.put("pm/ready", "LOC-1", description="Do it.")
        real = os.fstat

        def edited(fd):
            """os.fstat, which reading an issue file calls once it has its text, then a person's
            edit, every time."""
            found = real(fd)
            with open(path, "a", encoding="utf-8") as f:
                f.write("More.\n")
            return found

        with mock.patch("os.fstat", side_effect=edited):
            code, out, err = self.run_local(["move", "LOC-1", "done"])
        self.assertEqual(
            (code, out, err),
            (1, b"", f"ensemble-orchestrator issue: {path}: changed while writing\n"),
        )

    def test_a_state_outside_the_seven_is_a_usage_error(self):
        self.put("pm/ready", "LOC-1")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            self.run_local(["move", "LOC-1", "Ready"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("invalid choice: 'Ready'", err.getvalue())

    def test_a_bad_cursor_task_or_client_exits_two_writing_nothing(self):
        self.put("pm/ready", "LOC-1")
        before = self.snapshot()
        create = ["create", "--title", "T", "--body-file", "-"]
        client = (
            "--client 'a b': want a client name: no whitespace, control character or leading quote"
        )
        for argv, message in (
            (["list", "--cursor", "LOC-999"], "--cursor LOC-999: not a cursor of this list\n"),
            (["list", "--task", "nope"], "--task nope: not a configured task: "),
            ([*create, "--task", "nope"], "--task nope: not a configured task: "),
            (["list", "--client", "a b"], client + "\n"),
            ([*create, "--client", "a b"], client + "\n"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.run_local(argv, b"body")
                self.assertEqual((code, out), (2, b""))
                self.assertTrue(err.startswith(f"ensemble-orchestrator issue: {message}"), err)
        self.assertEqual(self.snapshot(), before)


class LocalFirstIssue(LocalCase):
    def test_create_on_an_empty_board(self):
        os.makedirs(os.path.join(self.board, "unassigned"))
        code, out, err = self.run_local(["create", "--title", "First", "--body-file", "-"], b"body")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["issue"]["identifier"], "LOC-1")

    def test_create_uses_the_local_configuration(self):
        os.makedirs(os.path.join(self.board, "unassigned"))
        path = Path(self.config)
        path.write_text(path.read_text().replace('issue_prefix = "LOC"', 'issue_prefix = "AGE"'))
        code, out, err = self.run_local(["create", "--title", "First", "--body-file", "-"], b"body")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["issue"]["identifier"], "AGE-1")


class HelpOnBothTrackers(LocalCase):
    def test_help_is_the_same_text_on_a_local_and_a_linear_board(self):
        for command in (
            "",
            "read",
            "list",
            "lookup",
            "create",
            "edit",
            "move",
            "comment",
            "archive",
            "relation add",
            "relation remove",
            "label swap",
            "label add",
            "label remove",
        ):
            texts = []
            for text in (LOCAL, CONFIG):
                with open(self.config, "w", encoding="utf-8") as f:
                    f.write(text)
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as cm:
                    issue.main([*command.split(), "--help"], config_path=self.config)
                self.assertEqual(cm.exception.code, 0)
                texts.append(output.getvalue())
            with self.subTest(command=command):
                self.assertEqual(texts[0], texts[1])
                self.assertIn("--help", texts[0])


class Full(IssueCase):
    def gql(self):
        node = issue_node(
            "TASK-2",
            comments=conn([comment(9, body="评" * 400)], more=True, cursor="c-1"),
            children=conn([rel("TASK-10")], more=True, cursor="k-1"),
        )
        return Gql(node, {"comments": [conn([comment(1)])], "children": [conn([rel("TASK-11")])]})

    def test_writes_every_page_to_the_file_and_prints_its_path(self):
        path = os.path.join(self.root, "TASK-2.json")
        code, out, err = self.run_main(["read", "TASK-2", "--full", "--output", path], self.gql())
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"identifier": "TASK-2", "output": path})
        with open(path, encoding="utf-8") as f:
            text = f.read()
        view = json.loads(text)
        self.assertEqual(
            text, json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        self.assertEqual([c["id"] for c in view["comments"]], ["c9", "c1"])
        self.assertEqual(len(view["comments"][0]["body"]), 400)
        self.assertEqual([c["identifier"] for c in view["children"]], ["TASK-10", "TASK-11"])

    def test_a_relative_output_prints_its_absolute_path(self):
        with mock.patch("os.getcwd", return_value=self.root):
            code, out, _ = self.run_main(
                ["read", "TASK-2", "--output", "out.json", "--full"], self.gql()
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["output"], os.path.join(self.root, "out.json"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out.json")))

    def test_full_and_output_go_together(self):
        for argv in (["read", "TASK-2", "--full"], ["read", "TASK-2", "--output", "x.json"]):
            gql = self.gql()
            with self.subTest(argv=argv):
                code, out, err = self.run_main(argv, gql)
                self.assertEqual((code, out, gql.calls), (2, b"", []))
                self.assertIn("--full --output FILE", err)

    def test_an_unwritable_output_exits_two(self):
        path = os.path.join(self.root, "missing", "TASK-2.json")
        code, out, err = self.run_main(["read", "TASK-2", "--full", "--output", path], self.gql())
        self.assertEqual((code, out), (2, b""))
        self.assertIn("--output", err)

    def test_a_failed_page_writes_no_file(self):
        gql = self.gql()
        gql.pages["children"] = [Unavailable("issue", reason="timed out")]
        path = os.path.join(self.root, "TASK-2.json")
        code, out, err = self.run_main(["read", "TASK-2", "--full", "--output", path], gql)
        self.assertEqual((code, out), (1, b""))
        self.assertIn("timed out", err)
        self.assertFalse(os.path.exists(path))

    def test_a_failed_write_keeps_the_earlier_copy_and_leaves_no_temp_file(self):
        path = os.path.join(self.root, "TASK-2.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"earlier": true}\n')
        argv = ["read", "TASK-2", "--full", "--output", path]
        for target in ("os.fsync", "os.replace"):
            with (
                self.subTest(target=target),
                mock.patch(target, side_effect=OSError(28, "No space")),
            ):
                code, out, err = self.run_main(argv, self.gql())
            self.assertEqual((code, out), (2, b""))
            self.assertIn("No space", err)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"earlier": true}\n')
            self.assertEqual(sorted(os.listdir(self.root)), ["TASK-2.json", "config.toml"])

    def test_a_write_replaces_the_earlier_copy_whole(self):
        path = os.path.join(self.root, "TASK-2.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * 100000)
        code, _, _ = self.run_main(["read", "TASK-2", "--full", "--output", path], self.gql())
        self.assertEqual(code, 0)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["identifier"], "TASK-2")
        self.assertEqual(sorted(os.listdir(self.root)), ["TASK-2.json", "config.toml"])

    def test_a_vanished_page_fails_with_a_capped_diagnostic_and_no_file(self):
        gql = self.gql()
        gql.pages["children"] = [None]
        path = os.path.join(self.root, "TASK-2.json")
        code, out, err = self.run_main(["read", "TASK-2", "--full", "--output", path], gql)
        self.assertEqual((code, out), (1, b""))
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(
            err.startswith("ensemble-orchestrator issue: TASK-2: children page missing"), err
        )
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(path))

    def test_an_auth_failure_writes_no_file_and_shows_no_credential(self):
        refused = urllib.error.HTTPError(client.URL, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        path = os.path.join(self.root, "TASK-2.json")
        with (
            mock.patch("subprocess.run", manager_tokens()),
            mock.patch("urllib.request.urlopen", side_effect=refused),
        ):
            code, out, err = self.run_main(
                ["read", "TASK-2", "--full", "--output", path], client.linear_gql
            )
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", err)
        self.assertEqual(leaks([out.decode(), err]), [])
        self.assertFalse(os.path.exists(path))


class ReadBody(Case):
    def test_a_file_or_stdin_reads_verbatim(self):
        path = os.path.join(self.home, "body.md")
        with open(path, "wb") as f:
            f.write(RAW_BODY.encode("utf-8"))
        self.assertEqual(issue.read_body(path, io.BytesIO()), RAW_BODY)
        self.assertEqual(issue.read_body("-", io.BytesIO(RAW_BODY.encode("utf-8"))), RAW_BODY)

    def test_only_a_leading_byte_order_mark_is_dropped(self):
        path = os.path.join(self.home, "bom-body.md")
        data = b"\xef\xbb\xbf" + (RAW_BODY + "﻿").encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)
        self.assertEqual(issue.read_body(path, io.BytesIO()), RAW_BODY + "﻿")
        self.assertEqual(issue.read_body("-", io.BytesIO(data)), RAW_BODY + "﻿")

    def test_an_unusable_body_is_refused(self):
        blank, latin = os.path.join(self.home, "blank.md"), os.path.join(self.home, "latin.md")
        with open(blank, "w", encoding="utf-8") as f:
            f.write(" \n\t\n")
        bom, bom_blank = os.path.join(self.home, "bom.md"), os.path.join(self.home, "bom-blank.md")
        with open(bom, "wb") as f:
            f.write(b"\xef\xbb\xbf")
        with open(bom_blank, "wb") as f:
            f.write(b"\xef\xbb\xbf \n")
        with open(latin, "wb") as f:
            f.write("café".encode("latin-1"))
        cases = (
            (os.path.join(self.home, "missing.md"), b"", "No such file"),
            (self.home, b"", "directory"),
            (latin, b"", "not UTF-8"),
            (blank, b"", "empty"),
            (bom, b"", "empty"),
            (bom_blank, b"", "empty"),
            ("-", b"\xef\xbb\xbf", "empty"),
            ("-", b"", "empty"),
            ("-", b"\xff", "not UTF-8"),
        )
        for path, stdin, phrase in cases:
            with self.subTest(phrase=phrase, path=path), self.assertRaises(IssueError) as cm:
                issue.read_body(path, io.BytesIO(stdin))
            self.assertIn(f"--body-file {path}", str(cm.exception))
            self.assertIn(phrase, str(cm.exception))

    def test_stdin_is_read_only_for_dash(self):
        path = os.path.join(self.home, "body.md")
        with open(path, "wb") as f:
            f.write(RAW_BODY.encode("utf-8"))
        # no stdin at all: fd 0 closed (None) or a text layer without a byte stream
        for stdin in (None, io.StringIO(RAW_BODY)):
            with self.subTest(stdin=stdin), mock.patch("sys.stdin", stdin):
                self.assertEqual(issue.read_body(path), RAW_BODY)
                with self.assertRaisesRegex(IssueError, "--body-file -: no stdin"):
                    issue.read_body("-")
        with mock.patch("sys.stdin", mock.Mock(buffer=io.BytesIO(RAW_BODY.encode("utf-8")))):
            self.assertEqual(issue.read_body("-"), RAW_BODY)


class Requests(Case):
    """The frame's typed requests: one parse of list, lookup, create or edit, every option in place."""

    def request(self, *argv, body=None):
        return issue._request(issue._parser().parse_args(argv), body)

    def test_list(self):
        argv = ("list", "--query", "q", "--state", "ready", "--role", "me", "--task", "build")
        argv += ("--client", "c", "--priority", "high", "--label", "bug", "--project", "P")
        got = self.request(*argv, "--parent", "TASK-1", "--include-archived")
        want = ListQuery(
            query="q",
            state=IssueState.READY,
            role="me",
            task="build",
            client="c",
            priority=Priority.HIGH,
            parent="TASK-1",
            include_archived=True,
            label="bug",
            project="P",
        )
        self.assertEqual(got, want)
        self.assertEqual((type(got.state), type(got.priority)), (IssueState, Priority))
        self.assertEqual(self.request("list"), list_query())

    def test_lookup(self):
        argv = ("lookup", "milestones", "--query", "m", "--team", "TASK", "--project", "P")
        self.assertEqual(
            self.request(*argv, "--limit", "5"),
            LookupQuery(resource=Resource.MILESTONES, query="m", team="TASK", project="P"),
        )
        self.assertEqual(
            self.request("lookup", "teams"),
            LookupQuery(resource=Resource.TEAMS, query=None, team=None, project=None),
        )

    def test_create(self):
        argv = ("create", "--title", "T", "--body-file", "f", "--state", "running")
        argv += ("--role", "engineer", "--task", "build", "--client", "c", "--priority", "urgent")
        argv += ("--parent", "TASK-1", "--label", "a", "--label", "b", "--project", "P")
        argv += ("--milestone", "M", "--blocks", "TASK-2", "TASK-3", "--blocked-by", "TASK-4")
        got = self.request(*argv, body="B")
        want = NewIssue(
            title="T",
            body="B",
            state=IssueState.RUNNING,
            role="engineer",
            task="build",
            client="c",
            priority=Priority.URGENT,
            parent="TASK-1",
            labels=("a", "b"),
            project="P",
            milestone="M",
            blocks=("TASK-2", "TASK-3"),
            blocked_by=("TASK-4",),
        )
        self.assertEqual(got, want)
        self.assertEqual((type(got.state), type(got.priority)), (IssueState, Priority))
        self.assertEqual(
            self.request("create", "--title", "t", "--body-file", "f", body="b"), new_issue()
        )

    def test_edit(self):
        argv = ("edit", "TASK-1", "--title", "T", "--body-file", "f", "--task", "build")
        argv += ("--role", "pm", "--project", "P", "--client", "c", "--priority", "low")
        got = self.request(*argv, body="B")
        want = IssueEdit(
            id="TASK-1",
            title="T",
            body="B",
            role="pm",
            project="P",
            task="build",
            client="c",
            priority=Priority.LOW,
        )
        self.assertEqual(got, want)
        self.assertIs(type(got.priority), Priority)
        self.assertEqual(
            self.request("edit", "TASK-1", "--task", "build"), issue_change("TASK-1", task="build")
        )

    def test_the_other_commands_carry_none(self):
        for argv in (("read", "TASK-1"), ("move", "TASK-1", "done"), ("archive", "TASK-1")):
            with self.subTest(argv=argv):
                self.assertIsNone(self.request(*argv))
