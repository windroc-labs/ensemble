"""Orchestrator prune behavior at its dependency boundaries."""

import io
import json
import os
import shutil
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from ensemble.delegate import ConfigError, SessionError
from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import config, log, prune, prune_state
from ensemble.orchestrator.engine.flow.types import IssueState, Snapshot
from ensemble.orchestrator.engine.types import Entry, LinearError, PruneState, Unavailable
from ensemble.orchestrator.trackers.linear import linear_tracker, ops
from ensemble.utils import temp_dirs
from tests.delegate.workdir import checkout_path
from tests.orchestrator.process_fixtures import CompletedProcess
from tests.orchestrator.prune_fixtures import (
    NOW,
    PRUNE_QUERIES,
    STATE_IDS,
    PruneDelegate,
    PruneScenario,
    gql_for,
    tree,
    write,
)
from tests.orchestrator.unit import promote_fixtures as tp
from tests.orchestrator.unit.board_ids import FINISHED
from tests.orchestrator.unit.run_fixtures import CORE_LOCAL, HARNESS, logged, show

A, B = "engineer-TASK-7-0b6f2c1e", "dummy-tester-TASK-7-7c1d9e2f"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
OTHER_SID = "7c1d9e2f-0a3b-4c5d-8e6f-1a2b3c4d5e6f"


def gone_on_reread(gql, key):
    """gql, but key's archive read finds no issue."""

    def call(query, **v):
        if query == linear_tracker.Q_ARCHIVE and v["i"] == key:
            return {"issue": None}
        return gql(query, **v)

    return call


class RepositoryCleanup(PruneScenario):
    def test_prune_cleans_through_the_delegate(self):
        self.mkc("TASK-49")
        self.mkg("TASK-49", "o", "wt")
        beside = os.path.join(self.work, "TASK-49", "notes.md")
        write(beside, "x")
        busy = self.mkc("TASK-50")
        gql = gql_for(
            {
                "TASK-49": ("Done", [(30, "In Progress"), (2, "Done")]),
                "TASK-50": ("In Progress", [(30, "In Progress")]),
            }
        )
        code, out = self.prune(gql)
        self.assertEqual((code, out.splitlines()), (0, ["prune-removed TASK-49 what=agent dir"]))
        self.assertIsNone(self.checkouts("TASK-49"))
        self.assertTrue(os.path.isfile(beside))
        self.assertTrue(os.path.isdir(busy))
        self.assertOutsideKept()
        self.assertNotIn(ops.Q_TEAM, gql.calls)
        self.assertIn("state { id type }", linear_tracker.Q_ISSUE)

    def test_a_tick_reads_the_runs_log_once(self):
        idents = [f"TASK-{n}" for n in range(40, 46)]
        for ident in idents:
            self.mkc(ident)
        real, reads = open, []

        def counted(path, *args, **kwargs):
            reads.append(path)
            return real(path, *args, **kwargs)

        gql = gql_for(dict.fromkeys(idents, ("Done", [(30, "Done")])))
        with mock.patch("builtins.open", side_effect=counted):
            code, out = self.prune(gql)
        self.assertEqual(len(out.splitlines()), len(idents), out)
        self.assertEqual((code, reads.count(self.cfg.runs_log)), (0, 1))

    def test_clean_refusal_is_a_skip_once_and_cleans_no_clone(self):
        self.fake_clone()
        wt = self.mkg("TASK-49")
        refusal = ConfigError("not a real directory: x, refusing to touch")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        with mock.patch.object(PruneDelegate, "clean", side_effect=refusal) as clean:
            for out in (
                "prune-skip TASK-49 reason=ConfigError: not a real directory: x, refusing to touch",
                "",
            ):
                self.assertEqual(self.prune(gql, writable=(self.work,)), (0, out))
        self.assertEqual(clean.call_args.args, (self.cfg.issue_workdir("TASK-49"),))
        self.assertEqual(clean.call_args.kwargs, {"dry": False})
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))

    def test_a_session_error_in_clean_is_an_error(self):
        self.fake_clone()
        wt = self.mkg("TASK-49")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        with mock.patch.object(PruneDelegate, "clean", side_effect=SessionError("host gone")):
            self.assertEqual(
                self.prune(gql, writable=(self.work,)),
                (3, "prune-error TASK-49 error=clean: SessionError: host gone"),
            )
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))

    def test_prune_uses_the_recorded_delegate_kind(self):
        class Recorded(PruneDelegate):
            cleaned = []

            @classmethod
            def clean(cls, workdir, *, dry=False, scratch=False):
                cls.cleaned.append(workdir)
                return super().clean(workdir, dry=dry, scratch=scratch)

        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, [B], agent=False)
        with open(self.cfg.runs_log) as f:
            rows = f.read().replace('"delegate_kind": "harness"', '"delegate_kind": "recorded"')
        write(self.cfg.runs_log, rows)
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        self.cfg = replace(self.cfg, delegates={"harness": PruneDelegate})
        unknown = (
            "prune-skip TASK-7 reason=ConfigError: delegate kind 'recorded' is no longer configured"
        )
        self.assertEqual(self.prune(gql), (0, unknown))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(self.checkouts("TASK-7"), ())
        self.cfg = replace(self.cfg, delegates={"harness": PruneDelegate, "recorded": Recorded})
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir"),
        )
        self.assertEqual(Recorded.cleaned, [self.cfg.issue_workdir("TASK-7")])

    def test_pruned_issue_is_left_alone(self):
        self.record("TASK-7", SID, [A])
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])})
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir"),
        )
        PruneDelegate.attach(self.cfg.issue_workdir("TASK-7"))
        self.record("TASK-8", OTHER_SID, [B], agent=False)
        again = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        with mock.patch.object(PruneDelegate, "attach", side_effect=AssertionError("attached")):
            self.assertEqual(self.prune(again), (0, ""))
            self.assertEqual(self.prune(again), (0, ""))
        self.assertEqual(len(again.read), 4)
        self.assertEqual(self.closed(), [A])

    def test_young_workdirs_without_agent_files_are_queried_and_kept(self):
        old = os.path.join(self.work, "TASK-49", "src", "o", "repo")
        os.makedirs(os.path.join(old, ".git"))
        self.record("TASK-48", OTHER_SID, [B], agent=False)
        os.makedirs(checkout_path(os.path.join(self.work, "notes"), "o", "repo", "notes"))
        gql = gql_for(dict.fromkeys(("TASK-48", "TASK-49"), ("Done", [(30, "Done")])))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(len(gql.read), 2)
        self.assertTrue(os.path.isdir(os.path.join(old, ".git")))

    def test_young_and_in_progress_untouched(self):
        path = self.mkc("TASK-48")
        busy = self.mkc("TASK-50")
        self.api.live = ["engineer-TASK-48-aaaaaaaa", "engineer-TASK-50-bbbbbbbb"]
        gql = gql_for(
            {
                "TASK-48": ("Done", [(50, "Done"), (30, "In Progress"), (2 - 1 / 3600, "Done")]),
                "TASK-50": ("In Progress", [(50, "Done"), (30, "In Progress")]),
            }
        )
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertTrue(os.path.isdir(path) and os.path.isdir(busy))
        self.assertEqual(self.api.order, [])

    def test_unknown_finish_time_skipped(self):
        path = self.mkc("TASK-49")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [])}))
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49 reason=finish time unknown", out)
        self.assertTrue(os.path.isdir(path))

    def test_worktree_clones_are_cleaned_once_after_the_agent_files_go(self):
        clone = self.fake_clone()
        for owner, name in (("o", "a"), ("p", "b")):
            self.mkg("TASK-49", owner, name)
        write(os.path.join(self.mkg("TASK-49", "o", "c"), "notes.md"), "uncommitted")
        calls = []

        def git(argv, _timeout):
            calls.append((self.checkouts("TASK-49"), argv))
            return CompletedProcess(argv, 0, "", "")

        code, out = self.prune(
            gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=git, writable=(self.work,)
        )
        self.assertEqual((code, out.splitlines()), (0, ["prune-removed TASK-49 what=agent dir"]))
        self.assertEqual(
            [argv[-2:] for _, argv in calls if "worktree" in argv], [["worktree", "prune"]]
        )
        self.assertEqual(
            {(there, tuple(argv[5:8])) for there, argv in calls},
            {(None, ("-C", clone, f"--git-dir={clone}/.git"))},
        )

    def test_a_failed_clean_logs_the_error_and_cleans_no_clone(self):
        self.fake_clone()
        kept = self.mkg("TASK-48")
        self.mkc("TASK-49")
        with mock.patch.object(
            shutil, "rmtree", side_effect=[PermissionError("denied\nby os"), None]
        ) as rmtree:
            code, out = self.prune(
                gql_for(dict.fromkeys(("TASK-48", "TASK-49"), ("Done", [(30, "Done")]))),
                writable=(self.work,),
            )
        self.assertEqual((code, rmtree.call_count), (3, 2))
        self.assertEqual(
            out.splitlines(),
            [
                "prune-error TASK-48 error=clean: PermissionError: denied by os",
                "prune-removed TASK-49 what=agent dir",
            ],
        )
        self.assertTrue(os.path.isfile(os.path.join(kept, ".git")))

    def test_dry_run_plans_and_touches_nothing(self):
        clone = self.fake_clone()
        self.record("TASK-7", SID, [A])
        wt = self.mkg("TASK-7")
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])}, owners={"TASK-7": "u-pm"})
        with mock.patch.object(PruneDelegate, "clean", side_effect=AssertionError("cleaned")):
            code, out = self.prune(gql, dry=True, writable=(self.work,))
        self.assertEqual(
            (code, out.splitlines()),
            (
                0,
                [
                    f"prune-plan TASK-7 session={SID} what=close the run",
                    "prune-plan TASK-7 what=delete the agent dir",
                    f"prune-plan TASK-7 what=prune {clone}'s worktree records and pushed TASK-7-* branches",
                    "prune-plan TASK-7 what=archive",
                ],
            ),
        )
        self.assertNotIn(linear_tracker.M_ARCHIVE, gql.calls)
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
        self.assertEqual(self.api.order, [])
        PruneDelegate.attach(self.cfg.issue_workdir("TASK-7"))
        PruneDelegate.end(self.cfg.issue_workdir("TASK-7"))
        self.assertEqual(self.prune(gql, dry=True, writable=(self.work,))[0], 0)
        self.assertEqual(self.checkouts("TASK-7"), (wt,))


