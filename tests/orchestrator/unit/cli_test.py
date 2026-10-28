"""CLI command selection and usage failures; the launchd jobs that run router and promote."""

import io
import json
import os
import plistlib
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import config, prune, router, run_comments
from ensemble.orchestrator.engine import log as event_log
from ensemble.orchestrator.engine.types import (
    Claim,
    ConfigError,
    State,
    TrackerKind,
)
from ensemble.orchestrator.trackers.linear import client, ops
from ensemble.orchestrator.trackers.linear.linear_tracker import LinearTracker
from ensemble.orchestrator.trackers.local.local_tracker import LocalTracker
from ensemble.utils import SID_ENV, Record, Status
from tests.delegate.fakes.delegate import FakeDelegate
from tests.orchestrator.case import REPO, Case
from tests.orchestrator.linear_run_case import SID, ReadyDelegate, RunCase
from tests.orchestrator.unit import run_fixtures
from tests.orchestrator.unit.board_ids import HARNESS_IDENTITY
from tests.orchestrator.unit.keychain_fixtures import app_transport, token_http_error
from tests.orchestrator.unit.run_fixtures import logged, show
from tests.orchestrator.unit.streams import Stderr
from tests.utils.cli_help import check_examples, parse_only

ISSUE = "11111111-2222-4333-8444-555555555555"


class Commands(Case):
    def test_trackers_cover_every_kind(self):
        self.assertEqual(set(cli.TRACKERS), set(TrackerKind))
        self.assertEqual(cli.TRACKERS[TrackerKind.LINEAR].connect, LinearTracker.connect)


