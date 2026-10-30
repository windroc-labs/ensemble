import argparse
import contextlib
import io
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ensemble.session import EnvironmentSpec, SessionBusy, cli
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import ConfigError, Hosts, Liveness, SessionError
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.types import Op
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.modes.tui import TuiStore
from ensemble.session.viewer import display as displays
from ensemble.utils import ACCOUNT_ENV, Record
from tests.session import harnesses
from tests.session.case import Case
from tests.session.fakes.terminal import ScreenHost
from tests.session.hosted_support import SID, HostedTuiBase
from tests.utils.cli_help import check_examples, help_text, parse_only


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

    def test_credential_declarations_are_forwarded_without_decoding(self):
        for value in (' {"future-kind": [1]}\n', "{", "{}", ""):
            with self.subTest(value=value):
                code, _, err = self.start("--credentials", value)
                self.assertEqual(code, 0, err)
                fields = self.workers.new.call_args.kwargs
                self.assertEqual(fields["credentials"], value)
                self.assertIsNone(fields.get("environment_credentials"))

    def test_environment_credentials_option_is_forwarded_without_decoding(self):
        for value in (' {"future-kind": [1]}\n', "{", "{}", ""):
            with self.subTest(value=value):
                code, _, err = self.start(
                    "--credentials", "caller", "--environment-credentials", value
                )
                self.assertEqual(code, 0, err)
                fields = self.workers.new.call_args.kwargs
                self.assertEqual(fields["environment_credentials"], value)
                self.assertEqual(fields["credentials"], "caller")

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


class ElsewhereHost(ScreenHost):
    """Another configured Host listing nothing, whose attach targets name it."""

    def __init__(self, name, settings):
        super().__init__(name)

    def attach_target(self, name):
        raise SessionError(f"no docker session {name}")


class ControlBase(HostedTuiBase):
    def setUp(self):
        super().setUp()
        self.tmux_process = mock.MagicMock()
        process = self.tmux_process.__enter__.return_value
        process.communicate.return_value = ("", "no server running")
        process.poll.return_value = 1
        process.args = ["tmux"]
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
            mock.patch("subprocess.Popen", return_value=self.tmux_process),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def owned(self, name="w1", *, sid=SID, gone=True, held=True) -> harnesses.HeldOwner:
        """Tui session `name` of `sid` whose session.json names a running session harness."""
        self.tui(name=name, sid=sid)
        path = Path(self.root, name, "session.json")
        state = json.loads(path.read_text())
        owner = harnesses.HeldOwner(self, Channel.create())
        path.write_text(json.dumps({**state, "owner": owner.channel.directory}))
        status = {"state": "running", "name": name, "sid": sid, "generation": state["generation"]}
        if held:
            owner.hold(**status)
        else:
            owner.channel.write_status(status)
        if gone:
            self.host.processes.pop(name)
        return owner