class RetentionCleanup(PruneScenario):
    def test_writable_dirs_default_to_work_and_the_temp_dirs(self):
        pruner = prune.Pruner(self.board, None, NOW, dry=False, work=self.work, cfg=self.cfg)
        self.assertEqual(pruner.writable, (self.work, *temp_dirs()))

    def test_archives_pm_and_engineer_issues_finished_2h(self):
        gql = gql_for(
            {
                "TASK-1": ("Done", [(30, "In Progress"), (2, "Done")]),
                "TASK-2": ("Canceled", [(50, "Canceled")]),
                "TASK-3": ("Done", [(30, "Done")]),
                "TASK-4": ("Canceled", [(2, "Canceled")]),
            },
            owners={
                "TASK-1": "u-pm",
                "TASK-2": "u-pm",
                "TASK-3": "u-engineer",
                "TASK-4": "u-engineer",
            },
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3", "id-TASK-4"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-archived TASK-1",
                "prune-archived TASK-2",
                "prune-archived TASK-3",
                "prune-archived TASK-4",
            ],
        )

    def test_archives_canceled_and_duplicate_issues_of_any_assignee(self):
        gql = gql_for(
            {
                "TASK-1": ("Canceled", [(30, "Canceled")]),
                "TASK-2": ("Duplicate", [(30, "Duplicate")]),
                "TASK-3": ("Canceled", [(30, "Canceled")]),
                "TASK-4": ("Duplicate", [(2, "Duplicate")]),
                "TASK-5": ("Duplicate", [(2 - 1 / 3600, "Duplicate")]),
                "TASK-6": ("Canceled", [(30, "Canceled")]),
                "TASK-8": ("Done", [(30, "Done")]),
                "TASK-9": ("Done", [(30, "Done")]),
            },
            owners={
                "TASK-1": None,
                "TASK-2": "u-researcher",
                "TASK-3": "u-other",
                "TASK-4": None,
                "TASK-5": None,
                "TASK-6": "u-researcher",
                "TASK-8": "u-researcher",
                "TASK-9": None,
            },
            children={"TASK-6": {"TASK-7": "started"}},
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3", "id-TASK-4"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-archived TASK-1",
                "prune-archived TASK-2",
                "prune-archived TASK-3",
                "prune-archived TASK-4",
                "prune-skip TASK-6 reason=archive: sub-issue TASK-7 not finished",
            ],
        )

    def test_linear_finish_stamps_stand_in_for_a_missing_history_move(self):
        for ident in ("TASK-1", "TASK-2", "TASK-3"):
            self.mkc(ident)
        gql = gql_for(
            {
                "TASK-1": ("Duplicate", [(40, "In Progress")]),
                "TASK-2": ("Done", []),
                "TASK-3": ("Duplicate", []),
                "TASK-4": ("Canceled", [(30, "Canceled")]),
            },
            owners={"TASK-2": "u-pm"},
            stamps={
                "TASK-1": {"canceledAt": 30},
                "TASK-2": {"completedAt": 30},
                "TASK-3": {"canceledAt": 1},
                "TASK-4": {"canceledAt": 1},
            },
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-removed TASK-1 what=agent dir",
                "prune-removed TASK-2 what=agent dir",
                "prune-archived TASK-1",
                "prune-archived TASK-2",
            ],
        )
        self.assertTrue(os.path.isdir(self.cfg.issue_workdir("TASK-3")))
        self.assertIsNotNone(self.checkouts("TASK-3"))

    def test_not_archived(self):
        done = ("Done", [(50, "Done")])
        gql = gql_for(
            {
                "TASK-1": ("Done", [(2 - 1 / 3600, "Done")]),
                "TASK-2": ("Done", [(50, "Done"), (30, "In Progress"), (2 - 1 / 3600, "Done")]),
                "TASK-3": ("Done", []),
                "TASK-4": ("In Progress", [(50, "Done"), (30, "In Progress")]),
                "TASK-5": done,
                "TASK-6": done,
                "TASK-7": done,
            },
            owners={
                "TASK-1": "u-pm",
                "TASK-2": "u-engineer",
                "TASK-3": "u-pm",
                "TASK-4": "u-pm",
                "TASK-5": "u-researcher",
                "TASK-6": "u-other",
                "TASK-7": None,
            },
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, [])
        self.assertEqual(
            out.splitlines(), ["prune-skip TASK-3 reason=archive: finish time unknown"]
        )
        (v,) = gql.finished
        self.assertEqual(sorted(v["a"]), ["u-engineer", "u-pm"])
        self.assertEqual(v["d"], [STATE_IDS["Done"]])
        self.assertEqual(sorted(v["s"]), sorted(FINISHED - {STATE_IDS["Done"]}))

    def test_archive_failure_others_archived(self):
        done = ("Done", [(30, "Done")])
        gql = gql_for(
            {"TASK-1": done, "TASK-2": done, "TASK-3": done},
            owners=dict.fromkeys(("TASK-1", "TASK-2", "TASK-3"), "u-pm"),
            fail={"TASK-1"},
            refuse={"TASK-2"},
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-error TASK-1 error=archive: LinearError: linear api error: boom",
                "prune-error TASK-2 error=archive: LinearError: issueArchive: success: false",
                "prune-archived TASK-3",
            ],
        )

    def test_archive_reads_every_page_first(self):
        done = ("Done", [(30, "Done")])
        idents = ("TASK-1", "TASK-2", "TASK-3")
        gql = gql_for(
            dict.fromkeys(idents, done), owners=dict.fromkeys(idents, "u-engineer"), page=2
        )
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual([v["c"] for v in gql.finished], [None, "2"])

    def test_an_unfinished_sub_issue_keeps_its_parent(self):
        done = ("Done", [(30, "Done")])
        for dry, archived in (
            (False, "prune-archived TASK-4"),
            (True, "prune-plan TASK-4 what=archive"),
        ):
            with self.subTest(dry=dry):
                gql = gql_for(
                    {"TASK-1": done, "TASK-4": done},
                    owners={"TASK-1": "u-pm", "TASK-4": "u-pm"},
                    children={"TASK-1": {"TASK-2": "completed", "TASK-3": "started"}},
                )
                code, out = self.prune(gql, dry=dry)
                self.assertEqual(code, 0)
                self.assertEqual(gql.archived, [] if dry else ["id-TASK-4"])
                self.assertEqual(
                    out.splitlines(),
                    ["prune-skip TASK-1 reason=archive: sub-issue TASK-3 not finished", archived],
                )

    def test_finished_or_archived_sub_issues_let_the_parent_go(self):
        gql = gql_for(
            {
                "TASK-1": ("Done", [(30, "Done")]),
                "TASK-2": ("Done", [(30, "Done")]),
                "TASK-3": ("Canceled", [(30, "Canceled")]),
                "TASK-5": ("Duplicate", [(30, "Duplicate")]),
            },
            owners={"TASK-1": "u-engineer", "TASK-2": "u-pm"},
            children={
                "TASK-1": {"TASK-2": "completed", "TASK-3": "canceled", "TASK-5": "duplicate"}
            },
        )
        self.assertEqual(self.prune(gql), (0, "prune-archived TASK-1"))
        self.assertEqual(gql.archived, ["id-TASK-1"])

    def test_an_open_grandchild_keeps_its_done_grandparent(self):
        gql = gql_for(
            {"TASK-1": ("Done", [(30, "Done")])},
            owners={"TASK-1": "u-pm"},
            children={"TASK-1": {"TASK-2": "completed"}, "TASK-2": {"TASK-3": "started"}},
        )
        self.assertEqual(
            self.prune(gql), (0, "prune-skip TASK-1 reason=archive: sub-issue TASK-3 not finished")
        )
        self.assertEqual((gql.archived, gql.subs), ([], ["id-TASK-2"]))

    def test_a_sub_issue_prune_would_not_archive_keeps_its_parent(self):
        for top, sub, state, owner, why in (
            ("TASK-1", "TASK-2", ("Done", [(1, "Done")]), "u-pm", "finished under 2 hours ago"),
            ("TASK-3", "TASK-4", ("Done", []), "u-engineer", "finish time unknown"),
            (
                "TASK-5",
                "TASK-6",
                ("Done", [(30, "Done")]),
                "u-researcher",
                "not an archive candidate",
            ),
            ("TASK-7", "TASK-8", None, None, "not an archive candidate"),  # another team's
        ):
            unknown = [f"prune-skip {sub} reason=archive: {why}"] if state == ("Done", []) else []
            for dry in (False, True):
                with self.subTest(why, sub=sub, dry=dry):
                    gql = gql_for(
                        {top: ("Done", [(30, "Done")])} | ({sub: state} if state else {}),
                        owners={top: "u-pm", sub: owner},
                        children={top: {sub: "completed"}},
                    )
                    code, out = self.prune(gql, dry=dry)
                    self.assertEqual(
                        (code, out.splitlines()),
                        (0, [*unknown, f"prune-skip {top} reason=archive: sub-issue {sub} {why}"]),
                    )
                    self.assertEqual(gql.archived, [])

    def test_a_parent_goes_with_sub_issues_prune_would_archive_too(self):
        gql = gql_for(
            {
                "TASK-1": ("Done", [(30, "Done")]),
                "TASK-2": ("Done", [(30, "Done")]),
                "TASK-3": ("Canceled", [(3, "Canceled")]),
            },
            owners={"TASK-1": "u-pm", "TASK-2": "u-engineer", "TASK-3": "u-researcher"},
            children={"TASK-1": {"TASK-2": "completed"}, "TASK-2": {"TASK-3": "canceled"}},
        )
        self.assertEqual(self.prune(gql), (0, "prune-archived TASK-1"))
        self.assertEqual(gql.archived, ["id-TASK-1"])

    def test_every_page_at_every_depth_is_read(self):
        many = {f"SUB-{n}": "completed" for n in range(1, 261)}
        for name, children, last in (
            ("261st child", {"TASK-1": {**many, "TASK-9": "started"}}, "TASK-9"),
            (
                "fifth level",
                {f"TASK-{n}": {f"TASK-{n + 1}": "completed"} for n in range(1, 5)}
                | {"TASK-5": {"TASK-6": "started"}},
                "TASK-6",
            ),
        ):
            for dry in (False, True):
                with self.subTest(name, dry=dry):
                    gql = gql_for(
                        {"TASK-1": ("Done", [(30, "Done")])},
                        owners={"TASK-1": "u-pm"},
                        children=children,
                    )
                    self.assertEqual(
                        self.prune(gql, dry=dry),
                        (0, f"prune-skip TASK-1 reason=archive: sub-issue {last} not finished"),
                    )
                    self.assertEqual(gql.archived, [])
        gql = gql_for(
            {"TASK-1": ("Done", [(30, "Done")])}
            | dict.fromkeys(many, ("Canceled", [(30, "Canceled")])),
            owners={"TASK-1": "u-pm"},
            children={"TASK-1": many},
        )
        code, out = self.prune(gql)
        self.assertEqual((code, out.splitlines()[0]), (0, "prune-archived TASK-1"))
        self.assertEqual(gql.archived, ["id-TASK-1"])
        self.assertEqual(gql.subs.count("id-TASK-1"), 5)

    def test_an_issue_reopened_since_the_list_is_kept(self):
        for dry in (False, True):
            with self.subTest(dry=dry):
                gql = gql_for(
                    {"TASK-1": ("Done", [(30, "Done")])},
                    owners={"TASK-1": "u-pm"},
                    children={"TASK-1": {"TASK-2": "completed"}},
                    reread={"TASK-1": "In Progress"},
                )
                self.assertEqual(
                    self.prune(gql, dry=dry),
                    (0, "prune-skip TASK-1 reason=archive: TASK-1 not finished"),
                )
                self.assertEqual(gql.archived, [])

    def test_an_issue_gone_before_its_archive_read_is_an_error(self):
        done = ("Done", [(30, "Done")])
        for dry in (False, True):
            with self.subTest(dry=dry):
                gql = gql_for(
                    {"TASK-1": done, "TASK-2": done},
                    owners=dict.fromkeys(("TASK-1", "TASK-2"), "u-pm"),
                )
                code, out = self.prune(gone_on_reread(gql, "id-TASK-1"), dry=dry)
                self.assertEqual(
                    (code, out.splitlines()),
                    (
                        3,
                        [
                            "prune-error TASK-1 error=archive: LinearError: id-TASK-1: issue not found",
                            "prune-plan TASK-2 what=archive" if dry else "prune-archived TASK-2",
                        ],
                    ),
                )
                self.assertEqual(gql.archived, [] if dry else ["id-TASK-2"])

    def test_candidate_query_failure_keeps_clone_step(self):
        clone = self.mkc("TASK-49")
        issues = gql_for({"TASK-49": ("Done", [(30, "Done")])})

        def gql(query, **v):
            if query == linear_tracker.Q_FINISHED:
                raise LinearError("linear api error: down")
            return issues(query, **v)

        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertIn("prune-error error=archive: LinearError: linear api error: down", out)
        self.assertFalse(os.path.lexists(clone))

    def test_recorded_run_alone_closes_all_owned_sessions_when_finished(self):
        self.record("TASK-7", SID, [B, A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        gql = gql_for(
            {"TASK-7": ("Done", [(24, "Done")]), "TASK-8": ("In Progress", [(30, "In Progress")])}
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [f"prune-closed TASK-7 session={SID}", "prune-removed TASK-7 what=agent dir"],
        )
        self.assertEqual(len(gql.read), 2)
        self.assertEqual(self.closed(), [B, A])
        again = gql_for({"TASK-7": ("Done", [(24, "Done")]), "TASK-8": ("In Progress", [])})
        self.prune(again)
        self.assertEqual(self.closed(), [B, A])

    def test_unrecorded_matching_names_never_make_candidates(self):
        self.api.live = [A, "ensemble-engineer-TASK-7", "notes"]
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(self.closed(), [])
        self.assertEqual(gql.read, [])

    def test_only_recorded_configured_roles_make_candidates(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-9", OTHER_SID, ["designer-agent"], role="designer")
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-9"), ("Done", [(30, "Done")])))
        self.assertEqual(
            self.prune(gql)[1].splitlines(),
            [
                f"prune-closed TASK-7 session={SID}",
                "prune-removed TASK-7 what=agent dir",
                f"prune-skip TASK-9 reason=ConfigError: {self.cfg.issue_workdir('TASK-9')}: "
                "delegate is still alive",
            ],
        )
        self.assertEqual(self.closed(), [A])

    def test_clone_and_session_issue_queried_once_sessions_first(self):
        clone = self.mkc("TASK-7")
        self.record("TASK-7", SID, [A])
        seen, close = [], self.api.close
        gql = gql_for({"TASK-7": ("Canceled", [(30, "Canceled")])})
        with mock.patch.object(
            self.api, "close", lambda name: (seen.append(os.path.isdir(clone)), close(name))
        ):
            code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-closed TASK-7 session={SID}",
                "prune-removed TASK-7 what=agent dir",
                "prune-archived TASK-7",
            ],
        )
        self.assertEqual((seen, self.closed()), ([True], [A]))
        self.assertEqual(gql.read, ["TASK-7"])
        self.assertFalse(os.path.lexists(clone))

    def test_a_failed_close_or_an_unfinished_issue_keeps_the_runs_state(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, [B])
        self.api.errors = {A: SessionError("boom")}
        gql = gql_for(
            {"TASK-7": ("Done", [(30, "Done")]), "TASK-8": ("Done", [(2 - 1 / 3600, "Done")])}
        )
        self.assertEqual(self.prune(gql)[0], 3)
        for ident in ("TASK-7", "TASK-8"):
            with self.subTest(ident=ident):
                PruneDelegate.attach(self.cfg.issue_workdir(ident))

    def test_sessions_unknown_prune_no_issue_but_archive(self):
        for dry, archived in (
            (False, "prune-archived TASK-49"),
            (True, "prune-plan TASK-49 what=archive"),
        ):
            with self.subTest(dry=dry):
                self.fresh()
                self.api.order.clear()
                kept = [self.mkc("TASK-49"), self.mkc("TASK-49", "p", "q")]
                self.record("TASK-49", SID, [A])
                self.unknown = True
                gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
                code, out = self.prune(gql, dry=dry)
                self.assertEqual(
                    (code, out.splitlines()),
                    (3, ["prune-error error=run inventory unknown", archived]),
                )
                self.assertTrue(all(os.path.isdir(os.path.join(p, ".git")) for p in kept))
                self.assertEqual(gql.read, [])
                self.assertEqual(gql.archived, [] if dry else ["id-TASK-49"])
                self.assertEqual(self.api.order, [])

    def test_close_failure_counted(self):
        for error in (SessionError("boom"), ConfigError("bad name")):
            with self.subTest(error=type(error).__name__):
                self.api.order.clear()
                self.record("TASK-7", SID, [B, A])
                self.api.errors = {B: error}
                code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 3)
                self.assertEqual(
                    out.splitlines(),
                    [f"prune-error TASK-7 session={SID} error={type(error).__name__}: {error}"],
                )
                self.assertEqual(self.closed(), [B, A])
                self.assertEqual(self.checkouts("TASK-7"), ())

    def test_a_failed_close_keeps_the_agent_dir_until_a_later_tick_closes_the_run(self):
        clone = self.mkc("TASK-7")
        self.record("TASK-7", SID, [B, A])
        self.api.errors = {B: SessionError("boom")}
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])})
        code, out = self.prune(gql)
        self.assertEqual(
            (code, out.splitlines(), self.closed()),
            (3, [f"prune-error TASK-7 session={SID} error=SessionError: boom"], [B, A]),
        )
        self.assertTrue(os.path.isdir(clone))
        self.api.errors = {}
        self.api.order.clear()
        code, out = self.prune(gql)
        self.assertEqual(
            (code, out.splitlines(), self.closed()),
            (
                0,
                [f"prune-closed TASK-7 session={SID}", "prune-removed TASK-7 what=agent dir"],
                [B, A],
            ),
        )
        self.assertIsNone(self.checkouts("TASK-7"))

    def test_failed_run_close_does_not_stop_another_finished_issue(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        self.api.errors = {A: SessionError("boom")}
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertEqual(self.closed(), [A, "other-host-agent"])
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-error TASK-7 session={SID} error=SessionError: boom",
                f"prune-closed TASK-8 session={OTHER_SID}",
                "prune-removed TASK-8 what=agent dir",
            ],
        )

    def test_a_run_without_delegate_state_skips_once_and_other_issues_still_prune(self):
        clone = self.mkc("TASK-7")
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        PruneDelegate.lose(self.cfg.issue_workdir("TASK-7"))
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        skip = (
            f"prune-skip TASK-7 sid={SID} reason=ConfigError: "
            f"{self.cfg.issue_workdir('TASK-7')!r}: no delegate.json"
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(self.closed(), ["other-host-agent"])
        self.assertFalse(os.path.exists(clone))
        self.assertIn(skip, out.splitlines())
        self.assertIn(f"prune-closed TASK-8 session={OTHER_SID}", out.splitlines())
        self.assertNotIn(skip, self.prune(gql)[1].splitlines())

    def test_promote_tick_archives_with_its_roles(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        code, _, out = self.tick(gql)
        self.assertEqual((code, gql.archived), (0, ["id-TASK-1"]))
        self.assertIn("prune-archived TASK-1", out)
        self.assertEqual(self.setup_queries, [ops.Q_SETUP])

    def test_promote_tick_closes_recorded_runs_through_the_delegate(self):
        self.record("TASK-7", SID, [A])
        code, _, out = self.tick(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 0)
        self.assertIn(f"prune-closed TASK-7 session={SID}", out)
        self.assertEqual(self.api.order, [("close", A)])

    def test_prune_failure_leaves_handoff_alone(self):
        clone = self.mkc("TASK-49")

        def down(_query, **_v):
            raise LinearError("linear api error: down")

        code, state, out = self.tick(down)
        self.assertEqual((code, state), (0, "Done"))
        self.assertIn(
            "prune-error TASK-49 error=issue read: LinearError: linear api error: down", out
        )
        self.assertTrue(os.path.isdir(clone))

    def test_an_outage_reading_an_issue_stops_prune(self):
        clones = [self.mkc(ident) for ident in ("TASK-48", "TASK-49")]
        issues = gql_for(dict.fromkeys(("TASK-48", "TASK-49"), ("Done", [(30, "Done")])))
        self.outage(
            "issues", issues, [(linear_tracker.Q_SNAPSHOTS, {"service": HARNESS, "f": mock.ANY})]
        )
        self.assertEqual(issues.calls, [])
        self.assertTrue(all(os.path.isdir(c) for c in clones))

    def test_an_outage_reading_the_finished_issues_stops_prune(self):
        issues = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        self.outage("issues", issues, [(linear_tracker.Q_FINISHED, mock.ANY)])

    def test_an_outage_archiving_stops_prune(self):
        done = ("Done", [(30, "Done")])
        issues = gql_for(
            {"TASK-1": done, "TASK-2": done}, owners={"TASK-1": "u-pm", "TASK-2": "u-pm"}
        )
        self.outage(
            "issueArchive",
            issues,
            [(linear_tracker.M_ARCHIVE, {"service": HARNESS, "i": "id-TASK-1"})],
        )


class WorkdirRetention(PruneScenario):
    """FR-5: a workdir without agent files goes whole 7 days after its issue finished."""

    def bare(self, ident, sid=SID, *, live=False):
        workdir = self.cfg.issue_workdir(ident)
        self.record(ident, sid, [A], agent=False)
        if not live:
            PruneDelegate.end(workdir)
        write(os.path.join(workdir, "writeback.json"), "{}")
        write(os.path.join(workdir, "writeback.lock"), "")
        self.assertEqual(sorted(os.listdir(workdir)), ["run", "writeback.json", "writeback.lock"])
        return workdir

    def test_a_bare_workdir_finished_7_days_is_removed(self):
        self.bare("TASK-7")
        self.bare("TASK-8", OTHER_SID)
        gql = gql_for(
            {
                "TASK-7": ("Done", [(8 * 24, "Done")]),
                "TASK-8": ("Canceled", [(7 * 24, "Canceled")]),
            }
        )
        self.assertEqual(
            self.prune(gql),
            (
                0,
                "prune-removed TASK-7 what=workdir\nprune-removed TASK-8 what=workdir\n"
                "prune-archived TASK-8",
            ),
        )
        self.assertEqual(os.listdir(self.work), [])

    def test_a_bare_workdir_with_a_read_only_go_module_cache_is_removed(self):
        module = os.path.join(self.bare("TASK-7"), "gopath", "mod", "x@v1.0.12")
        os.makedirs(module)
        write(os.path.join(module, "go.mod"), "module x\n")
        os.chmod(os.path.join(module, "go.mod"), 0o444)
        for path in (module, os.path.dirname(module)):
            os.chmod(path, 0o555)
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(self.prune(gql), (0, "prune-removed TASK-7 what=workdir"))
        self.assertEqual(os.listdir(self.work), [])

    def test_a_bare_workdir_finished_3_days_is_kept(self):
        workdir = self.bare("TASK-7")
        files = tree(workdir)
        for hours in (3 * 24, 7 * 24 - 1 / 3600):
            with self.subTest(hours=hours):
                self.assertEqual(
                    self.prune(gql_for({"TASK-7": ("Done", [(hours, "Done")])})), (0, "")
                )
                self.assertEqual(tree(workdir), files)

    def test_agent_files_take_the_2_hour_step_first(self):
        self.record("TASK-7", SID, [A])
        workdir = self.cfg.issue_workdir("TASK-7")
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir"),
        )
        self.assertEqual(os.listdir(workdir), ["run"])
        self.assertEqual(self.prune(gql), (0, "prune-removed TASK-7 what=workdir"))
        self.assertFalse(os.path.lexists(workdir))

    def test_a_live_run_skips_once_and_keeps_the_workdir(self):
        workdir = self.bare("TASK-7", live=True)
        files = tree(workdir)
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-skip TASK-7 reason=ConfigError: {workdir}: delegate is still alive"),
        )
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(tree(workdir), files)

    def test_a_failed_removal_is_an_error_and_others_go_on(self):
        for ident, sid in (("TASK-7", SID), ("TASK-8", OTHER_SID), ("TASK-9", SID)):
            self.bare(ident, sid)
        real = shutil.rmtree

        def clean(workdir, **_kwargs):
            if workdir.endswith("TASK-7"):
                raise SessionError("host gone")

        def rmtree(path, **kwargs):
            if path.endswith("TASK-8"):
                raise PermissionError("denied")
            real(path, **kwargs)

        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8", "TASK-9"), ("Done", [(8 * 24, "Done")])))
        with (
            mock.patch.object(PruneDelegate, "clean", side_effect=clean),
            mock.patch.object(shutil, "rmtree", side_effect=rmtree),
        ):
            code, out = self.prune(gql)
        self.assertEqual(
            (code, out.splitlines()),
            (
                3,
                [
                    "prune-error TASK-7 error=remove: SessionError: host gone",
                    "prune-error TASK-8 error=remove: PermissionError: denied",
                    "prune-removed TASK-9 what=workdir",
                ],
            ),
        )
        self.assertEqual(sorted(os.listdir(self.work)), ["TASK-7", "TASK-8"])

    def test_dry_run_plans_the_removal(self):
        workdir = self.bare("TASK-7")
        files = tree(self.work)
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(
            self.prune(gql, dry=True), (0, "prune-plan TASK-7 what=delete the workdir")
        )
        self.assertEqual(tree(self.work), files)
        with mock.patch.object(PruneDelegate, "clean") as clean:
            self.prune(gql, dry=True)
        self.assertEqual(clean.call_args, mock.call(workdir, dry=True))

    def test_an_unconfigured_kind_is_not_queried(self):
        workdir = self.bare("TASK-7")
        with open(self.cfg.runs_log) as f:
            rows = f.read().replace('"delegate_kind": "harness"', '"delegate_kind": "recorded"')
        write(self.cfg.runs_log, rows)
        files = tree(workdir)
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(gql.read, [])
        self.assertEqual(tree(workdir), files)

    def test_unfinished_or_unknown_finish_keeps_the_workdir(self):
        self.bare("TASK-7")
        self.bare("TASK-8", OTHER_SID)
        files = tree(self.work)
        gql = gql_for(
            {
                "TASK-7": ("In Progress", [(9 * 24, "Done"), (8 * 24, "In Progress")]),
                "TASK-8": ("Done", []),
            }
        )
        self.assertEqual(self.prune(gql), (0, "prune-skip TASK-8 reason=finish time unknown"))
        self.assertEqual(tree(self.work), files)

    def test_only_real_directories_are_visited(self):
        os.symlink(self.outside, os.path.join(self.work, "TASK-7"))
        write(os.path.join(self.work, "TASK-8"), "x")
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(8 * 24, "Done")])))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(gql.read, [])
        self.assertOutsideKept()
        self.assertTrue(os.path.isfile(os.path.join(self.work, "TASK-8")))