class Wirings(run_fixtures.Base):
    """Each tracker kind's engine wiring: its run reader, role apps and write-back writers."""

    def test_router_help_identifies_linear_delegate_ownership(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(router.main(["--help"], trackers=cli.TRACKERS), 0)
        self.assertIn("delegate (Agent field)", output.getvalue())
        self.assertNotIn("assigned to a role", output.getvalue())

    def test_linear_writes_as_the_role_app_and_the_harness_app(self):
        cfg = config.load()
        gql = mock.Mock(return_value={})
        linear = cli.TRACKERS[TrackerKind.LINEAR]
        identity = linear.identity(cfg, "engineer")
        self.assertEqual(identity, cfg.linear.roles["engineer"])
        writers = linear.writers(gql, cfg, identity)
        self.assertEqual(
            (writers.states, writers.humans, writers.repos),
            (cfg.linear.states, ("me@x.com",), {run_fixtures.PROJECT: "acme/ensemble"}),
        )
        self.assertEqual(
            (writers.tracker.state_ids, writers.tracker.team_id),
            (cfg.linear.states, cfg.linear.team),
        )
        gql.return_value = {"comment": None}
        writers.tracker.find_comment(ISSUE, (SID, "start"))
        gql.assert_called_with(
            ops.Q_COMMENT, identity=identity, c=ops.client_id(identity.user_id, SID, "start")
        )
        writers.harness.find_comment(ISSUE, (SID, "session"))
        gql.assert_called_with(
            ops.Q_COMMENT,
            identity=HARNESS_IDENTITY,
            timeout=run_comments.LIMIT,
            c=ops.client_id(HARNESS_IDENTITY.user_id, SID, "session"),
        )

    def test_linear_reads_a_run_as_the_harness_app(self):
        cfg = config.load()
        reader = cli.TRACKERS[TrackerKind.LINEAR].reader(mock.Mock(), cfg)
        self.assertEqual(
            (reader.gql.keywords, reader.state_ids, reader.team_id),
            (
                {"identity": HARNESS_IDENTITY},
                cfg.linear.states,
                cfg.linear.team,
            ),
        )

    def renewal(self, days, answer):
        """A harness call over a fake Keychain and token endpoint: its stderr."""
        err = io.StringIO()
        with app_transport(HARNESS_IDENTITY, days, answer), redirect_stderr(err):
            client.linear_gql("query { viewer { id } }", identity=HARNESS_IDENTITY)
        return err.getvalue()

    def test_linear_credentials_set_each_commands_renewal_policy(self):
        cfg = config.load()
        event_log.use_logs(cfg.logs_dir)
        cli.TRACKERS[TrackerKind.LINEAR].credentials(cfg, "promote", dry=False)
        self.assertEqual(self.renewal(3, token_http_error(401)), "")
        self.assertEqual(
            [(e["src"], show(e)) for e in logged()],
            [("promote", "token-error identity=harness reason=HTTP 401")],
        )
        cli.TRACKERS[TrackerKind.LINEAR].credentials(cfg, "router", dry=True)
        shown = [
            json.loads(line) for line in self.renewal(3, AssertionError("request")).splitlines()
        ]
        self.assertEqual(
            [(e["src"], e["kind"], e["identity"], round(e["days_left"])) for e in shown],
            [("router", "token", "harness", 3)],
        )
        self.assertEqual(len(logged()), 1)

    def test_a_local_board_sets_no_renewal_policy(self):
        board = os.path.realpath(os.path.join(self.work, "board"))
        cfg = replace(config.load(), tracker=TrackerKind.LOCAL, linear=None, board=board)
        cli.TRACKERS[TrackerKind.LOCAL].credentials(cfg, "prune", dry=True)
        self.assertIn("token-error: harness: HTTP 401", self.renewal(3, token_http_error(401)))

    def test_a_local_board_writes_as_one_tracker_with_no_apps(self):
        board = os.path.realpath(os.path.join(self.work, "board"))
        cfg = replace(config.load(), tracker=TrackerKind.LOCAL, linear=None, board=board)
        local = cli.TRACKERS[TrackerKind.LOCAL]
        self.assertIsNone(local.identity(cfg, "engineer"))
        writers = local.writers(None, cfg, None)
        self.assertIs(writers.tracker, writers.harness)
        self.assertIsInstance(writers.tracker, LocalTracker)
        self.assertEqual(writers.tracker.board, board)
        self.assertEqual((writers.humans, writers.repos, set(writers.states)), ((), {}, set(State)))
        self.assertEqual(
            (writers.states["in_progress"], writers.states["handoff"]), ("running", "approved")
        )
        for made in (local.connect(None, cfg, say=None), local.reader(None, cfg)):
            self.assertIsInstance(made, LocalTracker)

    def test_each_tracker_checks_its_start_rows(self):
        board = os.path.realpath(os.path.join(self.work, "board"))
        linear_cfg = config.load()
        local_cfg = replace(linear_cfg, tracker=TrackerKind.LOCAL, linear=None, board=board)
        row = Claim("TASK-1", SID, "engineer", ISSUE, None, None, "a", "fake")
        local_row = replace(row, issue="LOC-1", uuid="LOC-1")
        linear, local = cli.TRACKERS[TrackerKind.LINEAR], cli.TRACKERS[TrackerKind.LOCAL]
        writes = linear.claimed(mock.Mock(), linear_cfg, row).tracker
        self.assertEqual(writes.gql.keywords, {"identity": linear_cfg.linear.roles["engineer"]})
        self.assertIsInstance(local.claimed(None, local_cfg, local_row).tracker, LocalTracker)
        for wiring, cfg, claim, field, value in (
            (linear, linear_cfg, replace(row, uuid="TASK-1"), "uuid", "TASK-1"),
            (local, local_cfg, replace(local_row, uuid="LOC-2"), "uuid", "LOC-2"),
            (local, local_cfg, replace(local_row, uuid=ISSUE), "uuid", ISSUE),
        ):
            with (
                self.subTest(tracker=cfg.tracker, field=field),
                self.assertRaises(ConfigError) as cm,
            ):
                wiring.claimed(None, cfg, claim)
            self.assertEqual(
                str(cm.exception), f"{claim.issue}: session {SID}'s start row {field}: {value!r}"
            )

    def test_prune_prose_fits_eighty_columns(self):
        for line in prune.HELP.splitlines():
            if not line.strip().startswith("ensemble-"):
                self.assertLessEqual(len(line), 80, line)

    def test_every_help_surface_is_safe_and_its_examples_parse(self):
        names = (
            "",
            "router",
            "promote",
            "prune",
            "status",
            "show",
            "hide",
            "close",
            "attach-command",
            "resume",
            "content",
            "writeback",
            "issue",
            "issue read",
            "issue list",
            "issue lookup",
            "issue create",
            "issue edit",
            "issue move",
            "issue comment",
            "issue archive",
            "issue relation add",
            "issue relation remove",
            "issue label swap",
        )

        def text_of(name):
            for flag in ("-h", "--help"):
                with self.subTest(command=name, flag=flag):
                    output, errors = io.StringIO(), io.StringIO()
                    with (
                        redirect_stdout(output),
                        redirect_stderr(errors),
                        mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}),
                        mock.patch("sys.stdin.read", side_effect=AssertionError("read stdin")),
                        mock.patch("builtins.open", side_effect=AssertionError("opened a file")),
                        mock.patch("subprocess.run", side_effect=AssertionError("ran a process")),
                    ):
                        try:
                            result = cli.main([*name.split(), flag])
                        except SystemExit as error:
                            result = error.code
                    self.assertEqual((result, errors.getvalue()), (0, ""))
                    text = output.getvalue()
                    self.assertIn("ensemble-orchestrator", text)
                    self.assertIn("Example", text)
                    self.assertIn("ensemble-orchestrator --help", text)
            return text

        def check(command):
            if command[1] == "router" and command[2:] not in (["--help"], ["-h"]):
                self.assertIsNotNone(router.options(command[2:]))
            elif command[1] in {"promote", "prune"}:
                with (
                    mock.patch.dict(os.environ),
                    mock.patch("builtins.open", side_effect=StopIteration("config boundary")),
                    mock.patch("pathlib.Path.open", side_effect=StopIteration("config boundary")),
                    self.assertRaises(StopIteration),
                ):
                    cli.main(command[1:])
            else:
                parse_only(cli.main, command[1:])

        check_examples(self, text_of, names, check=check, optional={"writeback"})

    def test_run_control_help_explains_selection_effects_and_recovery(self):
        expected = {
            "status": ("Status", "read-only"),
            "show": ("saved display", "attach-command"),
            "hide": ("continues", "show"),
            "close": ("Terminate", "resume"),
            "attach-command": ("one command per", "printed command"),
            "resume": ("exactly one", "--input", "issue board", "usage gate"),
            "content": ("exactly one", "stdout"),
        }
        for command, phrases in expected.items():
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit):
                    cli.main([command, "--help"])
                text = output.getvalue()
                for phrase in (*phrases, "Record", "ISSUE", "configured", "Exit", "duplicate"):
                    self.assertIn(phrase, text)

    def test_resume_help_says_it_delivers_no_answer_to_a_needs_input_run(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            cli.main(["resume", "--help"])
        text = " ".join(output.getvalue().split())
        for phrase in (
            "Restart exactly one interrupted recorded run",
            "delivers no answer to a needs_input run",
            "ensemble-session send",
            "ensemble-crew reopen --help",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("then resume", text)

    def test_maintenance_and_automation_help_retains_operational_contracts(self):
        expected = {
            "": (
                "reaches Linear as the [linear.harness] OAuth app, whose token any command renews"
                " before a call when under 5 days are left (the issue commands: the [linear.manager]"
                " OAuth app)",
                "{router,promote,prune,issue,status,show,hide,close,attach-command,resume,content,writeback}",
            ),
            "router": (
                "ensemble-orchestrator router",
                "--dry-run",
                "--issue ID",
                "--brake ROLE",
                "--display",
                "--events",
                "at most once",
                "repeat",
                "Keychain",
                "read",
                "claim",
                "ENSEMBLE_EVENTS",
                "TUI (text user interface)",
            ),
            "promote": (
                "ensemble-orchestrator promote",
                "--dry-run",
                "--now",
                "10-minute",
                "Todo",
                "Done",
                "prune",
                "read",
                "repeat",
                "Keychain",
            ),
            "prune": (
                "ensemble-orchestrator prune",
                "--dry-run",
                "2 hours",
                "7 days",
                "uncommitted",
                "read",
                "repeat",
                "Keychain",
                "Canceled/Duplicate (any canceled-type state) on the next run",
                "Docker workspace volumes",
            ),
            "writeback": ("Automation", "one DelegateEvent", "--issue", "--workdir", "Exit"),
        }
        for command, phrases in expected.items():
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output):
                    try:
                        cli.main([*command.split(), "--help"])
                    except SystemExit as error:
                        self.assertEqual(error.code, 0)
                text = output.getvalue()
                for phrase in phrases:
                    self.assertIn(phrase, " ".join(text.split()))

    def test_resume_input_is_refused_before_loading_config(self):
        result, output, errors = _call(["resume", "TASK-1", "--input", "answer"])
        self.assertEqual((result, output), (2, ""))
        self.assertEqual(
            errors, "ensemble-orchestrator: pipeline answers belong on the issue board\n"
        )

    def test_missing_or_unknown_command_or_missing_options_are_usage_errors(self):
        for arguments in (
            [],
            ["outcome"],
            ["unknown"],
            ["writeback"],
            ["writeback", "--issue", "TASK-1"],
        ):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("ensemble-orchestrator", output.getvalue())


class InsideAnAgentSession(Case):
    def test_resume_exits_2_before_config_and_status_still_runs(self):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write('[orchestrator]\ntracker = "linear"\n')
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}):
            for command, first in (
                ("resume", "ensemble-orchestrator: refused inside an agent session"),
                ("status", "ensemble-orchestrator: orchestrator/config.toml: required, unset:"),
            ):
                errors = io.StringIO()
                with self.subTest(command=command), redirect_stderr(errors):
                    self.assertEqual(cli.main([command, "TASK-1"]), 2)
                self.assertTrue(errors.getvalue().startswith(first), errors.getvalue())


