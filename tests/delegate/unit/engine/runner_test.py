"""Pure logic over fake objects and in-process clients."""

import errno
import io
import json
import os
import signal
import sys
from contextlib import redirect_stderr
from dataclasses import asdict
from datetime import datetime
from unittest import mock

from ensemble.delegate import ConfigError, Outcome, Progress, Spec
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.plan import plan
from ensemble.delegate.engine.runner import (
    FIX,
    NUDGE,
    POLICIES,
    STOP_LIMIT,
    Attempt,
    Attempts,
    closing,
    execute,
    report,
    say,
    stopped,
)
from ensemble.delegate.engine.types import Begun, Finished, Layout, Limit, Plan, Result
from ensemble.delegate.event.reply import marks
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import (
    EnvironmentSpec,
    Exited,
    IdleTimeout,
    LimitHit,
    Mode,
    Reply,
    Session,
    SessionBusy,
    SessionError,
    TurnFailed,
)
from ensemble.session.engine.types import Activity
from ensemble.utils import Stopped
from tests.delegate.case import SID, Case
from tests.delegate.replies import bare, block
from tests.delegate.repo import ok
from tests.delegate.runner import (
    INVALID,
    INVALID_ERROR,
    LEFT_OPEN,
    PENDING,
    PR,
    Base,
    executed,
)
from tests.delegate.unit.engine.instruction import ROLES, InstructionCase
from tests.delegate.unit.runs import (
    DONE,
    GAVE_UP,
    NAME,
    START,
    STOP,
    FakeHandle,
    said,
    turn,
)
from tests.session.case import no_spawn


class DeadStream(io.StringIO):
    """stderr on a hung-up terminal."""

    def write(self, _text):
        raise OSError(errno.EIO, "Input/output error")