class Stalled(PruneScenario):
    """A stalled issue's checkouts and temp files go STALL after its latest move or run."""

    WEEK = 7 * 24

    def stalled(self, ident="TASK-7", *, hours=8 * 24, run=None, state="In Review"):
        """A W with agent files of an ended run, its issue moved to `state` `hours` ago."""
        workdir = self.cfg.issue_workdir(ident)
        self.record(ident, SID, [A], hours=hours if run is None else run)
        PruneDelegate.end(workdir)
        agent = os.path.join(workdir, "agent")
        for path in ("tmp/msg", "publish/Research/r.md", "prd.md"):
            os.makedirs(os.path.dirname(os.path.join(agent, path)), exist_ok=True)
            write(os.path.join(agent, path), "x")
        write(os.path.join(workdir, "writeback.json"), "{}")
        return workdir, gql_for({ident: (state, [(hours + 1, "In Progress"), (hours, state)])})

    def test_stalled_week_drops_scratch_keeps_logs(self):
        clone = self.fake_clone()
        workdir, gql = self.stalled(state="Handoff")
        wt = self.mkg("TASK-7")
        calls = []

        def git(argv, _timeout):
            calls.append(argv)
            return CompletedProcess(argv, 0, "", "")

        run = tree(os.path.join(workdir, "run"))
        self.assertEqual(
            self.prune(gql, git=git, writable=(self.work,)),
            (0, "prune-removed TASK-7 what=scratch"),
        )
        self.assertFalse(os.path.lexists(wt))
        self.assertEqual(sorted(os.listdir(workdir)), ["agent", "run", "writeback.json"])
        self.assertEqual(os.listdir(os.path.join(workdir, "agent")), ["prd.md"])
        self.assertEqual(tree(os.path.join(workdir, "run")), run)
        self.assertEqual(
            [argv[5:] for argv in calls],
            [["-C", clone, f"--git-dir={clone}/.git", "worktree", "prune"]],
        )
        self.assertEqual(self.api.order, [])

    def test_a_backlog_or_unconfigured_state_issue_stalls_too(self):
        for state in ("Backlog", "Triage"):
            with self.subTest(state=state):
                workdir, gql = self.stalled(state=state)
                self.assertEqual(self.prune(gql), (0, "prune-removed TASK-7 what=scratch"))
                self.assertEqual(os.listdir(os.path.join(workdir, "agent")), ["prd.md"])

    def test_recent_or_active_or_unknown_kept(self):
        issues = {}
        for n, (hours, run, state) in enumerate(
            (
                (self.WEEK - 1 / 3600, None, "In Review"),
                (9 * 24, self.WEEK - 1, "In Review"),
                (9 * 24, None, "Todo"),
                (9 * 24, None, "In Progress"),
            )
        ):
            issues.update(
                self.stalled(f"TASK-{n + 10}", hours=hours, run=run, state=state)[1].issues
            )
        self.stalled("TASK-20")
        issues["TASK-20"] = ("In Review", [(9 * 24, "In Progress")])
        files = tree(self.work)
        gql = gql_for(issues)
        self.assertEqual(self.prune(gql), (0, "prune-skip TASK-20 reason=stall time unknown"))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(tree(self.work), files)

    def test_live_refused_and_dry_run_plans(self):
        workdir, gql = self.stalled()
        files = tree(workdir)
        self.assertEqual(
            self.prune(gql, dry=True),
            (0, "prune-plan TASK-7 what=delete the checkouts and temp files"),
        )
        self.assertEqual(tree(workdir), files)
        PruneDelegate.attach(workdir).detach()
        files = tree(workdir)
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-skip TASK-7 reason=ConfigError: {workdir}: delegate is still alive"),
        )
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(tree(workdir), files)
        self.assertEqual(self.api.order, [])

    def test_no_event_once_scratch_gone(self):
        workdir, gql = self.stalled()
        self.assertEqual(self.prune(gql), (0, "prune-removed TASK-7 what=scratch"))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(os.listdir(os.path.join(workdir, "agent")), ["prd.md"])

    def test_a_duplicate_issue_is_finished_not_stalled(self):
        _, gql = self.stalled(state="Duplicate")
        self.assertEqual(
            self.prune(gql),
            (
                0,
                f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir\n"
                "prune-archived TASK-7",
            ),
        )
        self.assertIsNone(self.checkouts("TASK-7"))