LOG = "@HOME@/.ensemble/logs/orchestrator.jsonl"
JOBS = {"router": [], "promote": []}


def plist(job):
    with open(
        os.path.join(REPO, "launchd", f"com.windroc.ensemble.{job}.plist"),
        "rb",
    ) as f:
        return plistlib.load(f)


class LaunchdJobs(Case):
    def test_each_job_runs_the_launcher_in_ensemble_bin(self):
        self.assertTrue(os.access(os.path.join(REPO, "bin", "ensemble-orchestrator"), os.X_OK))
        for job, flags in JOBS.items():
            with self.subTest(job=job):
                job_plist = plist(job)
                self.assertEqual(job_plist["Label"], f"com.windroc.ensemble.{job}")
                self.assertEqual(
                    job_plist["ProgramArguments"],
                    ["@HOME@/.ensemble/bin/ensemble-orchestrator", job, *flags],
                )

    def test_jobs_use_install_placeholders_and_the_default_board_directory(self):
        for job in JOBS:
            for field, expected in (
                ("EnvironmentVariables", {"PATH": "@PATH@"}),
                ("StandardOutPath", LOG),
                ("StandardErrorPath", LOG),
            ):
                with self.subTest(job=job, field=field):
                    self.assertEqual(plist(job)[field], expected)

    def test_router_ticks_on_the_half_hour_and_leaves_its_detached_runs_alive(self):
        router = plist("router")
        self.assertEqual(router["StartCalendarInterval"], [{"Minute": 0}, {"Minute": 30}])
        self.assertTrue(router["AbandonProcessGroup"])
        self.assertNotIn("StartInterval", router)

    def test_promote_runs_every_5_minutes(self):
        promote = plist("promote")
        self.assertEqual(promote["StartInterval"], 300)
        self.assertNotIn("StartCalendarInterval", promote)