class InProcess(Base):
    """The in_process-only fake, its run with no child process."""

    def setUp(self):
        super().setUp()
        self.configure_session(
            {
                "fake": {
                    "implementation": "ensemble.session.clients.fake:InProcessFakeClient",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )

    def go(self, *steps, **kw) -> Result:
        with no_spawn():
            return super().go(*steps, mode="in_process", **kw)

    def test_a_done_run(self):
        r = self.go(said(START, block(DONE)))
        sid = self.events[0].sid
        self.assertEqual(self.events, [Begun(sid), Progress("start", "go"), Finished(r)])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        self.assertEqual(self.kinds(), ["session", "input", "progress", "result", "end"])
        self.assertEqual((self.read(self.out), self.launches()[0]["pid"]), ("# Doc\n", os.getpid()))

    def test_a_reply_with_an_outcome_block_prints_only_the_block(self):
        r = self.go(said(START, "Built X, then reviewed it.", block(DONE), "Done."))
        self.assertEqual(r.outcome.status, "done")
        self.assertIn(block(DONE), self.err)
        self.assertIn("Progress (start): go", self.err)
        for prose in ("Built X", "Done.", START):
            self.assertNotIn(prose, self.err)

    def test_a_pull_request_run_passes_the_ready_gate_before_it_finishes(self):
        url, head = "https://github.com/o/n/pull/7", "a" * 40
        view = {"state": "OPEN", "isDraft": True, "headRefOid": head, "statusCheckRollup": []}
        done = {**DONE, "url": url, "updraft": {"status": "converged", "head": head}}
        for data, status, gate, last in (
            (done, "done", "passed", "S"),
            ({**done, "updraft": {"status": "stopped", "head": head}}, "failed", "failed",
             "ready gate: G1: updraft stopped"),
        ):  # fmt: skip
            with self.subTest(status=status):
                self.fresh()
                calls = []

                def gh(argv, calls=calls, **kw):
                    calls.append((argv, kw["timeout"]))
                    out = json.dumps(view) if argv[2] == "view" else ""
                    return ok(out)

                with mock.patch("subprocess.run", side_effect=gh):
                    r = self.go(said(START, block(data)), output=PR)
                self.assertEqual(
                    (r.outcome.status, r.outcome.summary.splitlines()[-1]), (status, last)
                )
                self.assertEqual(self.events[-1], Finished(r))
                self.assertEqual(self.last("result")["outcome"]["status"], status)
                self.assertEqual(bare(self.last("ready_gate"))["status"], gate)
                self.assertEqual(calls[0][0][:4], ["gh", "pr", "view", url])
                self.assertEqual(
                    [argv[2:] for argv, _ in calls[1:]],
                    [["ready", url]] if gate == "passed" else [],
                )
                self.assertEqual("delegate: ready gate: G1" in self.err, gate == "failed")

    def test_a_pull_request_run_without_a_pull_request_link_skips_the_gate(self):
        url = "https://github.com/o/n/compare/main...b?expand=1"
        r = self.go(said(START, block({**DONE, "url": url})), output=PR)
        self.assertEqual((r.outcome.status, r.outcome.url), ("done", url))
        self.assertNotIn("ready_gate", self.kinds())

    def gh_config(self, text=""):
        path = os.path.join(self.home, "gh-config")
        self.write(os.path.join(path, "hosts.yml"), text)
        return mock.patch.dict(os.environ, {"GH_CONFIG_DIR": path, "GH_HOST": ""})

    def test_a_ghe_pull_request_run_is_gated_on_its_own_host(self):
        url, head = "https://ghe.example.com/o/n/pull/3", "a" * 40
        view = {"state": "OPEN", "isDraft": True, "headRefOid": head, "statusCheckRollup": []}
        done = {**DONE, "url": url, "updraft": {"status": "converged", "head": head}}
        calls = []

        def gh(argv, **_):
            calls.append(argv)
            return ok(json.dumps(view) if argv[2] == "view" else "")

        with self.gh_config("ghe.example.com:\n"), mock.patch("subprocess.run", side_effect=gh):
            r = self.go(said(START, block(done)), output=PR)
        self.assertEqual(
            (r.outcome.status, bare(self.last("ready_gate"))["status"]), ("done", "passed")
        )
        self.assertEqual(
            [argv[:4] for argv in calls], [["gh", "pr", "view", url], ["gh", "pr", "ready", url]]
        )

    def test_a_pull_request_url_on_another_host_is_fixed_by_one_resume(self):
        bad = {**DONE, "url": "https://attacker.example/o/n/pull/1"}
        fixed = {**DONE, "url": "https://github.com/o/n/compare/main...b?expand=1"}
        with self.gh_config(), mock.patch("subprocess.run") as run:
            r = self.go(said(START, block(bad)), fixes=[said(block(fixed))], output=PR)
        self.assertEqual((r.outcome.status, r.outcome.url), ("done", fixed["url"]))
        self.assertIn(
            "url host attacker.example is not one of github.com", self.last("invalid")["error"]
        )
        run.assert_not_called()

    def test_a_relative_outcome_file_resolves_in_the_agent_dir(self):
        self.write(os.path.join(Layout(self.work).agent, "notes", "doc.md"), "# From a file\n")
        self.write(os.path.join(self.work, "notes", "doc.md"), "# Outside\n")
        data = {k: v for k, v in DONE.items() if k != "deliverable"}
        r = self.go(said(START, block(data | {"deliverable_file": "notes/doc.md"})))
        self.assertEqual((r.outcome.status, r.outcome.deliverable), ("done", "# From a file\n"))

    def test_an_outcome_file_outside_the_agent_dir_is_invalid(self):
        outside = os.path.join(self.work, "run", "notes.md")
        self.write(outside, "# Outside\n")
        data = {k: v for k, v in DONE.items() if k != "deliverable"} | {"deliverable_file": outside}
        r = self.go(said(START, block(data)), fixes=[said(block(data))])
        self.assertIsNone(r.outcome)
        self.assertIn("under the workdir", r.error)

    def test_on_docker_the_outcomes_files_are_dropped_and_noted(self):
        r = self.go(said(START, block(DONE | {"files": ["/x/spec.md", "/x/plan.md"]})), docker=True)
        self.assertEqual((r.outcome.status, r.outcome.files), ("done", []))
        self.assertEqual(self.last("files_dropped")["count"], 2)
        r = self.go(said(START, block(DONE | {"files": ["/x/spec.md"]})), fixes=[])
        self.assertEqual(self.kinds().count("files_dropped"), 1)

    def test_a_raise_is_the_client_exiting_1_its_traceback_on_stderr(self):
        r = self.go(said(START), {"raise": "boom"})
        self.assertEqual(r, Result(1, None, "the client exited 1"))
        self.assertEqual(
            ([type(e) for e in self.events], self.last("end")["rc"]),
            ([Begun, Progress, Finished], 1),
        )
        lines = self.err.splitlines()
        self.assertLess(
            lines.index("Traceback (most recent call last):"), lines.index("RuntimeError: boom")
        )
        r = self.go(said(START, block(DONE)), {"raise": "boom"})
        self.assertEqual(
            (r.returncode, r.outcome.status, r.error), (1, "done", "the client exited 1")
        )

    def test_silence_longer_than_the_idle_timeout_is_recorded_and_the_run_goes_on(self):
        self.write(
            os.path.join(self.ensemble, "config.local.toml"), "[delegate]\nidle_timeout = 0.3\n"
        )
        r = self.go({"sleep": 1.0}, said(START, block(DONE)))
        self.assertEqual(r.outcome.status, "done")
        idle = [e for e in self.records() if e["kind"] == "idle"]
        self.assertTrue(idle)
        self.assertEqual({e["seconds"] for e in idle}, {0.3})

    def test_an_invalid_block_is_fixed_by_one_resume_of_the_session(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(DONE))])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        first, second = self.launches()
        sid = self.events[0].sid
        self.assertEqual(
            (first["sid"], first["resume"], second["sid"], second["resume"]),
            (sid, False, sid, True),
        )
        self.assertEqual(second["prompt"], FIX.format(error=INVALID_ERROR))
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )


class Handles(Base):
    """Runs over a FakeHandle (start patched as delegate.engine.runner sees it), headless unless
    a test says tui.
    """

    def setUp(self):
        super().setUp()
        self.configure_fake(tui=True)

    def faked(self, *events, fail=None, stop_fail=None, **kw) -> Result:
        self.handle = FakeHandle(events, fail, stop_fail)
        with mock.patch.object(self.driver, "start", side_effect=self.handle.start) as self.started:
            return self.go(**kw)

    def planned(self, **kw) -> Plan:
        """dummy-tester's echo planned with Spec fields `kw`, its agent and run dirs made."""
        os.makedirs(Layout(self.work).agent, exist_ok=True)
        composition = Composer()
        accepted = plan(
            composition.resolve("dummy-tester"),
            Spec("Echo me.", self.work, task="echo", client="fake", out=self.out, **kw),
            driver=self.driver,
            composition=composition,
        )
        os.makedirs(accepted.session.workdir, exist_ok=True)
        return accepted


class HandleRun(Handles):
    def test_execute_refuses_a_tui_plan(self):
        with (
            mock.patch.object(self.driver, "start") as start,
            self.assertRaises(ConfigError) as refused,
        ):
            self.go(mode="tui")
        self.assertEqual(str(refused.exception), "tui: its session harness runs it")
        start.assert_not_called()
        self.assertFalse(os.path.exists(self.channel))

    def test_a_named_headless_session_reaches_the_driver_with_its_name(self):
        for name in (None, "dummy-tester-0f8fad5b"):
            with self.subTest(name=name):
                self.fresh()
                self.faked(turn(START, block(DONE)), Exited(0), session_name=name)
                session = self.started.call_args.args[0].session
                self.assertEqual(
                    (session.mode, session.name, session.events), ("headless", name, None)
                )

    def test_a_draft_written_while_the_session_starts_outlives_the_stale_out(self):
        self.write(self.out, "# Earlier\n")
        self.handle = FakeHandle([turn(START, block(DONE)), Exited(0)])

        def start(spec, **kw):
            self.write(self.out, "# Draft\n")  # its client, already running when start returns
            return self.handle.start(spec, **kw)

        seen = []
        with mock.patch.object(self.driver, "start", side_effect=start):
            self.go(
                on_event=lambda e: (
                    seen.append(self.read(self.out)) if isinstance(e, Begun) else None
                ),
            )
        self.assertEqual(seen, ["# Draft\n"])
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.out))).count("out.md"), 1)

    def test_a_start_ended_by_a_signal_puts_the_stale_out_back(self):
        self.write(self.out, "# Earlier\n")
        with (
            mock.patch.object(self.driver, "start", side_effect=Stopped(143)),
            self.assertRaises(Stopped),
        ):
            self.go()
        self.assertEqual(self.read(self.out), "# Earlier\n")
        self.assertEqual(os.listdir(os.path.dirname(self.out)).count(".out.md.stale"), 0)

    def test_intermediate_reply_progress_is_recorded_and_final_reply_is_not_repeated(self):
        final = "[ensemble-progress:step] finished\n" + block(DONE)
        result = self.faked(
            Reply(START), Reply("intermediate prose"), Reply(final), turn(final), Exited(0)
        )
        self.assertEqual(result.outcome.status, "done")
        self.assertEqual(
            [event for event in self.events if isinstance(event, Progress)],
            [Progress("start", "go"), Progress("step", "finished")],
        )
        self.assertEqual(self.kinds().count("progress"), 2)
        self.assertEqual(self.err.count("intermediate prose"), 1)
        self.assertEqual(self.err.count("Progress (step): finished"), 1)
        self.assertNotIn("missing progress mark", self.err)

    def test_a_usage_limit_ends_the_run_stopping_its_client(self):
        at = 1767225600.0
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        r = self.faked(LimitHit(at), Exited(-signal.SIGHUP))
        self.assertEqual(
            r, Result(-signal.SIGHUP, None, f"usage limit; resets at {when}", Limit(at))
        )
        self.assertEqual(
            (self.handle.calls, self.kinds()), ([("stop",)], ["input", "limit", "result", "end"])
        )

    def test_a_failed_headless_turn_stops_its_session_keeping_its_exit_status(self):
        r = self.faked(Reply(START), TurnFailed("overloaded"), Exited(3))
        self.assertEqual(
            (r.returncode, r.outcome, r.error, self.handle.calls),
            (3, None, "the client's turn failed: overloaded", [("stop",)]),
        )

    def test_a_session_exiting_without_an_outcome_is_the_error(self):
        for events, result in (
            ([STOP, Exited(3)], Result(3, None, "the client exited 3")),
            ([Exited(0)], GAVE_UP),
        ):
            with self.subTest(events=events):
                self.fresh()
                self.assertEqual(self.faked(*events), result)
                self.assertNotIn(("stop",), self.handle.calls)

    def test_an_on_event_raise_stops_the_session_and_propagates(self):
        failure = RuntimeError("boom")

        def boom(event):
            if isinstance(event, Progress):
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            self.faked(turn(START), on_event=boom)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.handle.calls, [("stop",)])
        self.assertEqual(self.last("result")["error"], "stopped: RuntimeError: boom")

    def test_a_failed_stop_is_printed_and_the_runs_exception_raised(self):
        for error in (RuntimeError("boom"), KeyboardInterrupt()):

            def raise_(event, error=error):
                if isinstance(event, Progress):
                    raise error

            with self.subTest(error=error):
                self.fresh()
                with self.assertRaises(type(error)) as cm:
                    self.faked(turn(START), on_event=raise_, stop_fail=SessionError("gone"))
                self.assertIs(cm.exception, error)
                self.assertEqual(
                    (self.handle.calls, self.err.count("delegate: headless: gone\n")),
                    ([("stop",)], 1),
                )
                self.assertTrue(
                    self.last("result")["error"].startswith(f"stopped: {type(error).__name__}")
                )

    def killed(self, **kw):
        """A run whose terminal is hung up at its first progress mark: stderr gone, then
        SIGHUP's Stopped(129); the error it raised.
        """

        def hangup(event):
            if isinstance(event, Progress):
                sys.stderr = DeadStream()
                raise Stopped(128 + signal.SIGHUP)

        with self.assertRaises(BaseException) as raised:
            self.faked(turn(START), on_event=hangup, **kw)
        return raised.exception

    def assert_closed_as_stopped(self):
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        self.assertEqual(
            (self.last("result")["error"], self.last("end")["rc"]), ("stopped: Stopped: 129", 129)
        )

    def test_a_hung_up_run_with_its_stderr_gone_stops_its_session_and_closes_the_run(self):
        for stop_fail in (None, SessionError("worker unknown")):
            with self.subTest(stop_fail=stop_fail):
                self.fresh()
                error = self.killed(stop_fail=stop_fail)
                self.assertEqual((type(error), error.code), (Stopped, 129))
                self.assertEqual(self.handle.calls, [("stop",)])
                self.assert_closed_as_stopped()

    def test_a_hung_up_run_whose_sessions_own_stop_fails_has_it_stopped_by_the_retry(self):
        def hangup():
            yield turn(START)
            sys.stderr = DeadStream()
            yield Reply("a line for the hung-up terminal")
            try:  # the session's own stop on the signal, as HostedSession.events() does
                self.handle.stop()
            except SessionError:
                self.handle.stop_fail = None
            raise Stopped(128 + signal.SIGHUP)

        self.handle = FakeHandle(hangup(), stop_fail=SessionError("worker unknown"))
        with (
            mock.patch.object(self.driver, "start", side_effect=self.handle.start),
            self.assertRaises(Stopped) as raised,
        ):
            self.go()
        self.assertEqual(raised.exception.code, 129)
        self.assertEqual(self.handle.calls[-2:], [("stop",), ("stop",)])
        self.assertIsNone(self.handle.stop_fail)
        self.assert_closed_as_stopped()

    def test_a_stop_that_raises_closes_the_run_and_keeps_its_stopped(self):
        crash = RuntimeError("stop crashed")
        error = self.killed(stop_fail=crash)
        self.assertEqual((type(error), error.code, error.__cause__), (Stopped, 129, crash))
        self.assertEqual(self.handle.calls, [("stop",)])
        self.assert_closed_as_stopped()

        def terminated(event):
            if isinstance(event, Progress):
                raise Stopped(128 + signal.SIGTERM)

        self.fresh()
        with self.assertRaises(Stopped) as raised:
            self.faked(turn(START), on_event=terminated, stop_fail=crash)
        self.assertEqual(raised.exception.code, 143)
        self.assertIn("delegate: headless: stop failed: RuntimeError: stop crashed\n", self.err)

    def test_a_failed_stop_is_printed_and_never_replaces_the_limit_or_stale_out_result(self):
        gone = SessionError("gone")
        r = self.faked(LimitHit(None), stop_fail=gone)
        self.assertEqual(r, Result(0, None, "usage limit; reset time unknown", Limit(None)))
        self.assertEqual(
            (self.handle.calls, self.err.count("delegate: headless: gone\n")), ([("stop",)], 1)
        )
        self.assertEqual(self.kinds(), ["input", "limit", "result", "end"])
        blocker = os.path.join(self.tmp, "file")
        self.write(blocker, "")
        self.fresh()
        r = self.faked(
            turn(START, block(DONE)), stop_fail=gone, out=os.path.join(blocker, "out.md")
        )
        self.assertEqual(
            (r.returncode, r.outcome, self.events, self.handle.calls),
            (1, None, [Finished(r)], [("stop",)]),
        )
        self.assertRegex(r.error, r"^cannot remove the stale out: ")
        self.assertEqual(self.err.count("delegate: headless: gone\n"), 1)

    def test_a_driver_failure_at_start_is_the_result(self):
        os.makedirs(self.work)
        with mock.patch.object(self.driver, "start", side_effect=SessionError("no worker")):
            self.assertEqual(self.go(), Result(1, None, "headless: no worker"))
        self.assertEqual(self.kinds(), ["input", "result", "end"])

    def test_a_busy_start_is_a_busy_result(self):
        os.makedirs(self.work)
        busy = SessionBusy("dummy-lock", "ensemble-x-1 (owner x)")
        with mock.patch.object(self.driver, "start", side_effect=busy):
            r = self.go()
        self.assertEqual(
            r, Result(75, None, "busy: lock dummy-lock is held by ensemble-x-1 (owner x)")
        )
        self.assertEqual(self.events, [Finished(r)])
        self.assertEqual(self.kinds(), ["input", "result", "end"])
        self.assertEqual(self.last("end")["rc"], 75)

    def test_the_runner_records_into_an_existing_run_dir_only(self):
        composition = Composer()
        accepted = plan(
            composition.resolve("dummy-tester"),
            Spec("Echo me.", self.work, task="echo", client="fake"),
            driver=self.driver,
            composition=composition,
        )
        with (
            mock.patch.object(self.driver, "start", side_effect=SessionError("no worker")),
            redirect_stderr(io.StringIO()),
            self.assertRaises(FileNotFoundError),
        ):
            execute(accepted, driver=self.driver)
        self.assertFalse(os.path.lexists(self.work))

    def test_headless_turn_ends_never_nudge_and_an_idle_timeout_is_only_recorded(self):
        r = self.faked(
            *[STOP] * (2 + STOP_LIMIT),
            IdleTimeout(7200),
            turn(START, block(DONE)),
            Exited(0),
        )
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", []))
        self.assertEqual(self.kinds(), ["input", "idle", "progress", "result", "end"])
        args, given = self.started.call_args
        self.assertEqual(
            (given, args[0].session.sid, args[0].session.resume, args[0].session.events),
            ({"idle_timeout": 7200}, self.events[0].sid, False, None),
        )