class PruneCommand(PruneScenario):
    """ensemble-orchestrator prune: one prune tick alone, as promote's tick runs it."""

    def command(self, prune_gql, *argv):
        write(
            os.path.join(self.ensemble, "config.local.toml"),
            f'[orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL,
        )
        path = os.path.join(self.root, "config.toml")
        write(path, tp.CONFIG)
        fake = tp.FakeLinear()

        def gql(query, **v):
            return (prune_gql if query in PRUNE_QUERIES else fake)(query, **v)

        before, out, err = len(logged(self.logs)), io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prune.main(
                list(argv),
                trackers=cli.TRACKERS,
                gql=gql,
                now=NOW,
                config_path=path,
                pruner=self.pruner,
            )
        lines = err.getvalue().splitlines()
        copies = [json.loads(line) for line in lines if line.startswith("{")]
        said = "\n".join(show(e) for e in logged(self.logs)[before:] + copies)
        rest = "".join(f"{line}\n" for line in lines if not line.startswith("{"))
        return code, out.getvalue(), rest, said

    def test_config_error_exits_2(self):
        # a config.toml or Linear setup error, each logged as config-error
        def rejected(*_, **__):
            raise LinearError("linear api error: teams: HTTP 401")

        write(
            os.path.join(self.ensemble, "config.local.toml"),
            f'[orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL,
        )
        path = os.path.join(self.root, "config.toml")
        for text, gql, reason in (
            ("bogus = 1\n" + tp.CONFIG, None, "orchestrator/config.toml: unknown keys: bogus"),
            (tp.CONFIG, rejected, "linear api error: teams: HTTP 401"),
        ):
            with self.subTest(reason):
                write(path, text)
                with redirect_stderr(io.StringIO()):
                    code = prune.main(
                        [],
                        trackers=cli.TRACKERS,
                        gql=gql,
                        now=NOW,
                        config_path=path,
                        pruner=self.pruner,
                    )
                self.assertEqual(code, 2)
                self.assertIn(f"config-error reason={reason}", [show(e) for e in logged(self.logs)])

    def test_a_tick_deletes_finished_checkouts_and_archives(self):
        clone = self.mkc("TASK-49")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        self.assertEqual(self.command(gql)[:3], (0, "", ""))
        self.assertFalse(os.path.exists(clone))
        self.assertEqual(gql.archived, ["id-TASK-49"])
        self.assertOutsideKept()

    def test_a_duplicate_issue_is_finished_by_its_state_type(self):
        clone = self.mkc("TASK-49")
        gql = gql_for(
            {"TASK-49": ("Duplicate", [(30, "In Progress"), (2, "Duplicate")])},
            owners={"TASK-49": "u-pm"},
        )
        self.assertEqual(self.command(gql)[:3], (0, "", ""))
        self.assertFalse(os.path.exists(clone))
        self.assertEqual(gql.archived, ["id-TASK-49"])

    def test_a_dry_run_plans_and_changes_nothing(self):
        clone = self.mkc("TASK-49")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        code, out, err, said = self.command(gql, "--dry-run")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue(os.path.isdir(os.path.join(clone, ".git")))
        self.assertEqual(gql.archived, [])
        self.assertEqual(
            said.splitlines(),
            [
                "prune-plan TASK-49 what=delete the agent dir",
                "prune-plan TASK-49 what=archive",
            ],
        )

    def test_a_failed_step_exits_1_after_the_rest(self):
        def down(query, **v):
            if query == linear_tracker.Q_ISSUE:
                raise LinearError("linear api error: down")
            return gql_for({})(query, **v)

        self.mkc("TASK-49")
        code, _, _, said = self.command(down)
        self.assertEqual(code, 1)
        self.assertIn(
            "prune-error TASK-49 error=issue read: LinearError: linear api error: down", said
        )

    def test_other_arguments_are_a_usage_error(self):
        for argv in (["--now"], ["TASK-1"], ["--dry-run", "x"]):
            with self.subTest(argv=argv):
                code, out, err, said = self.command(gql_for({}), *argv)
                self.assertEqual((code, out, said), (2, "", ""))
                self.assertIn("ensemble-orchestrator prune", err)


