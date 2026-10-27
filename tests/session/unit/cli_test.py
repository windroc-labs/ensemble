import argparse
import contextlib
import io
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ensemble.session import EnvironmentSpec, SessionBusy, cli
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.tmux_cli import attach_command
from ensemble.session.engine.types import ConfigError, Hosts
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer import display as displays
from ensemble.utils import ACCOUNT_ENV, Record
from tests.session.case import Case
from tests.session.fakes.terminal import ScreenHost
from tests.session.hosted_support import SID, HostedTuiBase
from tests.utils.cli_help import examples, parse_only


class CliHost(ScreenHost):
    current: ScreenHost

    def __init__(self, name, settings):
        self.__dict__ = self.current.__dict__


class OtherHost(CliHost):
    def validate(self, name, launch):
        raise ConfigError("refused on the other Host")


class ReservedName(Case):
    def test_none_as_a_session_name_is_a_usage_error(self):
        self.addCleanup(setattr, HostedSession, "_setup", None)
        for argv in (["start", "none", "--client", "claude"], ["hide", "none"], ["close", "none"]):
            err = io.StringIO()
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(err),
                self.assertRaises(SystemExit) as caught,
            ):
                cli.main(argv)
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("want [A-Za-z0-9_-]+ but not none, not 'none'", err.getvalue())


class WorkerEnvironment(Case):
    def setUp(self):
        self.environment = EnvironmentSpec(
            image="dummy-env:test",
            gateway="workers",
            lock="dummy-lock",
            secrets=({"item": "ensemble-broker-env/dummy", "file": "dummy.txt"},),
        )
        self.addCleanup(setattr, HostedSession, "_setup", None)
        self.workers = mock.MagicMock()
        self.worker = self.workers.new.return_value
        self.record = Record(
            v=1,
            kind="worker",
            name="dummy-1",
            sessions=("dummy-1",),
            sid=SID,
            workdir=self.home,
            ctl=("ensemble-session",),
            resume=("ensemble-session", "resume", "dummy-1"),
            display=None,
            detail="claude",
        )
        self.worker.start.return_value = self.record
        self.workers.load.return_value = self.worker
        self.worker.resume.return_value = self.worker.restart.return_value = self.record
        self.enterContext(mock.patch.dict(os.environ, {"ENSEMBLE_SID": SID}))

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv), worker_cls=self.workers)
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def start(self, *argv):
        return self.invoke(
            "start", "dummy-1", "--client", "claude", "--cwd", self.home, "--display", "none", *argv
        )

    def test_resolved_environment_and_account_reach_the_persisted_worker(self):
        code, out, err = self.start(
            "--environment-spec",
            json.dumps(self.environment.persisted()),
            "--account",
            "work",
        )
        self.assertEqual((code, out, err), (0, self.record.to_line() + "\n", ""))
        fields = self.workers.new.call_args.kwargs
        self.assertEqual((fields["environment"], fields["sid"]), (self.environment, SID))
        self.assertEqual(fields["env"], {ACCOUNT_ENV: "work"})
        self.worker.check.assert_called_once_with(prompt="", resume=False)
        self.worker.start.assert_called_once_with(prompt="", resume=False, hidden=True)

    def test_bad_environment_json_is_rejected_before_a_worker_exists(self):
        valid = self.environment.persisted()
        for data in (
            "{",
            "null",
            "[]",
            "{}",
            json.dumps(valid | {"role": "engineer"}),
            json.dumps(valid | {"prefetch": {"repo": "o/r", "ref": "main"}}),
        ):
            with self.subTest(data=data):
                code, out, err = self.start("--environment-spec", data)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("environment", err)
        self.workers.new.assert_not_called()

    def test_start_without_environment_preserves_the_default_worker(self):
        self.assertEqual(self.start()[0], 0)
        self.assertIsNone(self.workers.new.call_args.kwargs.get("environment"))

    def test_busy_start_restart_and_resume_exit_75_naming_the_holder(self):
        busy = SessionBusy("dummy-lock", "ensemble-other-1 (owner other)")
        for method, argv in (
            (
                "start",
                ("start", "dummy-1", "--client", "claude", "--cwd", self.home, "--display", "none"),
            ),
            ("restart", ("restart", "dummy-1")),
            ("resume", ("resume", "dummy-1")),
        ):
            with (
                self.subTest(method=method),
                mock.patch.object(self.worker, method, side_effect=busy),
            ):
                code, out, err = self.invoke(*argv)
            self.assertEqual((code, out), (75, ""))
            self.assertIn("dummy-lock", err)
            self.assertIn("ensemble-other-1 (owner other)", err)