class TuiStart(Handles):
    """Attempts.begin of a tui plan, as an owned run's drive starts its session."""

    def began(self, handle=None, *, start=None, **kw) -> Session | Result:
        """Over `handle` (else a new FakeHandle), the driver's start `start` (else the handle's);
        self.events gets each event, self.err the stderr.
        """
        self.handle = FakeHandle([]) if handle is None else handle
        accepted = self.planned(mode="tui", **kw)
        self.events, err = [], io.StringIO()
        self.attempts = Attempts(accepted, self.events.append, self.driver)
        try:
            with (
                redirect_stderr(err),
                mock.patch.object(
                    self.driver, "start", side_effect=start or self.handle.start
                ) as self.started,
            ):
                return self.attempts.begin()
        finally:
            self.err = err.getvalue()

    def test_a_tui_handle_without_a_name_reports_a_driver_error(self):
        handle = FakeHandle([])
        handle.name = None
        self.assertEqual(
            self.began(handle, start=lambda *_a, **_kw: handle),
            Result(1, None, "tui: tui session has no name"),
        )

    def test_start_hands_the_plan_to_the_driver_and_prints_the_attach_command(self):
        events = os.path.join(self.tmp, "events.log")
        for kw, want, name in (
            ({}, ({"resume": None, "events": None}, None), NAME),
            (
                {"sid": SID, "resume": True, "name": "mine", "events": events},
                ({"resume": SID, "events": events}, "mine"),
                "mine",
            ),
        ):
            with self.subTest(kw=kw):
                self.fresh()
                self.assertIs(self.began(**kw), self.handle)
                args, given = self.started.call_args
                self.assertEqual((args[0].session.client, args[0].session.mode), ("fake", "tui"))
                self.assertEqual(
                    (
                        given,
                        args[0].session.sid,
                        args[0].session.name,
                        args[0].session.resume,
                        args[0].session.events,
                    ),
                    (
                        {"idle_timeout": 7200},
                        self.events[0].sid,
                        want[1],
                        want[0]["resume"] is not None,
                        want[0]["events"],
                    ),
                )
                self.assertIn(f"delegate: tui session {name}: tmux attach -t '={name}'\n", self.err)

    def test_a_showable_session_is_shown_as_the_configured_show_says(self):
        command = "open -a Terminal {{session}}"
        for local, shown in (
            ("", [None]),
            ('[delegate]\nshow = "none"\n', [None]),
            (f'[delegate]\nshow = "{command}"\n', [command]),
            ('[delegate]\nshow = "none"\n[delegate.roles.dummy-tester]\nshow = ""\n', [None]),
            ('[delegate.roles.dummy-tester]\nshow = "none"\n', [None]),
        ):
            with self.subTest(local=local):
                self.fresh()
                self.write(os.path.join(self.ensemble, "config.local.toml"), local)
                self.driver.display = "mgr"
                self.began()
                self.assertEqual(self.handle.shown, shown)
                self.assertIn(f"delegate: tui session {NAME}: tmux attach -t '={NAME}'\n", self.err)

    def test_a_hidden_session_is_not_shown_at_start(self):
        for local in ("", '[delegate]\nshow = "none"\n', '[delegate]\nshow = "open {{session}}"\n'):
            with self.subTest(local=local):
                self.fresh()
                self.write(os.path.join(self.ensemble, "config.local.toml"), local)
                self.driver.display = "mgr"
                handle = FakeHandle([])
                handle.hidden = True
                self.began(handle)
                self.assertEqual(self.handle.shown, [])
                self.assertIn(f"delegate: tui session {NAME}: tmux attach -t '={NAME}'\n", self.err)

    def test_a_session_started_without_a_display_is_not_shown(self):
        self.began()
        self.assertEqual(self.handle.shown, [])
        self.assertIn(f"delegate: tui session {NAME}: tmux attach -t '={NAME}'\n", self.err)

    def test_a_failed_show_is_printed_and_the_start_goes_on(self):
        for failure in (SessionError(f"no tmux session {NAME}"), SessionConfigError("bad split")):
            with self.subTest(failure=failure):
                self.fresh()
                self.driver.display = "mgr"
                handle = FakeHandle([])
                handle.show_fail = failure
                self.assertIs(self.began(handle), handle)
                self.assertIn(f"delegate: show {NAME}: {failure}\n", self.err)
                self.assertEqual(self.events, [Begun(self.events[0].sid)])

    def test_a_stop_during_the_show_leaves_the_started_session_to_its_drive(self):
        self.driver.display = "mgr"
        handle = FakeHandle([])
        handle.show_fail = Stopped(129)
        with self.assertRaises(Stopped):
            self.began(handle)
        self.assertEqual((self.attempts.handle, self.attempts.begun), (handle, False))

    def test_a_driver_failure_at_start_is_the_result_and_a_client_without_tui_a_config_error(self):
        self.assertEqual(self.began(start=SessionError("no tmux")), Result(1, None, "tui: no tmux"))
        self.assertEqual(self.kinds(), ["input"])
        self.fresh()
        self.configure_fake()
        with self.assertRaises(ConfigError) as cm:
            Attempts(self.planned(mode="tui"), lambda _event: None, self.driver).begin()
        self.assertEqual(str(cm.exception), "client 'fake' does not support tui mode")