class Bound(PruneScenario):
    """A tick reads Linear through its cache, change feed and caps, whatever the dirs."""

    quiet = ("In Review", [(30, "In Review")])

    def dirs(self, n, start=100, state=quiet):
        idents = [f"TASK-{start + k}" for k in range(n)]
        for ident in idents:
            os.makedirs(self.cfg.issue_workdir(ident))
        return dict.fromkeys(idents, state)

    def cache(self):
        return os.path.join(self.logs, "prune.json")

    def counts(self, gql):
        return [gql.calls.count(q) for q in (linear_tracker.Q_SNAPSHOTS, linear_tracker.Q_ISSUE)]

    def tick(self, gql, **kw):
        gql.calls.clear()
        gql.read.clear()
        return self.prune(gql, **kw)


class RequestBound(Bound):
    def test_a_cold_tick_reads_up_to_its_cap(self):
        issues = self.dirs(300)
        elsewhere = list(issues)[:20]
        gql = gql_for(issues, changed=(), elsewhere=elsewhere)
        code, out = self.tick(gql)
        self.assertEqual((code, out), (0, "prune-deferred issues=16"))
        self.assertEqual(self.counts(gql), [6, 4])
        self.assertNotIn(linear_tracker.Q_CHANGED, gql.calls)

    def test_a_cold_start_converges(self):
        issues = self.dirs(300)
        elsewhere = list(issues)[:20]
        gql = gql_for(issues, changed=(), elsewhere=elsewhere)
        singles = []
        for want in ([6, 4], [0, 10], [0, 6], [0, 0]):
            with self.subTest(want=want):
                self.tick(gql)
                self.assertEqual(self.counts(gql), want)
                singles += [c for c in gql.read if c in elsewhere and want[0] == 0]
        self.assertEqual(sorted(singles), sorted(elsewhere[4:]))

    def test_a_quiet_tick_reads_only_the_feed_and_finished_page(self):
        for n in (300, 10):
            with self.subTest(dirs=n):
                self.fresh()
                os.makedirs(self.logs, exist_ok=True)
                if os.path.exists(self.cache()):
                    os.remove(self.cache())
                gql = gql_for(self.dirs(n), changed=())
                self.tick(gql)
                self.assertEqual(self.tick(gql), (0, ""))
                self.assertEqual(gql.calls, [linear_tracker.Q_CHANGED, linear_tracker.Q_FINISHED])

    def test_a_changed_issue_is_read_and_acted_on(self):
        self.record("TASK-7", SID, [A])
        gql = gql_for({"TASK-7": ("In Progress", [(30, "In Progress")])}, changed=())
        self.assertEqual(self.tick(gql), (0, ""))
        gql.issues["TASK-7"] = ("Done", [(30, "In Progress"), (3, "Done")])
        self.assertEqual(self.tick(gql), (0, ""))
        self.assertEqual(gql.read, [])
        gql.changed = {"TASK-7"}
        self.assertEqual(
            self.tick(gql),
            (0, f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir"),
        )
        self.assertEqual(gql.read, ["TASK-7"])

    def test_no_cache_trusts_nothing(self):
        gql = gql_for(self.dirs(3), changed=())
        self.tick(gql)
        self.assertEqual(self.counts(gql), [1, 0])
        self.assertEqual(sorted(gql.read), ["TASK-100", "TASK-101", "TASK-102"])

    def test_an_unreadable_cache_starts_cold(self):
        gql = gql_for(self.dirs(3), changed=())
        self.tick(gql)
        write(self.cache(), "{")
        code, out = self.tick(gql)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^prune-skip reason=prune.json unreadable: \S")
        self.assertEqual(self.counts(gql), [1, 0])
        self.assertNotIn(linear_tracker.Q_CHANGED, gql.calls)

    def test_a_feed_over_its_pages_trusts_nothing(self):
        inner = gql_for(self.dirs(3), changed=())
        self.tick(inner)
        pages = []

        burst = NOW + timedelta(hours=2)

        def gql(query, **v):
            if query != linear_tracker.Q_CHANGED:
                return inner(query, **v)
            pages.append(v["c"])
            node = {"identifier": f"TASK-{900 + len(pages)}", "updatedAt": burst.isoformat()}
            more = {"hasNextPage": True, "endCursor": str(len(pages))}
            return {"issues": {"nodes": [node], "pageInfo": more}}

        inner.calls.clear()
        inner.read.clear()
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(len(pages), prune.SYNC_PAGES)
        self.assertEqual(self.counts(inner), [1, 0])
        self.assertEqual(prune_state.load(self.cache())[0].synced, burst)
        self.assertEqual(self.tick(inner), (0, ""))
        self.assertEqual(inner.calls, [linear_tracker.Q_CHANGED, linear_tracker.Q_FINISHED])

    def test_a_feed_error_trusts_nothing(self):
        inner = gql_for(self.dirs(3), changed=())
        self.tick(inner)

        def gql(query, **v):
            if query == linear_tracker.Q_CHANGED:
                raise LinearError("linear api error: feed down")
            return inner(query, **v)

        inner.calls.clear()
        inner.read.clear()
        self.assertEqual(
            self.prune(gql),
            (3, "prune-error error=change feed: LinearError: linear api error: feed down"),
        )
        self.assertEqual(self.counts(inner), [1, 0])

    def synced(self):
        return prune_state.load(self.cache())[0].synced

    def test_a_host_clock_ahead_misses_no_reopen(self):
        self.record("TASK-7", SID, [A])
        issues = {"TASK-7": ("Done", [(30, "In Progress"), (0.5, "Done")])}
        gql = gql_for(issues, changed="since")
        ahead = NOW + timedelta(hours=1)
        self.assertEqual(self.tick(gql, now=ahead), (0, ""))
        issues["TASK-7"] = (
            "In Progress",
            [(30, "In Progress"), (0.5, "Done"), (-5 / 60, "In Progress")],
        )
        self.assertEqual(self.tick(gql, now=ahead + timedelta(minutes=10)), (0, ""))
        self.assertEqual(gql.read, ["TASK-7"])
        self.assertEqual(self.tick(gql, now=ahead + timedelta(minutes=40)), (0, ""))
        self.assertEqual(gql.read, [])
        self.assertEqual(self.closed(), [])
        self.assertIsNotNone(self.checkouts("TASK-7"))
        self.assertEqual(self.synced(), NOW + timedelta(minutes=5))

    def test_a_host_clock_behind_still_runs_the_feed(self):
        gql = gql_for(self.dirs(10), changed="since")
        behind = NOW - timedelta(hours=1)
        self.tick(gql, now=behind)
        self.tick(gql, now=behind + timedelta(minutes=5))
        self.assertEqual(gql.calls, [linear_tracker.Q_CHANGED, linear_tracker.Q_FINISHED])

    def test_the_cursor_is_the_ticks_anchor(self):
        old = NOW - timedelta(hours=30)
        for case in ("warm", "warm quiet", "cold", "cold with nothing"):
            with self.subTest(case):
                self.fresh()
                if os.path.exists(self.cache()):
                    os.remove(self.cache())
                if case == "cold with nothing":
                    self.tick(gql_for({}, changed=()))
                    self.assertIsNone(self.synced())
                    continue
                issues = self.dirs(2, state=("In Review", [(30, "In Review")]))
                gql = gql_for(issues, changed=())
                self.tick(gql)
                self.assertEqual(self.synced(), old)
                if case == "warm":
                    gql.changed = {"TASK-100"}
                    self.tick(gql)
                    self.assertEqual(self.synced(), gql.clock)
                elif case == "warm quiet":
                    gql.issues["TASK-9"] = ("Canceled", [(-14 / 60, "Canceled")])
                    self.tick(gql, now=NOW + prune.BACKSTOP)
                    self.assertEqual(len(gql.read), 2)
                    self.assertEqual(self.synced(), old)

    def test_a_slow_tick_keeps_the_cursor_at_the_feed(self):
        for case in ("a", "b"):
            with self.subTest(case):
                self.fresh()
                if os.path.exists(self.cache()):
                    os.remove(self.cache())
                issues = self.dirs(2, state=("Done", [(1, "Done")]), start=7)
                issues["TASK-8"] = ("In Review", [(2, "In Review")])
                inner = gql_for(issues, owners={"TASK-9": "u-pm"}, changed="since")
                self.tick(inner)
                self.assertEqual(self.synced(), NOW - timedelta(hours=1))
                if case == "a":
                    issues["TASK-8"] = ("In Review", [(2, "In Review"), (0, "In Review")])

                def gql(query, inner=inner, issues=issues, **v):
                    if query == linear_tracker.Q_FINISHED and "TASK-9" not in issues:
                        issues["TASK-7"] = ("In Progress", [(1, "Done"), (-1 / 60, "In Progress")])
                        issues["TASK-9"] = ("Done", [(-14 / 60, "Done")])
                    return inner(query, **v)

                inner.calls.clear()
                self.prune(gql)
                self.assertEqual(self.synced(), NOW if case == "a" else NOW - timedelta(hours=1))
                self.tick(inner)
                self.assertIn("TASK-7", inner.read)

    def test_a_named_issue_unchanged_since_its_read_is_not_read_again(self):
        gql = gql_for(self.dirs(3, state=("In Review", [(0.1, "In Review")])), changed="since")
        self.tick(gql)
        self.assertEqual(self.tick(gql), (0, ""))
        self.assertEqual(gql.calls, [linear_tracker.Q_CHANGED, linear_tracker.Q_FINISHED])

    def test_a_day_old_entry_is_read_again(self):
        gql = gql_for(self.dirs(3), changed=())
        self.tick(gql)
        self.tick(gql, now=NOW + prune.BACKSTOP - timedelta(seconds=1))
        self.assertEqual(self.counts(gql), [0, 0])
        self.tick(gql, now=NOW + prune.BACKSTOP)
        self.assertEqual(self.counts(gql), [1, 0])

    def test_an_entry_read_in_the_future_is_read_again(self):
        gql = gql_for(self.dirs(1), changed=())
        later = NOW + timedelta(hours=1)
        snap = Snapshot(IssueState.REVIEW, NOW - timedelta(hours=30))
        state = PruneState(
            synced=NOW - timedelta(minutes=5), issues={"TASK-100": Entry(snap, later)}
        )
        os.makedirs(self.logs, exist_ok=True)
        prune_state.save(self.cache(), state)
        self.tick(gql)
        self.assertEqual(gql.read, ["TASK-100"])

    def test_reads_past_the_cap_wait_their_turn(self):
        gql = gql_for(self.dirs(600), changed=())
        self.assertEqual(self.tick(gql), (0, "prune-deferred issues=100"))
        self.assertEqual(len(gql.read), 500)
        self.assertEqual(self.tick(gql), (0, ""))
        self.assertEqual(sorted(gql.read), [f"TASK-{n}" for n in range(600, 700)])

    def test_a_named_issue_whose_read_failed_is_read_next_tick(self):
        for how in ("deferred", "batch error", "inventory unknown"):
            with self.subTest(how=how):
                self.fresh()
                if os.path.exists(self.cache()):
                    os.remove(self.cache())
                self.unknown = False
                self.mkc("TASK-999")
                self.record("TASK-999", SID, [A])
                issues = {"TASK-999": ("In Progress", [(30, "In Progress")])}
                inner = gql_for(issues, changed=())
                self.tick(inner)
                issues["TASK-999"] = ("Done", [(30, "In Progress"), (3, "Done")])
                inner.changed = {"TASK-999"}
                gql = inner
                if how == "deferred":
                    issues |= self.dirs(500)
                elif how == "batch error":

                    def gql(query, inner=inner, **v):
                        if query == linear_tracker.Q_SNAPSHOTS:
                            raise LinearError("linear api error: down")
                        return inner(query, **v)

                else:
                    self.unknown = True
                inner.calls.clear()
                inner.read.clear()
                self.prune(gql)
                self.assertNotIn("TASK-999", inner.read)
                self.assertIsNotNone(self.checkouts("TASK-999"))
                self.unknown = False
                inner.changed = set()
                code, out = self.tick(inner)
                self.assertIn("TASK-999", inner.read)
                self.assertIn("prune-removed TASK-999 what=agent dir", out)
                self.assertIsNone(self.checkouts("TASK-999"))

    def test_a_batch_error_skips_its_issues(self):
        inner = gql_for(self.dirs(60), changed=())
        first = []

        def gql(query, **v):
            if query == linear_tracker.Q_SNAPSHOTS and not first:
                first.append(v)
                raise LinearError("linear api error: down")
            return inner(query, **v)

        code, out = self.prune(gql)
        failed = [f"TASK-{n}" for n in range(100, 150)]
        self.assertEqual(code, 3)
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-error {i} error=issue read: LinearError: linear api error: down"
                for i in failed
            ],
        )
        self.assertEqual(sorted(inner.read), [f"TASK-{n}" for n in range(150, 160)])
        self.assertEqual(sorted(prune_state.load(self.cache())[0].issues), inner.read)
        self.assertEqual(self.tick(inner), (0, ""))
        self.assertEqual(sorted(inner.read), failed)

    def test_a_gone_issue_is_skipped_and_not_read_again(self):
        os.makedirs(self.cfg.issue_workdir("TASK-7"))
        gql = gql_for(self.dirs(1), changed=())  # a live issue gives the cold tick its anchor
        self.assertEqual(self.tick(gql), (0, "prune-skip TASK-7 reason=not on the board"))
        self.assertEqual(self.counts(gql), [1, 1])
        self.assertEqual(self.tick(gql), (0, ""))
        self.assertEqual(self.counts(gql), [0, 0])
        self.assertTrue(os.path.isdir(self.cfg.issue_workdir("TASK-7")))

    def test_a_tick_seeing_no_time_stays_cold(self):
        os.makedirs(self.cfg.issue_workdir("TASK-7"))
        gql = gql_for({}, changed=())
        self.tick(gql)
        self.tick(gql)
        self.assertNotIn(linear_tracker.Q_CHANGED, gql.calls)
        self.assertEqual(self.counts(gql), [1, 1])

    def test_another_teams_finished_issue_is_pruned(self):
        self.record("OPS-7", SID, [A])
        os.makedirs(self.cfg.issue_workdir("OPS-8"))
        done = ("Elsewhere Done", [(30, "In Progress")])
        gql = gql_for(
            {"OPS-7": done, "OPS-8": done},
            changed=(),
            elsewhere=("OPS-7", "OPS-8"),
            stamps={"OPS-7": {"completedAt": 3}, "OPS-8": {"completedAt": 8 * 24}},
        )
        code, out = self.tick(gql)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-closed OPS-7 session={SID}",
                "prune-removed OPS-7 what=agent dir",
                "prune-removed OPS-8 what=workdir",
            ],
        )
        self.assertFalse(os.path.exists(self.cfg.issue_workdir("OPS-8")))

    def test_a_dry_run_never_writes_the_cache(self):
        gql = gql_for(self.dirs(3), changed=())
        self.tick(gql, dry=True)
        self.assertFalse(os.path.exists(self.cache()))

    def test_a_rate_limit_writes_no_cache(self):
        self.dirs(3)

        def limited(_query, **_v):
            raise Unavailable("issues", status=400, reason="RATELIMITED")

        with self.assertRaises(Unavailable):
            self.prune(limited)
        self.assertFalse(os.path.exists(self.cache()))