class RecordControl(HostedTuiBase):
    def setUp(self):
        super().setUp()
        CliHost.current = self.host
        self.addCleanup(setattr, HostedSession, "_setup", None)
        Path(self.ensemble).mkdir(exist_ok=True)
        Path(self.ensemble, "config.local.toml").write_text(
            '[session.hosts.tmux]\nimplementation = "tests.session.unit.cli_test:CliHost"\n'
            '[session.clients.fake]\nimplementation = "tests.session.fake_client:FakeClient"\n'
            "tui = true\n"
            f"scenario = {json.dumps(self.scenario)}\nlog = {json.dumps(self.log)}\n"
        )
        self.record = Record(
            v=1,
            sid=SID,
            kind="worker",
            name="w1",
            sessions=("w1",),
            workdir=None,
            display=None,
            detail=None,
            ctl=("ensemble-session",),
            resume=("ensemble-session", "resume", "w1"),
        )

    def invoke(self, *argv, record=None, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        source = stdin if stdin is not None else "" if record is None else record.to_line() + "\n"
        with (
            mock.patch.object(sys, "stdin", io.StringIO(source)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_record_sid_mismatch_command_results(self):
        replacement = self.host.create("w1", sid="replacement")
        for command in ("status", "close", "show", "hide", "attach-command", "content"):
            with self.subTest(command=command):
                code, out, err = self.invoke(command, record=self.record)
                if command == "status":
                    self.assertEqual(code, 0)
                    self.assertFalse(json.loads(out)["alive"])
                elif command == "close":
                    self.assertEqual((code, out), (0, ""))
                else:
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn(SID, err)
                    self.assertIn("replacement", err)
        replacement.calls.close.assert_not_called()

    def test_start_runs_the_worker_on_its_host_option_and_refuses_an_unknown_one(self):
        with open(Path(self.ensemble, "config.local.toml"), "a") as f:
            f.write(
                '[session.hosts.other]\nimplementation = "tests.session.unit.cli_test:OtherHost"\n'
            )
        code, out, err = self.invoke("start", "w2", "--client", "fake", "--host", "nowhere")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--host nowhere: want one of docker, other, tmux", err)
        code, out, err = self.invoke(
            "start", "w2", "--client", "fake", "--host", "other", "--display", "none"
        )
        self.assertEqual((code, out), (2, ""))
        self.assertIn("refused on the other Host", err)
        self.assertEqual(self.host.processes, {})

    def test_start_account_sets_the_account_variable_and_env_cannot(self):
        with open(Path(self.ensemble, "config.local.toml"), "a") as f:
            f.write(
                'accounts = ["default", "work"]\n[session.hosts.other]\n'
                'implementation = "tests.session.unit.cli_test:OtherHost"\n'
            )
        code, out, err = self.invoke(
            "start", "w2", "--client", "fake", "--env", "ENSEMBLE_ACCOUNT=work"
        )
        self.assertEqual((code, out), (2, ""))
        self.assertIn("ENSEMBLE_ACCOUNT is set by --account or a role's accounts table", err)
        code, out, err = self.invoke("start", "w2", "--client", "fake", "--account", "nope")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("account 'nope': not a registered account of client fake", err)
        self.assertEqual(self.host.processes, {})
        # a registered account passes the selection and reaches the Host
        code, out, err = self.invoke(
            "start", "w2", "--client", "fake", "--account", "work", "--host", "other"
        )
        self.assertEqual((code, out), (2, ""))
        self.assertIn("refused on the other Host", err)

    def test_status_refuses_a_record_with_one_replaced_session(self):
        self.host.create("w1", sid=SID)
        self.host.create("w2", sid="replacement")
        record = replace(self.record, sessions=("w1", "w2"))
        code, out, _ = self.invoke("status", record=record)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["alive"])

    def test_usage_error_keeps_valid_keys(self):
        self.host.create("w1", sid=SID)
        invalid = replace(self.record, name="w9", sessions=())
        code, out, err = self.invoke(
            "close", stdin=self.record.to_line() + "\n" + invalid.to_line() + "\n"
        )
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn("w1", self.host.processes)
        self.assertEqual(err, "ensemble-session: w9: names no session\n")

    def test_matching_private_sid_allows_close_without_record_metadata(self):
        process = self.host.create("w1", sid=SID)
        self.assertIsNone(process.record())
        self.assertEqual(self.invoke("close", record=self.record)[:2], (0, ""))
        self.assertNotIn("w1", self.host.processes)

    def test_all_record_sessions_are_checked_before_effects(self):
        first = self.host.create("w1", sid=SID)
        replacement = self.host.create("w2", sid="replacement")
        record = replace(self.record, sessions=("w1", "w2"))
        for command in ("show", "hide", "close"):
            with self.subTest(command=command):
                self.invoke(command, record=record)
        first.calls.close.assert_not_called()
        replacement.calls.close.assert_not_called()

    def test_duplicate_keys_refuse_before_close(self):
        process = self.host.create("w1", sid=SID)
        self.assertEqual(self.invoke("close", "w1", "w1")[:2], (2, ""))
        process.calls.close.assert_not_called()

    def test_content_takes_exactly_one_key(self):
        self.host.create("w1", sid=SID)
        self.host.create("w2", sid=SID)
        code, out, err = self.invoke("content", "w1", "w2")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("exactly one", err)

    def test_sid_option_rejects_reused_names_without_effects(self):
        self.tui(name="w1", sid=SID)
        replacement = self.host.create("w1", sid="replacement")
        for argv in (
            ("send", "w1", "hello"),
            ("send", "w1", "--key", "Enter"),
            ("reply", "w1"),
            ("read", "w1"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.invoke(*argv, "--sid", SID)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(SID, err)
                self.assertIn("replacement", err)
        replacement.calls.send.assert_not_called()
        replacement.calls.press.assert_not_called()
        self.host.calls.screen.assert_not_called()

    def test_read_keeps_name_only_host_terminals(self):
        self.host.create("plain")
        self.host.calls.screen.return_value = "host screen"
        self.assertEqual(self.invoke("read", "plain")[:2], (0, "host screen\n"))

    def test_matching_sid_allows_send_reply_read(self):
        self.tui(name="w1", sid=SID)
        self.host.calls.screen.return_value = "screen"
        self.assertEqual(self.invoke("send", "w1", "hello", "--sid", SID)[:2], (0, ""))
        self.assertEqual(self.invoke("reply", "w1", "--sid", SID)[:2], (0, ""))
        self.assertEqual(self.invoke("read", "w1", "--sid", SID)[:2], (0, "screen\n"))

    def test_malformed_record_is_usage_error(self):
        self.assertEqual(self.invoke("status", stdin="{}\n")[:2], (2, ""))

    def test_content_record_uses_session_target(self):
        self.tui(name="w1", sid=SID)
        self.host.calls.screen.return_value = "screen"
        record = replace(self.record, name="outer")
        self.assertEqual(self.invoke("content", record=record)[:2], (0, "screen\n"))

    def test_batch_failure_keeps_other_keys(self):
        self.host.create("w1", sid="replacement")
        self.host.create("w2", sid=SID)
        records = (
            self.record.to_line()
            + "\n"
            + replace(self.record, name="w2", sessions=("w2",)).to_line()
            + "\n"
        )
        code, out, err = self.invoke("attach-command", stdin=records)
        self.assertEqual(code, 1)
        self.assertIn(SID, err)
        self.assertEqual(out, f"{attach_command('w2')}\n")


COMMANDS = (
    "start",
    "restart",
    "resume",
    "reply",
    "status",
    "content",
    "show",
    "hide",
    "close",
    "attach-command",
    "ls",
    "send",
    "read",
    "tile",
    "hook",
    "client-resume",
)


class HelpTest(Case):
    def test_descriptions_and_display_prose_fit_eighty_columns(self):
        for command in ("start", "send", "hide"):
            description = self.help([command, "--help"]).split("\n\n")[1]
            with self.subTest(command=command):
                self.assertLessEqual(max(map(len, description.splitlines())), 80)
        for line in displays.display_help(TmuxHost("tmux")).splitlines():
            if not line.strip().startswith("ensemble-"):
                self.assertLessEqual(len(line), 80, line)

    def test_published_operation_examples_use_real_parser_before_dispatch(self):
        for name in [None, *COMMANDS]:
            published = list(examples(self.help(([name] if name else []) + ["--help"])))
            if name not in {"tile", "hook", "client-resume"}:
                self.assertTrue(published, name)
            for command in published:
                with (
                    self.subTest(name=name, command=command),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    parse_only(cli.main, command[1:])

    def help(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            cli.main(argv)
        self.assertEqual(caught.exception.code, 0)
        return out.getvalue()

    def test_every_surface_is_safe_with_unavailable_configuration(self):
        for command in (None, *COMMANDS):
            for flag in ("--help", "-h"):
                with (
                    self.subTest(command=command, flag=flag),
                    mock.patch("pathlib.Path.open", side_effect=OSError("unavailable")),
                    mock.patch("subprocess.Popen", side_effect=AssertionError("operation")),
                    mock.patch("subprocess.run", side_effect=AssertionError("operation")),
                    mock.patch.object(
                        sys,
                        "stdin",
                        mock.Mock(read=mock.Mock(side_effect=AssertionError("stdin"))),
                    ),
                    mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "guard"}),
                ):
                    text = self.help(([command] if command else []) + [flag])
                    self.assertIn("usage:", text)
                    self.assertIn("Examples", text)
                    self.assertIn("ensemble-session", text)
                    if command:
                        self.assertIn("Next help", text)

    def test_exact_hook_help_does_not_even_read_configuration(self):
        with (
            mock.patch("pathlib.Path.open", side_effect=AssertionError("config")),
            mock.patch("builtins.open", side_effect=AssertionError("config")),
        ):
            for flag in ("--help", "-h"):
                self.assertIn("Automation", self.help(["hook", flag]))

    def test_other_hook_arguments_preserve_callback_dispatch(self):
        for tail in (
            [],
            ["/session", "stop"],
            ["/session", "--help"],
            ["--help", "stop"],
            ["-h", "extra"],
        ):
            stream = mock.Mock(buffer=mock.Mock(read=mock.Mock(side_effect=[b"{}", b""])))
            with (
                self.subTest(tail=tail),
                mock.patch.object(sys, "stdin", stream),
                mock.patch("builtins.open", side_effect=OSError("no state")),
                contextlib.redirect_stderr(io.StringIO()),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(cli.main(["hook", *tail]), 0)
                self.assertEqual(stream.buffer.read.call_count, 2)
                self.assertEqual(out.getvalue(), "")

    def test_strict_and_event_are_no_hook_options_but_bad_arguments_exiting_0(self):
        for tail in (
            ["--strict", "/session", "stop"],
            ["/session", "stop", "--strict"],
            ["--event", "g.1", "/session", "stop"],
        ):
            stream = mock.Mock(buffer=mock.Mock(read=mock.Mock(side_effect=[b"{}", b""])))
            with (
                self.subTest(tail=tail),
                mock.patch.object(sys, "stdin", stream),
                mock.patch("builtins.open", side_effect=OSError("no state")),
                contextlib.redirect_stderr(io.StringIO()) as err,
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(cli.main(["hook", *tail]), 0)
                self.assertIn("usage: ensemble-session hook", err.getvalue())
                self.assertEqual(out.getvalue(), "")

    def test_configured_display_is_last_and_fallback_is_useful(self):
        hosts = cli._configure()
        self.assertTrue(
            cli._parser(hosts)
            .format_help()
            .endswith(
                f"  tmux: {displays.display_help(TmuxHost('tmux'))}\n"
                f"  docker: {displays.display_help(TmuxHost('docker'))}\n"
            )
        )
        fallback = cli._parser(None).format_help()
        for text in ("config could not be read", "--display none", "attach-command", "anchor"):
            self.assertIn(text, fallback)
        invalid = Hosts(default="unknown", tables={"unknown": {}})
        self.assertEqual(cli._display_help(invalid), cli._display_help(None))

    def test_tmux_guidance_covers_placement_and_metadata(self):
        text = displays.display_help(TmuxHost("tmux"))
        for term in (
            "OPENER",
            "ANCHOR",
            "right or below",
            "ITERM_SESSION_ID",
            "grid",
            "stack",
            "hidden",
            "printable",
            ";",
            "attach-command",
            "--display none",
        ):
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_root_inventory_and_every_owned_argument_has_help(self):
        parser = cli._parser(None)
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(tuple(sub.choices), COMMANDS)
        for name, leaf in sub.choices.items():
            self.assertTrue(leaf.description, name)
            for action in leaf._actions:
                self.assertTrue(action.help, (name, action.dest))

    def test_start_forwarded_help_remains_after_the_guard_boundary(self):
        with (
            mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "guard"}),
            contextlib.redirect_stderr(io.StringIO()) as err,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(cli.main(["start", "helper", "--client", "codex", "--", "--help"]), 2)
        self.assertIn("refused inside an agent session", err.getvalue())
        self.assertEqual(out.getvalue(), "")
        for display in ("none", "/below/commander"):
            args = cli._parser(None).parse_args(
                ["start", "helper", "--client", "codex", "--display", display]
            )
            self.assertEqual((args.name, args.client, args.display), ("helper", "codex", display))

    def test_examples_and_repeated_options_use_real_parser(self):
        parser = cli._parser(None)
        examples = (
            ["restart", "helper"],
            ["resume", "helper"],
            ["reply", "helper"],
            *(
                [command, "helper"]
                for command in ("status", "content", "show", "hide", "close", "attach-command")
            ),
            ["ls", "--here", "--records"],
            ["send", "helper", "hello"],
            ["send", "helper", "--key", "C-c"],
            ["read", "helper", "--lines", "20"],
            ["tile", "@1"],
            ["hook", "/absolute/session", "stop"],
            ["client-resume", "codex", "resume", "conversation-id"],
        )
        for example in examples:
            with self.subTest(example=example):
                parser.parse_args(example)
        args = parser.parse_args(
            [
                "start",
                "helper",
                "--client",
                "codex",
                "--prompt",
                "first",
                "--prompt",
                "last",
                "--env",
                "X=1",
                "--env",
                "X=2",
            ]
        )
        self.assertEqual((args.prompt, dict(args.env)), ("last", {"X": "2"}))
        args = parser.parse_args(["client-resume", "codex", "--help", "--", "literal"])
        self.assertEqual(args.arguments, ["--help", "--", "literal"])

    def test_invalid_list_and_send_combinations_remain_usage_errors(self):
        parser = cli._parser(None)
        for argv, operation in (
            (["ls", "--descendants"], cli._ls),
            (["send", "helper"], cli._send),
            (["send", "helper", "hello", "--key", "Enter"], cli._send),
        ):
            with self.subTest(argv=argv), self.assertRaises(cli.UsageError):
                operation(parser.parse_args(argv), mock.Mock())
        for argv in (["ls", "--here", "--display", "none"], ["read", "helper", "--lines", "0"]):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                parser.parse_args(argv)
            self.assertEqual(caught.exception.code, 2)