LOCKED = EnvironmentSpec(image="dummy-env:test", gateway="workers", lock="dummy-lock")


class LockCloseTest(Handles):
    """A locked headless run's worker, ended with its client or stopped, is closed before its
    Finished and only then claimed released (a locked tui is always owned: handler_test).
    """

    def locked(self, *events, environment=LOCKED, close_fail=None, **kw):
        self.at_finish = None

        def on(event):
            if isinstance(event, Finished):
                self.at_finish = list(self.handle.calls)

        FakeHandle.close_fail = close_fail
        self.addCleanup(setattr, FakeHandle, "close_fail", None)
        self.fresh()
        return self.faked(*events, environment=environment, on_event=on, **kw)

    def test_every_run_end_closes_before_finished(self):
        for name, events in (
            ("outcome", (turn(START, block(DONE)), Exited(0))),
            ("no outcome", (turn(START), Exited(0))),
            ("failed turn", (turn(START), TurnFailed("overloaded"), Exited(1))),
            ("usage limit", (turn(START), LimitHit(None), Exited(0))),
        ):
            with self.subTest(name):
                self.locked(*events)
                self.assertEqual(self.handle.calls.count(("close",)), 1, self.handle.calls)
                self.assertIn(("close",), self.at_finish)
                self.assertIn(f"session {NAME} closed (lock dummy-lock)", self.err)
                self.assertEqual(self.kinds()[-3:], ["release", "result", "end"])
                self.assertNotIn("left open", self.err)

    def test_a_raised_error_closes_after_stopping(self):
        with self.assertRaises(AssertionError):
            self.locked(turn(START))
        self.assertEqual(self.handle.calls[-2:], [("stop",), ("close",)])

    def test_a_failed_close_is_printed_not_raised(self):
        r = self.locked(turn(START, block(DONE)), Exited(0), close_fail=SessionError("gone"))
        self.assertEqual(r.outcome.status, "done")
        self.assertIn("gone", self.err)
        self.assertNotIn("closed (lock", self.err)

    def test_without_a_lock_the_session_stays_open(self):
        unlocked = EnvironmentSpec(image="dummy-env:test", gateway="workers")
        for environment in (None, unlocked):
            with self.subTest(environment=environment):
                self.locked(turn(START, block(DONE)), Exited(0), environment=environment)
                self.assertNotIn(("close",), self.handle.calls)
                self.assertNotIn("closed (lock", self.err)
                self.assertNotIn("release", self.kinds())