class ArchiveBound(Bound):
    done = ("Done", [(30, "Done")])

    def candidates(self, n, start=1000):
        idents = [f"TASK-{start + k}" for k in range(n)]
        return dict.fromkeys(idents, self.done), dict.fromkeys(idents, "u-pm")

    def test_a_cold_tick_stays_within_the_bound(self):
        bound = prune.SYNC_PAGES + prune.READ_REQUESTS + prune.ARCHIVE_REQUESTS
        self.assertEqual(bound, 33)
        for dirs, finished in ((300, 120), (10, 5)):
            with self.subTest(dirs=dirs, finished=finished):
                self.fresh()
                if os.path.exists(self.cache()):
                    os.remove(self.cache())
                issues = self.dirs(dirs)
                done, owners = self.candidates(finished)
                gql = gql_for(issues | done, owners, elsewhere=list(issues)[:20])
                self.tick(gql)
                self.assertLessEqual(len(gql.calls), bound)

    def test_archive_past_the_cap_waits(self):
        done, owners = self.candidates(30)
        gql = gql_for(done, owners, changed=())
        code, out = self.tick(gql)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "prune-deferred archive=21")
        self.assertEqual(len(gql.archived), 9)
        self.assertEqual(len(gql.calls), prune.ARCHIVE_REQUESTS)
        for _ in range(3):
            self.tick(gql)
        self.assertEqual(len(gql.archived), 30)

    def test_an_issue_too_big_to_plan_lets_the_rest_go(self):
        big = {f"SUB-{k}": "completed" for k in range(25)}
        children = {"TASK-1": big} | {c: {f"LEAF-{c}": "completed"} for c in big}
        gql = gql_for(
            {"TASK-1": self.done, "TASK-2": self.done},
            owners={"TASK-1": "u-pm", "TASK-2": "u-pm"},
            children=children,
            changed=(),
        )
        self.assertEqual(
            self.tick(gql)[1].splitlines(),
            [f"prune-skip TASK-1 reason={prune.TOO_BIG}", "prune-deferred archive=1"],
        )
        self.assertEqual(self.tick(gql), (0, "prune-archived TASK-2"))
        self.assertEqual(gql.archived, ["id-TASK-2"])
        self.assertEqual(gql.calls.count(linear_tracker.Q_ARCHIVE), 1)

    def test_a_dry_run_counts_the_archive_write(self):
        big = {f"SUB-{k}": "completed" for k in range(18)}
        children = {"TASK-1": big} | {c: {f"LEAF-{c}": "completed"} for c in big}
        subs = [*big, *(f"LEAF-{c}" for c in big)]
        for dry in (False, True):
            with self.subTest(dry=dry):
                if os.path.exists(self.cache()):
                    os.remove(self.cache())
                gql = gql_for(
                    {"TASK-1": self.done} | dict.fromkeys(subs, self.done),
                    owners=dict.fromkeys(["TASK-1", *subs], "u-pm"),
                    children=children,
                    changed=(),
                )
                self.assertEqual(
                    self.tick(gql, dry=dry)[1].splitlines(),
                    [
                        f"prune-skip TASK-1 reason={prune.TOO_BIG}",
                        f"prune-deferred archive={len(subs)}",
                    ],
                )
                self.assertEqual(len(gql.calls), prune.ARCHIVE_REQUESTS)

    def test_a_list_leaving_no_room_to_archive_waits(self):
        done, owners = self.candidates(19 * 50 + 1)
        gql = gql_for(done, owners, changed=())
        self.assertEqual(self.tick(gql), (0, f"prune-deferred archive={19 * 50 + 1}"))
        self.assertEqual(prune_state.load(self.cache())[0].refused, {})

    def test_a_finished_list_past_the_cap_waits(self):
        done, owners = self.candidates(30)
        gql = gql_for(done, owners, page=1, changed=())
        self.assertEqual(self.tick(gql), (0, "prune-deferred archive=all"))
        self.assertEqual(gql.archived, [])

    def test_a_refused_candidate_is_replanned_only_on_change(self):
        gql = gql_for(
            {"TASK-1": self.done, "TASK-3": ("In Progress", [])},
            owners={"TASK-1": "u-pm"},
            children={"TASK-1": {"TASK-2": "completed", "TASK-3": "started"}},
            changed=(),
        )
        skip = "prune-skip TASK-1 reason=archive: sub-issue TASK-3 not finished"
        self.assertEqual(self.tick(gql), (0, skip))
        self.assertEqual(gql.calls.count(linear_tracker.Q_ARCHIVE), 1)
        self.tick(gql)
        self.assertEqual(gql.calls, [linear_tracker.Q_CHANGED, linear_tracker.Q_FINISHED])
        gql.changed = {"TASK-2"}
        self.tick(gql)
        self.assertEqual(gql.calls.count(linear_tracker.Q_ARCHIVE), 0)
        gql.changed = {"TASK-3"}
        self.tick(gql)
        self.assertEqual(gql.calls.count(linear_tracker.Q_ARCHIVE), 1)
        self.tick(gql, now=NOW + prune.BACKSTOP)
        self.assertEqual(gql.calls.count(linear_tracker.Q_ARCHIVE), 1)

    def test_a_quarantined_sub_issue_holds_its_parent_only_until_due(self):
        gql = gql_for(
            {"TASK-1": self.done, "TASK-2": ("Done", [(1, "Done")])},
            owners={"TASK-1": "u-pm", "TASK-2": "u-pm"},
            children={"TASK-1": {"TASK-2": "completed"}},
            changed=(),
        )
        skip = "prune-skip TASK-1 reason=archive: sub-issue TASK-2 finished under 2 hours ago"
        self.assertEqual(self.tick(gql), (0, skip))
        self.assertEqual(self.tick(gql, now=NOW + timedelta(hours=1)), (0, "prune-archived TASK-1"))
        self.assertEqual(gql.archived, ["id-TASK-1"])

    def test_a_changed_archive_candidate_waits_for_a_consistent_list(self):
        for changed in ("TASK-1", "TASK-2"):
            for dry in (False, True):
                with self.subTest(changed=changed, dry=dry):
                    if os.path.exists(self.cache()):
                        os.remove(self.cache())
                    issues = {"TASK-1": self.done}
                    children = {}
                    if changed == "TASK-2":
                        issues[changed] = self.done
                        children = {"TASK-1": {changed: "completed"}}
                    gql = gql_for(
                        issues,
                        owners=dict.fromkeys(issues, "u-pm"),
                        children=children,
                        changed=(),
                    )

                    def recompleted(query, gql=gql, issues=issues, changed=changed, **v):
                        result = gql(query, **v)
                        if query == linear_tracker.Q_FINISHED:
                            issues[changed] = ("Done", [(1, "Done")])
                        return result

                    code, out = self.prune(recompleted, dry=dry)
                    self.assertEqual(code, 0)
                    self.assertIn("changed since archive candidate read", out)
                    self.assertEqual(gql.archived, [])
                    if not dry:
                        self.assertEqual(prune_state.load(self.cache())[0].refused, {})
                    self.assertNotIn("prune-archived", self.tick(gql, dry=dry)[1])
                    self.assertEqual(gql.archived, [])
                    code, out = self.tick(gql, now=NOW + timedelta(hours=1), dry=dry)
                    self.assertEqual(code, 0)
                    self.assertIn("prune-plan" if dry else "prune-archived", out)
                    self.assertEqual(gql.archived, [] if dry else ["id-TASK-1"])

    def test_a_parent_held_by_a_sub_issue_is_replanned_only_on_change(self):
        gql = gql_for(
            {"TASK-1": self.done, "TASK-2": self.done},
            owners={"TASK-1": "u-pm", "TASK-2": "u-researcher"},
            children={"TASK-1": {"TASK-2": "completed"}},
            changed=(),
        )
        skip = "prune-skip TASK-1 reason=archive: sub-issue TASK-2 not an archive candidate"
        self.assertEqual(self.tick(gql), (0, skip))
        self.tick(gql)
        self.assertEqual(gql.calls, [linear_tracker.Q_CHANGED, linear_tracker.Q_FINISHED])
        gql.changed = {"TASK-2"}
        self.tick(gql)
        self.assertEqual(gql.calls.count(linear_tracker.Q_ARCHIVE), 1)
        self.assertEqual(gql.archived, [])

    def test_a_refusal_named_again_unchanged_is_kept(self):
        issues = {"TASK-1": self.done, "TASK-3": ("In Progress", [(0.1, "In Progress")])}
        gql = gql_for(
            issues,
            owners={"TASK-1": "u-pm"},
            children={"TASK-1": {"TASK-3": "started"}},
            changed="since",
        )
        self.tick(gql)
        self.assertEqual(gql.calls.count(linear_tracker.Q_ARCHIVE), 1)
        self.tick(gql)
        self.assertEqual(gql.calls, [linear_tracker.Q_CHANGED, linear_tracker.Q_FINISHED])
        issues["TASK-3"] = ("In Progress", [(0.1, "In Progress"), (0.05, "In Progress")])
        self.tick(gql)
        self.assertEqual(gql.calls.count(linear_tracker.Q_ARCHIVE), 1)

    def test_a_now_finished_sub_issue_lets_the_parent_go(self):
        children = {"TASK-1": {"TASK-3": "started"}}
        issues = {"TASK-1": self.done, "TASK-3": ("In Progress", [(30, "In Progress")])}
        gql = gql_for(
            issues, owners={"TASK-1": "u-pm", "TASK-3": "u-pm"}, children=children, changed=()
        )
        self.tick(gql)
        children["TASK-1"]["TASK-3"], issues["TASK-3"] = "completed", ("Done", [(3, "Done")])
        gql.changed = {"TASK-3"}
        self.assertEqual(self.tick(gql), (0, "prune-archived TASK-1"))

    def test_an_invalidated_refusal_is_replanned_after_a_cut_short_tick(self):
        children = {"TASK-1": {"TASK-3": "started"}}
        issues = {"TASK-1": self.done, "TASK-3": ("In Progress", [(30, "In Progress")])}
        gql = gql_for(
            issues, owners={"TASK-1": "u-pm", "TASK-3": "u-pm"}, children=children, changed=()
        )
        self.tick(gql)
        children["TASK-1"]["TASK-3"], issues["TASK-3"] = "completed", ("Done", [(3, "Done")])
        gql.changed = {"TASK-3"}

        def down(query, **v):
            if query == linear_tracker.Q_ARCHIVE:
                raise LinearError("linear api error: down")
            return gql(query, **v)

        self.assertEqual(self.prune(down)[0], 3)
        gql.changed = set()
        self.assertEqual(self.tick(gql), (0, "prune-archived TASK-1"))

    def test_a_standalone_prune_adds_only_its_setup_reads(self):
        issues = self.dirs(300)
        done, owners = self.candidates(120)
        prune_gql, setup = gql_for(issues | done, owners), []
        write(
            os.path.join(self.ensemble, "config.local.toml"),
            f'[orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL,
        )
        path = os.path.join(self.root, "config.toml")
        write(path, tp.CONFIG)
        fake = tp.FakeLinear()

        def routed(query, **v):
            if query in PRUNE_QUERIES:
                return prune_gql(query, **v)
            setup.append(query)
            return fake(query, **v)

        with redirect_stderr(io.StringIO()):
            prune.main(
                [],
                trackers=cli.TRACKERS,
                gql=routed,
                now=NOW,
                config_path=path,
                pruner=self.pruner,
            )
        self.assertEqual(setup, [ops.Q_SETUP])
        self.assertLessEqual(len(prune_gql.calls) + len(setup), 34)