class RecordControl(ControlBase):
    def test_record_sid_mismatch_command_results(self):
        self.tui(name="w1", sid="replacement")
        replacement = self.host.processes["w1"]
        for command in ("status", "close", "show", "hide", "content"):
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
        self.assertIn("ENSEMBLE_ACCOUNT is set by --account or [clients.<client>].account", err)
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
        self.tui(name="w1", sid=SID)
        invalid = replace(self.record, name="w9", sessions=())
        code, out, err = self.invoke(
            "close", stdin=self.record.to_line() + "\n" + invalid.to_line() + "\n"
        )
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn("w1", self.host.processes)
        self.assertEqual(err, "ensemble-session: w9: names no session\n")

    def test_matching_worker_state_sid_allows_close_without_record_metadata(self):
        self.tui(name="w1", sid=SID)
        self.assertEqual(self.invoke("close", record=self.record)[:2], (0, ""))
        self.assertNotIn("w1", self.host.processes)
        self.assertFalse(os.path.exists(os.path.join(self.root, "w1")))

    def test_a_stale_record_still_asks_the_owner_its_session_json_names(self):
        unconfirmed = {"ok": False, "error": "w1: worker exit not confirmed", "unanswered": True}
        refused = {"ok": False, "error": "cannot close w1"}
        served = self.owned().serve(unconfirmed, refused, {"ok": True})
        for reply in (unconfirmed, refused):
            code, out, err = self.invoke("close", record=self.record)
            self.assertEqual((code, out), (1, ""))
            self.assertIn(reply["error"], err)
        self.assertEqual(self.invoke("close", record=self.record)[:2], (0, ""))
        self.assertEqual(served, [Op.CLOSE] * 3)

    def test_a_stale_record_asks_each_owner_after_one_whose_harness_is_gone(self):
        self.owned("w1", held=False)
        served = self.owned("w2").serve({"ok": False, "error": "cannot close w2"})
        record = replace(self.record, sessions=("w1", "w2"))
        code, out, err = self.invoke("close", record=record)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("cannot close w2", err)
        self.assertEqual(served, [Op.CLOSE])

    def test_a_plain_close_error_is_done_only_once_the_record_looks_stale(self):
        """Its harness gone and its worker ABSENT, its leftovers' release fails."""
        for name, stale in (("w1", True), ("w2", False)):
            with self.subTest(stale=stale):
                self.owned(name, held=False)
                self.addCleanup(shutil.rmtree, Path(self.root, name), ignore_errors=True)
                path = Path(self.root, name, "session.json")

                def fail(_name, _generation, stale=stale, path=path):
                    if stale:  # started again meanwhile, under another sid
                        path.write_text(json.dumps({**json.loads(path.read_text()), "sid": "x"}))
                    raise SessionError(f"{_name}: cannot close")

                self.host.calls.release.side_effect = fail
                self.addCleanup(setattr, self.host.calls.release, "side_effect", None)
                record = replace(self.record, name=name, sessions=(name,))
                code, out, err = self.invoke("close", record=record)
                self.assertEqual((code, out), (0, "") if stale else (1, ""), err)

    def test_a_session_failing_its_close_fails_it_after_another_closed(self):
        self.tui(name="w1", sid=SID)
        self.owned("w2", held=False)

        def fail(name, _generation):
            if name == "w2":
                raise SessionError("w2: cannot close")

        self.host.calls.release.side_effect = fail
        self.addCleanup(setattr, self.host.calls.release, "side_effect", None)
        record = replace(self.record, sessions=("w1", "w2"))
        code, out, err = self.invoke("close", record=record)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("w2: cannot close", err)
        self.assertNotIn("w1", self.host.processes)

    def test_a_close_refused_busy_exits_75_unless_another_key_failed_otherwise(self):
        """Its harness gone, its worker generation live or unknown: SessionBusy (AC14)."""
        self.owned("w1", held=False)
        self.addCleanup(self.ended)
        for liveness in (Liveness.LIVE, Liveness.UNKNOWN):
            with self.subTest(liveness=liveness):
                self.host.calls.worker_liveness.return_value = liveness
                code, out, err = self.invoke("close", "w1")
                self.assertEqual((code, out), (75, ""), err)
                self.assertIn("busy", err)
                self.assertTrue(os.path.isdir(os.path.join(self.root, "w1")))
        self.tui(name="w2", sid=SID)
        self.host.calls.release.side_effect = SessionError("w3: cannot close")
        self.owned("w3", held=False)
        self.host.calls.worker_liveness.side_effect = lambda name, _generation: (
            Liveness.UNKNOWN if name == "w1" else Liveness.ABSENT
        )
        code, out, err = self.invoke("close", "w1", "w3")
        self.assertEqual((code, out), (1, ""), err)

    def test_a_stale_record_skips_an_owner_of_another_sid(self):
        channel = self.owned(sid="replacement").channel
        self.assertEqual(self.invoke("close", record=self.record)[:2], (0, ""))
        self.assertFalse(os.path.exists(os.path.join(channel.directory, "requests")))

    def test_an_owner_refusal_is_a_failed_close_even_when_the_record_then_looks_stale(self):
        refused = {"ok": False, "error": "cannot close w1"}
        self.owned(gone=False).serve(refused, before=lambda: self.host.processes.pop("w1"))
        code, out, err = self.invoke("close", record=self.record)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("cannot close w1", err)

    def test_all_record_sessions_are_checked_before_effects(self):
        first = self.host.create("w1", sid=SID)
        replacement = self.host.create("w2", sid="replacement")
        record = replace(self.record, sessions=("w1", "w2"))
        for command in ("show", "hide", "close"):
            with self.subTest(command=command):
                self.invoke(command, record=record)
        first.calls.close.assert_not_called()
        replacement.calls.close.assert_not_called()

    def test_hide_sid_mismatch_marks_nothing(self):
        """AC7: a close hook of an earlier run (its --sid) leaves a later run of the name shown;
        the run's own sid passes through to the hide.
        """
        self.tui(name="w1", sid="replacement")
        code, out, err = self.invoke("hide", "--if-closed", "--sid", SID, "w1")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"session w1: expected sid {SID!r}, found 'replacement'", err)
        self.assertFalse(json.loads(self.invoke("status", "w1")[1])["hidden"])
        code, out, err = self.invoke("hide", "--if-closed", "--sid", "replacement", "w1")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue(json.loads(self.invoke("status", "w1")[1])["hidden"])

    def test_hide_if_closed_needs_the_held_generation_of_a_live_run(self):
        """AC6: a hook's --sid/--generation must be held and live, else exit 1, nothing written."""
        self.owned("w1", held=False)
        self.addCleanup(self.ended)
        generation = json.loads(Path(self.root, "w1", "session.json").read_text())["generation"]

        def hide(sid, run):
            return self.invoke("hide", "--if-closed", "--sid", sid, "--generation", run, "w1")

        def hidden():
            return json.loads(self.invoke("status", "w1")[1])["hidden"]

        code, out, err = hide(SID, generation)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"session w1: run {generation} is not live", err)
        self.host.calls.worker_liveness.return_value = Liveness.LIVE
        self.assertFalse(hidden())
        for sid, run, why in (
            (SID, "other", f"expected generation 'other', found {generation!r}"),
            ("replacement", generation, f"expected sid 'replacement', found {SID!r}"),
        ):
            with self.subTest(why=why):
                code, out, err = hide(sid, run)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"session w1: {why}", err)
                self.assertFalse(hidden())
        self.assertEqual(hide(SID, generation), (0, "", ""))
        self.assertTrue(hidden())

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
        self.tui(name="w1", sid="replacement")
        replacement = self.host.processes["w1"]
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
        read = [call.args[1] for call in self.host.calls.screen.call_args_list]
        self.assertNotIn(80, read)  # its harness's own scans only: no read of the caller's

    def test_matching_sid_allows_send_reply_read(self):
        self.tui(name="w1", sid=SID)
        self.host.calls.screen.return_value = "screen"
        self.assertEqual(self.invoke("send", "w1", "hello", "--sid", SID)[:2], (0, ""))
        self.assertEqual(self.invoke("reply", "w1", "--sid", SID)[:2], (0, ""))
        self.assertEqual(self.invoke("read", "w1", "--sid", SID)[:2], (0, "screen\n"))

    def test_a_send_gate_error_is_printed_and_exits_one(self):
        self.owned().serve({"ok": False, "error": "send blocked by turn interruption"})
        code, out, err = self.invoke("send", "w1", "next")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("turn interruption", err)

    def test_malformed_record_is_usage_error(self):
        self.assertEqual(self.invoke("status", stdin="{}\n")[:2], (2, ""))

    def test_content_record_uses_session_target(self):
        self.tui(name="w1", sid=SID)
        self.host.calls.screen.return_value = "screen"
        record = replace(self.record, name="outer")
        self.assertEqual(self.invoke("content", record=record)[:2], (0, "screen\n"))

    def test_batch_failure_keeps_other_keys(self):
        self.tui(name="w1", sid="replacement")
        self.tui(name="w2", sid=SID)
        records = (
            self.record.to_line()
            + "\n"
            + replace(self.record, name="w2", sessions=("w2",)).to_line()
            + "\n"
        )
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError(2, "no tmux", "tmux")):
            code, out, err = self.invoke("show", stdin=records)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(SID, err)
        self.assertIn("session w2: not shown; attach with /fake/attach w2", err)

    def ended(self):
        """Its Host's fakes back to every worker ended, closing cleanly."""
        self.host.calls.worker_liveness.side_effect = None
        self.host.calls.worker_liveness.return_value = Liveness.ABSENT
        self.host.calls.release.side_effect = None