class InstructionRuns(InstructionCase):
    def test_each_role_gets_its_planned_prompt_and_finishes_without_an_outcome(self):
        for role in ROLES:
            with self.subTest(role=role):
                args = self.given(role)
                want, events = plan(*args, driver=self.driver, composition=Composer()).prompt, []
                with no_spawn(), redirect_stderr(io.StringIO()):
                    r = executed(*args, on_event=events.append, driver=self.driver)
                self.assertEqual((r.returncode, r.outcome, r.instruction), (0, None, args[1].out))
                with open(args[1].out) as f:
                    self.assertEqual(f.read(), want)
                self.assertEqual(marks(want), [("round", "a mark line in the input")])
                self.assertEqual(events, [Finished(r)])
                self.assertFalse(os.path.exists(os.path.join(args[1].workdir, "run.jsonl")))

    def test_an_unconfigured_client_does_not_start_a_session(self):
        args = self.given("dummy-tester", client="unconfigured")
        with no_spawn():
            result = executed(*args, driver=self.driver)
        self.assertEqual(
            (result.returncode, result.outcome, result.instruction), (0, None, args[1].out)
        )

    def test_a_resume_is_refused(self):
        args = self.given("dummy-tester", resume=True)
        with (
            no_spawn(),
            self.assertRaisesRegex(ConfigError, "instruction has no session to resume"),
        ):
            executed(*args, driver=self.driver)