DONE_3H = "- 2026-09-27 12:00 done self\n"  # NOW - 3 h


class LocalBoardPrune(PruneScenario):
    """ensemble-orchestrator prune on a local board: finished issues archived flat into archive/."""

    def setUp(self):
        super().setUp()
        zone = mock.patch.dict(os.environ, {"TZ": "UTC0"})
        zone.start()
        self.addCleanup(time.tzset)
        self.addCleanup(zone.stop)
        time.tzset()
        self.board = os.path.join(self.root, "board")
        os.environ[config.BOARD_ENV] = self.board
        self.logs = os.path.join(self.board, ".state", "logs")
        write(os.path.join(self.ensemble, "config.local.toml"), "[orchestrator]\n" + CORE_LOCAL)
        self.path = os.path.join(self.root, "config.toml")
        write(self.path, tp.CONFIG.replace('tracker = "linear"', 'tracker = "local"'))

    def issue(self, rel, ident, front="", history=DONE_3H):
        path = os.path.join(self.board, rel, f"{ident} x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = f"---\nid: {ident}\ntitle: x\ncreated: 2026-09-27T09:00:00Z\n{front}---\n"
        write(path, text + f"### History\n{history}")
        return path

    def run_prune(self, *argv):
        fake, err = tp.FakeLinear(), io.StringIO()
        before = len(logged(self.logs)) if os.path.exists(self.logs) else 0
        with redirect_stderr(err):
            code = prune.main(
                list(argv),
                trackers=cli.TRACKERS,
                gql=fake,
                now=NOW,
                config_path=self.path,
                pruner=self.pruner,
            )
        self.assertEqual(fake.calls, [])
        lines = err.getvalue().splitlines()
        events = logged(self.logs)[before:] if os.path.exists(self.logs) else []
        events += [json.loads(line) for line in lines if line.startswith("{")]  # a dry run's
        rest = "".join(f"{line}\n" for line in lines if not line.startswith("{"))
        return code, rest, [show(e) for e in events if e["src"] == "prune"]

    def archived(self):
        folder = os.path.join(self.board, "archive")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def test_skips_an_unfinished_sub_issue_then_archives_flat(self):
        self.issue("pm/done", "LOC-1")
        sub = self.issue("engineer/ready", "LOC-2", front="parent: LOC-1\n", history="")
        self.issue("researcher/done", "LOC-3")  # a researcher's report stays
        code, _, said = self.run_prune("--dry-run")
        self.assertEqual((code, self.archived()), (0, []))
        self.assertIn("prune-skip LOC-1 reason=archive: sub-issue LOC-2 not finished", said)
        code, _, said = self.run_prune()
        self.assertEqual((code, self.archived()), (0, []))
        self.assertIn("prune-skip LOC-1 reason=archive: sub-issue LOC-2 not finished", said)
        os.makedirs(os.path.join(self.board, "engineer/done"))
        os.rename(sub, done := os.path.join(self.board, "engineer/done/LOC-2 x.md"))
        at = (NOW - timedelta(hours=3)).timestamp()
        os.utime(done, (at, at))
        code, _, said = self.run_prune("--dry-run")
        self.assertEqual((code, self.archived()), (0, []))
        self.assertIn("prune-plan LOC-1 what=archive", said)
        code, _, said = self.run_prune()
        self.assertEqual(code, 0, said)
        self.assertEqual(self.archived(), ["LOC-1 x.md", "LOC-2 x.md"])
        self.assertIn("prune-archived LOC-1", said)
        self.assertTrue(os.path.exists(os.path.join(self.board, "researcher/done/LOC-3 x.md")))

    def test_waits_2_hours(self):
        self.issue("pm/done", "LOC-1", history="- 2026-09-27 14:00 done self\n")
        self.assertEqual(self.run_prune()[0], 0)
        self.assertEqual(self.archived(), [])

    def test_an_unreadable_board_exits_2(self):
        self.assertEqual(
            self.run_prune()[:2], (2, f"prune.py: {self.board}: no such board directory\n")
        )
        path = self.issue("pm/done", "LOC-1")
        write(path, "nonsense\n")
        self.assertEqual(self.run_prune()[:2], (2, f"prune.py: {path}: no front matter\n"))

    def test_archived_meanwhile_logs_nothing(self):
        path = self.issue("pm/done", "LOC-1")
        cfg = config.load(path=self.path)
        log.use_logs(cfg.logs_dir)
        board = cli.TRACKERS[cfg.tracker].board(None, cfg)
        pruner = self.pruner(board, None, NOW, dry=False, work=cfg.workdirs, cfg=cfg)
        pruner.tracker.read("LOC-1")  # scanned: LOC-1 done, not archived
        os.makedirs(os.path.join(self.board, "archive"))
        os.rename(path, os.path.join(self.board, "archive/LOC-1 x.md"))  # by another hand
        self.assertEqual(pruner.run(), 0)
        said = [show(e) for e in logged(self.logs) if e["src"] == "prune"]
        self.assertNotIn("prune-archived LOC-1", said)
        self.assertEqual(self.archived(), ["LOC-1 x.md"])