def _call(arguments, *, stdin="", tty=False):
    stdout, stderr = io.StringIO(), Stderr(tty=tty)
    with (
        redirect_stdout(stdout),
        redirect_stderr(stderr),
        mock.patch("sys.stdin", io.StringIO(stdin)),
    ):
        result = cli.main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


class Queries(RunCase):
    def test_attach_command_example_supports_ordered_batch_records(self):
        _, first = self.start("TASK-1")
        _, second = self.start("TASK-2")
        with mock.patch.object(
            ReadyDelegate,
            "attach_command",
            lambda view: f"attach {view.workdir}",
        ):
            for arguments, stdin, selected in (
                (["attach-command", "TASK-1"], "", (first,)),
                (["attach-command", "TASK-2", "TASK-1"], "", (second, first)),
                (["attach-command"], second.to_line() + "\n" + first.to_line(), (second, first)),
            ):
                with self.subTest(arguments=arguments, stdin=stdin):
                    result, output, errors = _call(arguments, stdin=stdin)
                    self.assertEqual((result, errors), (0, ""))
                    self.assertEqual(
                        output.splitlines(), [f"attach {record.workdir}" for record in selected]
                    )
        self.assertTrue(FakeDelegate.attach(first.workdir).alive())
        self.assertTrue(FakeDelegate.attach(second.workdir).alive())

    def test_resume_checks_and_removes_the_environment_sid_before_starting(self):
        _, record = self.start()
        for supplied in (None, record.sid, "00000000-0000-4000-8000-000000000000"):
            with self.subTest(supplied=supplied), mock.patch.dict(os.environ):
                os.environ.pop(SID_ENV, None)
                if supplied is not None:
                    os.environ[SID_ENV] = supplied
                FakeDelegate.end(record.workdir)
                before = FakeDelegate.starts(record.workdir)
                result, output, errors = _call(["resume", "TASK-1"])
                self.assertNotIn(SID_ENV, os.environ)
                if supplied not in (None, record.sid):
                    self.assertEqual((result, output), (2, ""))
                    self.assertIn(SID_ENV, errors)
                else:
                    self.assertEqual((result, Record.parse(output)), (0, record), errors)
                self.assertEqual(
                    FakeDelegate.starts(record.workdir),
                    before + (supplied in (None, record.sid)),
                )
                self.assertEqual(
                    FakeDelegate.attach(record.workdir).alive(), supplied in (None, record.sid)
                )

    def test_resume_requires_one_key_before_attaching_or_starting(self):
        _, record = self.start()
        FakeDelegate.end(record.workdir)
        before = FakeDelegate.starts(record.workdir)
        for arguments, stdin in (
            (["resume"], ""),
            (["resume", "TASK-1", "TASK-2"], ""),
            (["resume", "TASK-1", "TASK-1"], ""),
            (["resume", "TASK-8", "TASK-1"], ""),
            (["resume"], record.to_line() + "\n" + record.to_line()),
            (["resume"], record.to_line() + "\ninvalid"),
        ):
            with self.subTest(arguments=arguments, stdin=stdin):
                with mock.patch.object(
                    ReadyDelegate, "attach", wraps=ReadyDelegate.attach
                ) as attach:
                    result, output, errors = _call(arguments, stdin=stdin)
                self.assertEqual((result, output), (2, ""))
                self.assertTrue(errors)
                attach.assert_not_called()
                self.assertEqual(FakeDelegate.starts(record.workdir), before)
                self.assertFalse(FakeDelegate.attach(record.workdir).alive())

    def test_resume_uses_the_record_sid_from_stdin(self):
        _, record = self.start()
        FakeDelegate.end(record.workdir)
        result, output, errors = _call(["resume"], stdin=record.to_line())
        self.assertEqual((result, Record.parse(output), errors), (0, record, ""))
        self.assertEqual(FakeDelegate.starts(record.workdir), 2)

    def test_content_from_key_or_record_prints_the_owned_runs_content(self):
        owner, record = self.start()
        for arguments, stdin in ((["content", "TASK-1"], ""), (["content"], record.to_line())):
            with self.subTest(arguments=arguments):
                self.assertEqual(_call(arguments, stdin=stdin), (0, owner.content() + "\n", ""))

    def test_content_requires_one_valid_key_before_attaching(self):
        _, record = self.start()
        before = FakeDelegate.starts(record.workdir)
        for arguments, stdin in (
            (["content"], ""),
            (["content", "TASK-1", "TASK-2"], ""),
            (["content"], record.to_line() + "\n" + record.to_line()),
            (["content"], "bad Record"),
            (["content"], record.to_line() + "\nbad Record"),
        ):
            with self.subTest(arguments=arguments, stdin=stdin):
                result, output, errors = _call(arguments, stdin=stdin)
                self.assertEqual((result, output), (2, ""))
                self.assertTrue(errors)
                self.assertEqual(FakeDelegate.starts(record.workdir), before)
                self.assertTrue(FakeDelegate.attach(record.workdir).alive())

    def test_status_for_two_issue_keys_or_stdin_records_has_two_ordered_lines(self):
        _, first = self.start("TASK-1")
        _, second = self.start("TASK-2")
        for arguments, stdin in (
            (["status", "TASK-2", "TASK-1"], ""),
            (["status"], second.to_line() + "\n" + first.to_line() + "\n"),
        ):
            with self.subTest(arguments=arguments):
                result, output, errors = _call(arguments, stdin=stdin)
                statuses = [Status.parse(line) for line in output.splitlines()]
                self.assertEqual((result, errors), (0, ""))
                self.assertEqual([status.name for status in statuses], ["TASK-2", "TASK-1"])
                self.assertTrue(all(status.alive for status in statuses))
                self.assertEqual([status.attach for status in statuses], [None, None])

    def test_status_carries_the_delegates_hidden_and_show_hide_reach_the_delegate(self):
        self.start()
        for hidden in (True, False):
            with self.subTest(hidden=hidden):

                def status(view, hidden=hidden):
                    return replace(FakeDelegate.status(view), hidden=hidden)

                with mock.patch.object(ReadyDelegate, "status", status):
                    result, output, errors = _call(["status", "TASK-1"])
                self.assertEqual((result, errors), (0, ""))
                self.assertEqual(Status.parse(output).hidden, hidden)
        calls = []
        with (
            mock.patch.object(ReadyDelegate, "show", lambda view: calls.append(("show", view))),
            mock.patch.object(ReadyDelegate, "hide", lambda view: calls.append(("hide", view))),
        ):
            self.assertEqual([_call([c, "TASK-1"]) for c in ("show", "hide")], [(0, "", "")] * 2)
        self.assertEqual([command for command, _ in calls], ["show", "hide"])
        workdir = self.cfg.issue_workdir("TASK-1")
        self.assertEqual([view.workdir for _, view in calls], [workdir, workdir])

    def test_live_completed_and_interrupted_resume_keep_record_and_start_once(self):
        _, record = self.start()
        for state in ("live", "completed", "interrupted", "live-again"):
            if state == "completed":
                FakeDelegate.finish(record.workdir)
            if state == "interrupted":
                FakeDelegate.end(record.workdir)
            result, output, errors = _call(["resume", "TASK-1"])
            ended = (
                "ensemble-orchestrator: TASK-1: run ended (done); nothing resumed; its"
                " conversation: ensemble-crew reopen TASK-1\n"
            )
            self.assertEqual((result, errors), (0, ended if state == "completed" else ""))
            self.assertEqual(Record.parse(output), record)
            self.assertEqual(
                FakeDelegate.starts(record.workdir),
                2 if state in ("interrupted", "live-again") else 1,
            )
        self.assertEqual(ReadyDelegate.usage_calls, ["researcher"])

    def test_config_drift_without_a_limit_probes_before_resuming(self):
        self.start()
        FakeDelegate.end(self.cfg.issue_workdir("TASK-1"))
        self.configure(default="other")
        result, output, errors = _call(["resume", "TASK-1"], tty=True)
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(Record.parse(output).name, "TASK-1")
        self.assertEqual(ReadyDelegate.usage_calls, ["researcher"])
        self.assertTrue(FakeDelegate.attach(self.cfg.issue_workdir("TASK-1")).alive())
        path = Path(self.cfg.logs_dir) / "orchestrator.jsonl"
        self.assertFalse(path.exists())

    def test_close_controls_only_selected_issue_and_show_failure_names_it(self):
        one, _ = self.start("TASK-1")
        two, _ = self.start("TASK-2")
        result, output, errors = _call(["close", "TASK-1"])
        self.assertEqual((result, output, errors), (0, "", ""))
        self.assertFalse(one.delegate.alive())
        self.assertTrue(two.delegate.alive())
        for command in ("show", "hide", "attach-command"):
            result, output, errors = _call([command, "TASK-2"])
            self.assertEqual((result, output), (1, ""))
            self.assertEqual(len(errors.splitlines()), 1)
            self.assertTrue(errors.startswith("ensemble-orchestrator: TASK-2: "))

    def test_invalid_or_duplicate_input_has_no_control_effect(self):
        owner, record = self.start()
        for arguments, stdin in (
            (["close", "TASK-1", "TASK-1"], ""),
            (["close"], record.to_line() + "\n" + record.to_line()),
            (["close"], record.to_line() + "\ninvalid"),
        ):
            with self.subTest(arguments=arguments, stdin=stdin):
                result, output, errors = _call(arguments, stdin=stdin)
                self.assertEqual((result, output), (2, ""))
                self.assertEqual(len(errors.splitlines()), 1)
                self.assertTrue(owner.delegate.alive())
        self.assertEqual(_call(["close"], stdin=""), (0, "", ""))
        self.assertTrue(owner.delegate.alive())

    def test_batch_configuration_error_preserves_valid_key_output(self):
        self.start()
        result, output, errors = _call(["status", "TASK-8", "TASK-1"])
        self.assertEqual(result, 2)
        self.assertEqual(Status.parse(output).name, "TASK-1")
        self.assertEqual(errors, "ensemble-orchestrator: TASK-8: TASK-8: no recorded run\n")