class Read(ControlBase):
    def test_through_harness(self):
        self.tui(name="w1", sid=SID)
        owner = json.loads(Path(self.root, "w1", "session.json").read_text())["owner"]
        self.host.calls.screen.side_effect = lambda _terminal, lines: f"screen {lines}"
        self.assertEqual(self.invoke("read", "w1", "--lines", "5")[:2], (0, "screen 5\n"))
        self.assertTrue(os.path.isdir(os.path.join(owner, "replies")))  # its harness answered

    def test_harness_gone(self):
        self.owned("w1", held=False, gone=False)
        code, out, err = self.invoke("read", "w1")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("session w1: its harness is gone", err)


class DisplayGone(ControlBase):
    """A harness-owned tmux worker whose display is gone (its Host lists it no more) while the
    default Host is another: every command reaches it on its own Host, through its harness.
    """

    def setUp(self):
        super().setUp()
        with open(Path(self.ensemble, "config.local.toml"), "a") as f:
            f.write(
                '[session.hosts]\ndefault = "other"\n[session.hosts.other]\n'
                'implementation = "tests.session.unit.cli_test:ElsewhereHost"\n'
            )
        self.tui(name="w1", sid=SID)
        state = json.loads(Path(self.root, "w1", "session.json").read_text())
        self.owner, self.generation = state["owner"], state["generation"]
        self.host.calls.list.return_value = []

    def test_status_is_its_harness_with_its_activity(self):
        Path(self.root, "w1", "activity").write_text(f"{self.generation} done\n")
        code, out, err = self.invoke("status", "w1")
        self.assertEqual(code, 0, err)
        found = json.loads(out)
        self.assertEqual((found["alive"], found["state"]), (True, "done"))

    def no_tmux(self):
        """The host tmux's calls fail (no pane to show it in)."""
        return mock.patch("subprocess.Popen", side_effect=FileNotFoundError(2, "no tmux", "tmux"))

    def test_show_asks_its_harness_nothing_then_shows(self):
        with self.no_tmux():
            code, out, err = self.invoke("show", "w1")
        self.assertFalse(os.path.exists(os.path.join(self.owner, "replies")))
        self.assertEqual((code, out), (0, ""), err)
        # the Viewer shows its display on the tmux Host: with no pane to open, its attach command
        self.assertIn("session w1: not shown; attach with /fake/attach w1", err)
        self.assertNotIn("docker", err)

    def test_hide_names_no_other_host(self):
        with self.no_tmux():
            code, out, err = self.invoke("hide", "w1")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue(json.loads(self.invoke("status", "w1")[1])["hidden"])