class Say(Case):
    def test_a_dead_stderr_drops_the_text_and_any_other_write_error_raises(self):
        for error, dead in (
            (OSError(errno.EIO, "Input/output error"), True),
            (BrokenPipeError(errno.EPIPE, "Broken pipe"), True),
            (OSError(errno.ENOSPC, "No space left on device"), False),
        ):
            with self.subTest(error=error):
                stream = mock.Mock()
                stream.write.side_effect = error
                stream.fileno.side_effect = io.UnsupportedOperation("fileno")
                with mock.patch("sys.stderr", stream):
                    if dead:
                        say("delegate: gone")
                    else:
                        with self.assertRaises(OSError) as raised:
                            say("delegate: gone")
                        self.assertIs(raised.exception, error)
                stream.write.assert_called_once_with("delegate: gone\n")

    def test_a_dead_stderr_goes_to_devnull_so_the_exit_flush_keeps_the_exit_status(self):
        """CPython exits 120 when that flush fails."""
        read, write = os.pipe()
        os.close(read)
        stream = io.TextIOWrapper(io.BufferedWriter(io.FileIO(write, "w")))
        self.addCleanup(stream.close)
        with mock.patch("sys.stderr", stream):
            say("delegate: a")
            say("delegate: b")
        stream.flush()
        self.assertTrue(os.path.samestat(os.fstat(write), os.stat(os.devnull)))


class Report(Case):
    def test_report_exits_75_only_for_busy_text(self):
        busy = "busy: lock dummy-lock is held by ensemble-x-1 (owner x)"
        for result, code in (
            (Result(75, None, busy), 75),
            (Result(1, None, busy), 75),
            (Result(1, None, f"tui: {busy}"), 1),
            (Result(1, None, "tui: broker failed"), 1),
            (Result(75, None, "the client exited 75"), 1),
            (Result(0, Outcome("done", "Title", "Summary")), 0),
        ):
            with self.subTest(result=result), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(report(result, None)[0], code)
            if result.outcome is None:
                self.assertEqual(err.getvalue(), f"delegate: {result.error}\n")


class Stopping(Case):
    def test_a_raised_run_ends_with_the_runners_code_and_a_one_line_stopped_error(self):
        for error, want in (
            (Stopped(143), Result(143, None, "stopped: Stopped: 143")),
            (SystemExit(3), Result(3, None, "stopped: SystemExit: 3")),
            (SystemExit("bye"), Result(1, None, "stopped: SystemExit: bye")),
            (RuntimeError("a\n  b"), Result(1, None, "stopped: RuntimeError: a b")),
            (KeyboardInterrupt(), Result(1, None, "stopped: KeyboardInterrupt")),
        ):
            with self.subTest(error=error):
                self.assertEqual(stopped(error), want)