class OnALocalBoard(RunCase):
    """router, writeback, resume, promote and prune reach the local board; issue reads it too, its
    Linear-only forms exiting 2."""

    def configure(self, default="fake"):
        self.board = os.path.join(os.path.realpath(self.home), "board")
        os.environ[config.BOARD_ENV] = self.board
        super().configure(default)
        local = Path(self.ensemble, "config.local.toml")
        text = local.read_text(encoding="utf-8")
        local.write_text(text.replace('tracker = "linear"', 'tracker = "local"'), encoding="utf-8")
        self.cfg = config.load()

    def test_local_runs_writeback_resume_promote_and_prune(self):
        self.assertEqual(self.cfg.tracker, "local")
        for argv in (["writeback", "--issue", "TASK-1", "--workdir", "/w"], ["resume", "TASK-1"]):
            with self.subTest(argv[0]):
                self.assertNotIn("needs [orchestrator]", _call(argv, stdin="{}\n")[2])
        for command in ("promote", "prune"):
            with self.subTest(command):
                self.assertEqual(
                    _call([command]),
                    (2, "", f"{command}.py: {self.board}: empty board: no assignee directory\n"),
                )

    def test_router_dry_run_reads_the_local_board(self):
        missing = _call(["router", "--dry-run"])
        self.assertEqual(missing[0], 2)
        self.assertEqual(
            missing[2].splitlines()[-1], f"router.py: {self.board}: no such board directory"
        )
        os.makedirs(self.board)
        empty = _call(["router", "--dry-run"])
        self.assertEqual(empty[0], 2)
        self.assertEqual(
            empty[2].splitlines()[-1],
            f"router.py: {self.board}: empty board: no assignee directory",
        )