class HeadlessElsewhere(ControlBase):
    """A named headless run on a Host other than the default: close finds it by its Host's
    record and asks its harness.
    """

    def setUp(self):
        super().setUp()
        with open(Path(self.ensemble, "config.local.toml"), "a") as f:
            f.write(
                '[session.hosts]\ndefault = "other"\n[session.hosts.other]\n'
                'implementation = "tests.session.unit.cli_test:ElsewhereHost"\n'
            )
        self.owner = harnesses.HeldOwner(self, Channel.create())
        found = {"owner": self.owner.channel.directory, "sid": SID, "generation": "g" * 32}
        self.owner.hold(name="h1", **found)
        patcher = mock.patch.object(CliHost, "_headless_owner", return_value=found, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_close_asks_its_harness_on_its_own_host(self):
        served = self.owner.serve({"ok": True})
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError(2, "no tmux", "tmux")):
            code, out, err = self.invoke("close", "h1")
        self.assertEqual((code, out, served), (0, "", [Op.CLOSE]), err)


class Ls(HostedTuiBase):
    def setUp(self):
        super().setUp()
        CliHost.current = self.host
        self.addCleanup(setattr, HostedSession, "_setup", None)
        Path(self.ensemble).mkdir(exist_ok=True)
        Path(self.ensemble, "config.local.toml").write_text(
            '[session.hosts.tmux]\nimplementation = "tests.session.unit.cli_test:CliHost"\n'
        )

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_records_from_worker_dir(self):
        """Each listed worker's Record stored for its session.json's generation; none for another
        generation's, none with no worker state, whatever its Host holds.
        """
        records = {
            name: Record(
                v=1,
                kind="worker",
                name=name,
                sessions=(name,),
                sid=SID,
                workdir=None,
                ctl=("ensemble-session",),
                resume=("ensemble-session", "resume", name),
                display=None,
                detail=None,
            )
            for name in ("w1", "w2", "w3")
        }
        for name, record in records.items():
            self.host.create(name, sid=SID, receipt=record)
        for name, generation in (("w1", "g1"), ("w2", "g1")):
            Path(self.root, name).mkdir(parents=True)
            Path(self.root, name, "session.json").write_text(
                json.dumps({"sid": SID, "generation": generation})
            )
            TuiStore.attach(name, host=self.host).store_record(generation, records[name])
        Path(self.root, "w2", "session.json").write_text(
            json.dumps({"sid": SID, "generation": "g2"})
        )
        self.assertEqual(self.invoke("ls", "--records"), (0, records["w1"].to_line() + "\n", ""))
        self.assertEqual(self.invoke("ls", "--records", "--match", "w[23]"), (0, "", ""))