class AttemptCore(Handles):
    """Attempt, the event-driven core, under the tui policy as the delegation handler feeds it:
    each event fed, a Result ends it; closing's fields.
    """

    def attempt(self, *, first=True, sent=None) -> Attempt:
        accepted = self.planned(mode="tui")
        self.handle = FakeHandle([], fail=sent)
        self.events, self.notes = [], []
        return Attempt(
            accepted,
            POLICIES[Mode.TUI](self.note, None),
            self.handle,
            note=self.note,
            on_event=self.events.append,
            first=first,
        )

    def note(self, kind: str, **_fields) -> None:
        self.notes.append(kind)

    def fed(self, attempt: Attempt, *events) -> list[Result | None]:
        err = io.StringIO()
        with redirect_stderr(err):
            found = [attempt.feed(event) for event in events]
        self.err = err.getvalue()
        return found

    def test_an_outcome_turn_end_is_the_attempts_result(self):
        attempt = self.attempt()
        found = self.fed(attempt, Activity("Read"), turn(START, block(DONE)))
        self.assertIsNone(found[0])
        self.assertEqual(found[1].outcome.status, "done")
        self.assertEqual(self.events, [Progress("start", "go")])
        self.assertNotIn("missing", self.notes)

    def test_a_valid_outcome_at_a_turn_end_is_its_result_even_with_work_pending(self):
        for pending in (0, 2):
            with self.subTest(pending=pending):
                found = self.fed(self.attempt(), turn(START, block(DONE), pending=pending))
                self.assertEqual(
                    (found[0].returncode, found[0].outcome.status, self.handle.calls),
                    (0, "done", []),
                )

    def test_an_invalid_outcome_is_fixed_by_one_send_that_is_neither_the_nudge_nor_counted(self):
        fix = ("send", FIX.format(error=INVALID_ERROR))
        for later, sends in (([], [fix]), ([STOP] * STOP_LIMIT, [fix, ("send", NUDGE)])):
            with self.subTest(later=len(later)):
                attempt = self.attempt()
                found = self.fed(attempt, turn(START, block(INVALID)), *later, turn(block(DONE)))
                self.assertEqual(found[:-1], [None] * (1 + len(later)))
                self.assertEqual((found[-1].outcome.status, self.handle.calls), ("done", sends))
                self.assertIn("invalid", self.notes)
                self.assertIn(
                    f"delegate: invalid outcome: {INVALID_ERROR}; resuming once\n", self.err
                )

    def test_an_invalid_outcome_twice_or_a_failed_fix_send_is_the_error(self):
        found = self.fed(self.attempt(), turn(START, block(INVALID)), turn(block(INVALID)))
        self.assertEqual(found[-1], Result(0, None, f"invalid outcome: {INVALID_ERROR}"))
        self.assertEqual(self.handle.calls, [("send", FIX.format(error=INVALID_ERROR))])
        found = self.fed(self.attempt(sent=SessionError("gone")), turn(START, block(INVALID)))
        self.assertEqual(
            found, [Result(1, None, f"invalid outcome: {INVALID_ERROR}; resume failed: gone")]
        )

    def test_a_failed_turn_and_a_limit_end_it(self):
        found = self.fed(self.attempt(), TurnFailed("server_overloaded", "busy"))
        self.assertEqual(
            found, [Result(0, None, "the client's turn failed: server_overloaded (busy)")]
        )
        attempt = self.attempt()
        found = self.fed(attempt, LimitHit(None))
        self.assertEqual(found, [Result(0, None, "usage limit; reset time unknown", Limit(None))])
        self.assertEqual(self.handle.calls, [])

    def test_turn_ends_without_an_outcome_are_nudged_then_given_up(self):
        attempt = self.attempt()
        found = self.fed(attempt, *[turn("Working.")] * (STOP_LIMIT + 1))
        self.assertEqual(found[:-1], [None] * STOP_LIMIT)
        self.assertEqual(found[-1], GAVE_UP)
        self.assertEqual(self.handle.calls, [("send", NUDGE)])
        self.assertEqual(self.notes, ["nudge", "give_up"])
        self.assertIn(f"delegate: no outcome after {STOP_LIMIT} turn ends; {LEFT_OPEN}", self.err)

    def test_a_mark_resets_the_count_before_its_turn_end_counts_and_never_the_nudge(self):
        mark = turn("[ensemble-progress:round] half way")
        for name, steps in (("once", [mark]), ("repeated on the next turn", [mark, mark])):
            with self.subTest(name):
                found = self.fed(
                    self.attempt(),
                    turn(START),
                    *[STOP] * (STOP_LIMIT - 1),
                    *steps,
                    *[STOP] * (STOP_LIMIT - 2),
                    turn(block(DONE)),
                )
                self.assertEqual(
                    (found[-1].outcome.status, self.handle.calls), ("done", [("send", NUDGE)])
                )
                self.assertEqual(self.err.count("Progress (round): half way"), len(steps))

    def test_turn_ends_with_work_pending_neither_nudge_nor_count_however_many(self):
        found = self.fed(self.attempt(), *[PENDING] * (2 + STOP_LIMIT), turn(START, block(DONE)))
        self.assertEqual((found[-1].outcome.status, self.handle.calls), ("done", []))
        found = self.fed(self.attempt(), PENDING, PENDING, STOP, *[PENDING, STOP] * STOP_LIMIT)
        self.assertEqual((found[-1], self.handle.calls), (GAVE_UP, [("send", NUDGE)]))

    def test_a_failed_nudge_is_printed_and_counts_as_the_nudge(self):
        found = self.fed(self.attempt(sent=SessionError("no pane")), *[STOP] * (1 + STOP_LIMIT))
        self.assertEqual((found[-1], self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn("delegate: tui: no pane\n", self.err)
        self.assertIn("nudge", self.notes)

    def test_idle_gives_a_tui_attempt_up_its_reason_in_seconds_minutes_or_hours(self):
        for seconds, shown in ((30, "30 s"), (90, "1.5 min"), (1800, "30 min"), (5400, "1.5 h")):
            with self.subTest(seconds=seconds):
                attempt = self.attempt()
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(attempt.idle(seconds), GAVE_UP)
                self.assertIn(f"delegate: no event for {shown}; {LEFT_OPEN}", err.getvalue())
                self.assertEqual(self.notes, ["idle", "give_up"])

    def test_only_a_first_attempt_logs_a_missing_start_mark(self):
        for first, want in ((True, ["missing"]), (False, [])):
            with self.subTest(first=first):
                self.fresh()
                attempt = self.attempt(first=first)
                self.fed(attempt, turn(block(DONE)))
                self.assertEqual([n for n in self.notes if n == "missing"], want)

    def test_closing_holds_the_result_note_fields(self):
        outcome = Outcome("done", "T", "S", deliverable="# Doc\n")
        self.assertEqual(closing(Result(1, None, "boom")), {"error": "boom"})
        self.assertEqual(
            closing(Result(0, outcome, "")),
            {"outcome": {k: v for k, v in asdict(outcome).items() if k != "deliverable"}},
        )
        self.assertEqual(
            closing(Result(2, outcome, "the client exited 2"))["error"], "the client exited 2"
        )