class WritebackUsage(RunCase):
    def test_writeback_renews_as_writeback(self):
        result, _, _ = _call(
            ["writeback", "--issue", "TASK-1", "--workdir", self.cfg.issue_workdir("TASK-1")],
            stdin="{\n",
        )
        self.assertEqual(result, 2)
        with app_transport(HARNESS_IDENTITY, 3, token_http_error(401)):
            client.linear_gql("query { viewer { id } }", identity=HARNESS_IDENTITY)
        self.assertIn(
            ("writeback", "token-error"), [(e["src"], e["kind"]) for e in logged(self.cfg.logs_dir)]
        )

    def test_what_is_not_one_writable_event_exits_2_without_output(self):
        begun = json.dumps({"v": 1, "kind": "begun", "sid": SID, "attempt": 1})
        other = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"
        workdir = self.cfg.issue_workdir("TASK-1")
        for name, stdin, workdir_arg, error in (
            ("no event", "", workdir, "want one DelegateEvent line"),
            ("two events", begun + "\n" + begun + "\n", workdir, "want one DelegateEvent line"),
            ("not JSON", "{\n", workdir, "not JSON"),
            ("no start row", begun.replace(SID, other) + "\n", workdir, "no start row"),
            ("another workdir", begun + "\n", self.cfg.issue_workdir("TASK-2"), "--workdir"),
        ):
            with self.subTest(name):
                result, output, errors = _call(
                    ["writeback", "--issue", "TASK-1", "--workdir", workdir_arg], stdin=stdin
                )
                self.assertEqual((result, output), (2, ""))
                self.assertIn("ensemble-orchestrator writeback: ", errors)
                self.assertIn(error, errors)
                row = logged(self.cfg.logs_dir)[-1]
                self.assertEqual(
                    (row["src"], row["kind"], row["issue"], row["exit"]),
                    ("writeback", "refused", "TASK-1", 2),
                )
                self.assertIn(error, row["reason"])

    def append_local(self, text):
        with open(os.path.join(self.ensemble, "config.local.toml"), "a", encoding="utf-8") as f:
            f.write(text)

    def test_a_local_key_newer_than_its_code_is_ignored_and_logged(self):
        self.append_local("\n[orchestrator.newer_limits]\nopencode = 1\n")
        other = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"
        begun = json.dumps({"v": 1, "kind": "begun", "sid": other, "attempt": 1})
        workdir = self.cfg.issue_workdir("TASK-1")
        result, output, errors = _call(
            ["writeback", "--issue", "TASK-1", "--workdir", workdir], stdin=begun + "\n"
        )
        unknown = "~/.ensemble/config.local.toml: unknown key 'newer_limits' in [orchestrator]"
        self.assertEqual((result, output), (2, ""))
        self.assertEqual(
            errors.splitlines(),
            [
                f"ensemble-orchestrator writeback: ignored: {unknown}",
                f"ensemble-orchestrator writeback: TASK-1: no start row for session {other}",
            ],
        )
        self.assertEqual(
            [(r["kind"], r["reason"]) for r in logged(self.cfg.logs_dir)],
            [
                ("refused", f"TASK-1: no start row for session {other}"),
                ("config-unknown", unknown),
            ],
        )
        result, _, errors = _call(["status", "TASK-1"])
        self.assertEqual(result, 2)
        self.assertIn("unknown key 'newer_limits'", errors)

    def test_a_configuration_it_cannot_load_is_logged_where_its_logs_go(self):
        self.append_local("\n[orchestrator.usage_limit]\na = 0\n")
        result, output, errors = _call(
            ["writeback", "--issue", "TASK-1", "--workdir", "/w"], stdin="{}\n"
        )
        refused = 'orchestrator/config.toml: [usage_limit] a must be "on" or "off": 0'
        self.assertEqual((result, output, errors), (2, "", f"ensemble-orchestrator: {refused}\n"))
        row = logged(self.cfg.logs_dir)[-1]
        self.assertEqual(
            {k: v for k, v in row.items() if k != "ts"},
            {
                "src": "writeback",
                "kind": "refused",
                "issue": "TASK-1",
                "reason": refused,
                "exit": 2,
            },
        )

    def test_a_configuration_refused_before_its_logs_dir_is_known_is_only_printed(self):
        self.append_local("\nnot = [toml\n")
        result, _, errors = _call(["writeback", "--issue", "TASK-1", "--workdir", "/w"])
        self.assertEqual(result, 2)
        self.assertTrue(errors.startswith("ensemble-orchestrator: "), errors)
        self.assertEqual(logged(self.cfg.logs_dir), [])