class ResumeArgv(Case):
    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_clients_resume_argv_as_one_json_array(self):
        self.assertEqual(
            self.invoke("resume-argv", "claude", SID),
            (0, json.dumps(["claude", "--resume", SID]) + "\n", ""),
        )
        self.assertEqual(
            self.invoke("resume-argv", "codex", "t-1"),
            (0, '["codex", "resume", "t-1", "--no-daemon"]\n', ""),
        )

    def test_an_unknown_client_or_a_refused_id_is_a_config_error(self):
        for argv in (
            ("resume-argv", "nobody", SID),
            ("resume-argv", "codex", "--", "-x"),
            ("resume-argv", "codex", "-x"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.invoke(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("ensemble-session", err)

    def test_runs_inside_an_agent_session(self):
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "w1"}):
            self.assertEqual(self.invoke("resume-argv", "codex", "t-1")[0], 0)


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
    "ls",
    "send",
    "read",
    "tile",
    "hook",
    "client-resume",
    "resume-argv",
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
        check_examples(
            self,
            lambda name: self.help(([name] if name else []) + ["--help"]),
            [None, *COMMANDS],
            check=lambda command: parse_only(cli.main, command[1:]),
            optional={"tile", "hook", "client-resume"},
        )

    def help(self, argv):
        return help_text(self, cli.main, argv)

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
        for text in (
            "config could not be read",
            "--display none",
            "ensemble-session status",
            "anchor",
        ):
            self.assertIn(text, fallback)
        invalid = Hosts(default="unknown", tables={"unknown": {}})
        self.assertEqual(cli._display_help(invalid), cli._display_help(None))

    def test_tmux_guidance_covers_placement_and_metadata(self):
        text = displays.display_help(TmuxHost("tmux"))
        for term in (
            "OPENER",
            "ANCHOR",
            "right or below",
            "attach command",
            "grid",
            "stack",
            "hidden",
            "printable",
            ";",
            "ensemble-session status helper",
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
            *([command, "helper"] for command in ("status", "content", "show", "hide", "close")),
            ["ls", "--here", "--records"],
            ["send", "helper", "hello"],
            ["send", "helper", "--key", "C-c"],
            ["read", "helper", "--lines", "20"],
            ["tile", "@1"],
            ["hook", "/absolute/session", "stop"],
            ["client-resume", "codex", "resume", "conversation-id"],
            ["resume-argv", "codex", "conversation-id"],
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
