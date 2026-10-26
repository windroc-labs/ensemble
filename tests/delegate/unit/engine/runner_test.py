"""Pure logic over fake objects and in-process clients."""

import errno
import io
import json
import os
import signal
import sys
from contextlib import redirect_stderr
from datetime import datetime
from unittest import mock

from ensemble.delegate import ConfigError, Progress, Spec
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.plan import plan
from ensemble.delegate.engine.runner import FIX, NUDGE, STOP_LIMIT, execute, say, stopped
from ensemble.delegate.engine.types import Begun, Finished, Layout, Limit, Result
from ensemble.delegate.event.reply import marks
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import (
    EnvironmentSpec,
    Exited,
    IdleTimeout,
    LimitHit,
    Reply,
    SessionError,
    TurnFailed,
)
from ensemble.session.engine.types import Activity, Blocked
from ensemble.utils import Stopped
from tests.delegate.case import Case
from tests.delegate.replies import bare, block
from tests.delegate.repo import ok
from tests.delegate.runner import (
    INVALID,
    INVALID_ERROR,
    LEFT_OPEN,
    PENDING,
    PR,
    SID,
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
    no_child,
    said,
    turn,
)


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
        with no_child():
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


class TuiPolicy(Base):
    """The tui policy over a FakeHandle (start patched as delegate.engine.runner sees it)."""

    def setUp(self):
        super().setUp()
        self.configure_fake(tui=True)
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("TUI_ATTACH_PREFIX", None)

    def faked(
        self, *events, fail=None, stop_fail=None, show_fail=None, hidden=False, mode="tui", **kw
    ) -> Result:
        self.handle = FakeHandle(events, fail, stop_fail)
        self.handle.show_fail, self.handle.hidden = show_fail, hidden
        with mock.patch.object(self.driver, "start", side_effect=self.handle.start) as self.started:
            return self.go(mode=mode, **kw)

    def test_a_named_headless_session_reaches_the_driver_with_its_name(self):
        for name in (None, "dummy-tester-0f8fad5b"):
            with self.subTest(name=name):
                self.fresh()
                self.faked(turn(START, block(DONE)), Exited(0), mode="headless", session_name=name)
                session = self.started.call_args.args[0].session
                self.assertEqual(
                    (session.mode, session.name, session.events), ("headless", name, None)
                )

    def test_a_tui_handle_without_a_name_reports_a_driver_error(self):
        os.makedirs(self.work)
        handle = FakeHandle([])
        handle.name = None
        with mock.patch.object(self.driver, "start", return_value=handle):
            result = self.go(mode="tui")
        self.assertEqual((result.returncode, result.error), (1, "tui: tui session has no name"))

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
                r = self.faked(turn(START, block(DONE)), **kw)
                args, given = self.started.call_args
                self.assertEqual(
                    ((args[0].session.client, args[0].session.mode), r.outcome.status),
                    (("fake", "tui"), "done"),
                )
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
                r = self.faked(turn(START, block(DONE)))
                self.assertEqual((r.outcome.status, self.handle.shown), ("done", shown))
                self.assertIn(f"delegate: tui session {NAME}: tmux attach -t '={NAME}'\n", self.err)

    def test_a_hidden_session_is_not_shown_at_start(self):
        for local in ("", '[delegate]\nshow = "none"\n', '[delegate]\nshow = "open {{session}}"\n'):
            with self.subTest(local=local):
                self.fresh()
                self.write(os.path.join(self.ensemble, "config.local.toml"), local)
                self.driver.display = "mgr"
                r = self.faked(turn(START, block(DONE)), hidden=True)
                self.assertEqual((r.outcome.status, self.handle.shown), ("done", []))
                self.assertIn(f"delegate: tui session {NAME}: tmux attach -t '={NAME}'\n", self.err)

    def test_a_session_started_without_a_display_is_not_shown(self):
        r = self.faked(turn(START, block(DONE)))
        self.assertEqual((r.outcome.status, self.handle.shown), ("done", []))
        self.assertIn(f"delegate: tui session {NAME}: tmux attach -t '={NAME}'\n", self.err)

    def test_a_failed_show_is_printed_and_the_run_goes_on(self):
        for failure in (SessionError(f"no tmux session {NAME}"), SessionConfigError("bad split")):
            with self.subTest(failure=failure):
                self.fresh()
                self.driver.display = "mgr"
                r = self.faked(turn(START, block(DONE)), show_fail=failure)
                self.assertEqual(r.outcome.status, "done")
                self.assertIn(f"delegate: show {NAME}: {failure}\n", self.err)

    def test_a_valid_outcome_at_a_turn_end_is_done_and_leaves_the_session_even_with_work_pending(
        self,
    ):
        for pending in (0, 2):
            with self.subTest(pending=pending):
                self.fresh()
                r = self.faked(turn(START, block(DONE), pending=pending))
                self.assertEqual(
                    (r.returncode, r.outcome.status, r.error, self.handle.calls),
                    (0, "done", "", []),
                )
                self.assertEqual(
                    self.events, [Begun(self.events[0].sid), Progress("start", "go"), Finished(r)]
                )
                self.assertEqual((self.read(self.out), self.last("end")["rc"]), ("# Doc\n", 0))
                self.assertEqual(self.kinds(), ["input", "progress", "result", "end"])

    def test_the_first_turn_end_without_an_outcome_nudges_once(self):
        r = self.faked(turn(START), STOP, turn(block(DONE)))
        nudge = (
            "Finish your task, then end your last reply with your outcome block "
            "(`ensemble-outcome`), as your instructions' Return says. If you are waiting for "
            "background work, wait for it first."
        )
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", [("send", nudge)]))
        self.assertEqual(self.kinds(), ["input", "progress", "nudge", "result", "end"])
        self.assertEqual(bare(self.last("nudge")), {"kind": "nudge"})

    def test_intermediate_reply_progress_is_recorded_and_final_reply_is_not_repeated(self):
        final = "[ensemble-progress:step] finished\n" + block(DONE)
        result = self.faked(Reply(START), Reply("intermediate prose"), Reply(final), turn(final))
        self.assertEqual(result.outcome.status, "done")
        self.assertEqual(
            [event for event in self.events if isinstance(event, Progress)],
            [Progress("start", "go"), Progress("step", "finished")],
        )
        self.assertEqual(self.kinds().count("progress"), 2)
        self.assertEqual(self.err.count("intermediate prose"), 1)
        self.assertEqual(self.err.count("Progress (step): finished"), 1)
        self.assertNotIn("missing progress mark", self.err)

    def test_it_gives_up_after_stop_limit_counted_turn_ends_and_leaves_the_session(self):
        r = self.faked(turn(START), *[STOP] * STOP_LIMIT)
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn(f"delegate: no outcome after 3 turn ends; {LEFT_OPEN}", self.err)
        self.assertEqual(self.kinds(), ["input", "progress", "nudge", "give_up", "result", "end"])
        self.assertEqual(
            bare(self.last("give_up")),
            {"kind": "give_up", "reason": "no outcome after 3 turn ends"},
        )
        self.assertEqual(
            (self.last("end")["rc"], [type(e) for e in self.events]),
            (0, [Begun, Progress, Finished]),
        )

    def test_a_mark_resets_the_count_before_its_turn_end_counts_and_never_the_nudge(self):
        mark = turn("[ensemble-progress:round] half way")
        for name, steps in (("once", [mark]), ("repeated on the next turn", [mark, mark])):
            with self.subTest(name):
                self.fresh()
                r = self.faked(
                    turn(START),
                    *[STOP] * (STOP_LIMIT - 1),
                    *steps,
                    *[STOP] * (STOP_LIMIT - 2),
                    turn(block(DONE)),
                )
                self.assertEqual((r.outcome.status, self.handle.calls), ("done", [("send", NUDGE)]))
                self.assertEqual(self.err.count("Progress (round): half way"), len(steps))

    def test_turn_ends_with_work_pending_neither_nudge_nor_count_however_many(self):
        r = self.faked(*[PENDING] * (2 + STOP_LIMIT), turn(START, block(DONE)))
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", []))
        r = self.faked(PENDING, PENDING, STOP, *[PENDING, STOP] * STOP_LIMIT)
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))

    def test_an_invalid_block_is_fixed_by_one_send_that_is_neither_the_nudge_nor_counted(self):
        fix = ("send", FIX.format(error=INVALID_ERROR))
        for later, sends in (([], [fix]), ([STOP] * STOP_LIMIT, [fix, ("send", NUDGE)])):
            with self.subTest(later=len(later)):
                self.fresh()
                r = self.faked(turn(START, block(INVALID)), *later, turn(block(DONE)))
                self.assertEqual(
                    (r.returncode, r.outcome.status, self.handle.calls), (0, "done", sends)
                )
                self.assertEqual(
                    (self.started.call_count, self.kinds()[:3]),
                    (1, ["input", "progress", "invalid"]),
                )
                self.assertIn(
                    f"delegate: invalid outcome: {INVALID_ERROR}; resuming once\n", self.err
                )

    def test_an_invalid_block_twice_or_a_failed_fix_send_is_the_error(self):
        r = self.faked(turn(START, block(INVALID)), turn(block(INVALID)))
        self.assertEqual(r, Result(0, None, f"invalid outcome: {INVALID_ERROR}"))
        self.assertEqual(self.handle.calls, [("send", FIX.format(error=INVALID_ERROR))])
        r = self.faked(turn(START, block(INVALID)), fail=SessionError("gone"))
        self.assertEqual(
            r, Result(1, None, f"invalid outcome: {INVALID_ERROR}; resume failed: gone")
        )

    def test_an_idle_timeout_gives_up_and_leaves_the_session(self):
        r = self.faked(turn(START), IdleTimeout(7200))
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn(f"delegate: no event for 2 h; {LEFT_OPEN}", self.err)
        self.assertEqual(
            self.kinds(), ["input", "progress", "nudge", "idle", "give_up", "result", "end"]
        )
        self.assertEqual(
            (self.last("idle")["seconds"], self.last("give_up")["reason"]),
            (7200, "no event for 2 h"),
        )

    def test_the_give_up_reason_shows_the_timeout_in_seconds_minutes_or_hours(self):
        for seconds, shown in ((30, "30 s"), (90, "1.5 min"), (600, "10 min"), (5400, "1.5 h")):
            with self.subTest(seconds=seconds):
                self.fresh()
                self.faked(turn(START), IdleTimeout(seconds))
                self.assertEqual(self.last("give_up")["reason"], f"no event for {shown}")

    def test_hours_of_activity_or_blocked_without_an_idle_timeout_never_give_up(self):
        now = [0.0]

        def hours_apart(*events):
            for event in events:
                now[0] += 3 * 60 * 60
                yield event

        handle = FakeHandle(
            hours_apart(
                Activity("Bash"),
                Blocked("permission"),
                Activity(""),
                Blocked("question"),
                turn(START, block(DONE)),
            )
        )
        with (
            mock.patch("time.monotonic", lambda: now[0]),
            mock.patch.object(self.driver, "start", side_effect=handle.start),
        ):
            r = self.go(mode="tui")
        self.assertEqual((r.outcome.status, handle.calls, now[0]), ("done", [], 15 * 60 * 60))
        self.assertEqual(self.kinds(), ["input", "progress", "result", "end"])

    def test_a_failed_nudge_is_printed_and_counts_as_the_nudge(self):
        r = self.faked(*[STOP] * (1 + STOP_LIMIT), fail=SessionError("no pane"))
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn("delegate: tui: no pane\n", self.err)
        self.assertIn("nudge", self.kinds())

    def test_a_usage_limit_stops_the_session_without_a_nudge(self):
        at = 1767225600.0
        r = self.faked(LimitHit(at), STOP, Exited(-signal.SIGHUP))
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        self.assertEqual(
            r, Result(-signal.SIGHUP, None, f"usage limit; resets at {when}", Limit(at))
        )
        self.assertEqual(
            (self.handle.calls, self.kinds()), ([("stop",)], ["input", "limit", "result", "end"])
        )

    def test_a_failed_turn_ends_the_run_without_a_nudge_and_leaves_the_session(self):
        for failed, error in (
            (TurnFailed("overloaded", "529"), "the client's turn failed: overloaded (529)"),
            (
                TurnFailed("authentication_failed"),
                "the client's turn failed: authentication_failed",
            ),
            (TurnFailed("billing_error", "low"), "the client's turn failed: billing_error (low)"),
        ):
            with self.subTest(failed=failed):
                self.fresh()
                r = self.faked(Reply(START), failed, STOP)
                self.assertEqual((r, self.handle.calls), (Result(0, None, error), []))
                self.assertEqual(
                    self.kinds(), ["input", "progress", "turn_failed", "result", "end"]
                )
                self.assertEqual(
                    bare(self.last("turn_failed")),
                    {"kind": "turn_failed", "error": failed.error, "detail": failed.detail},
                )
                self.assertEqual([type(e) for e in self.events], [Begun, Progress, Finished])

    def test_a_failed_headless_turn_stops_its_session(self):
        r = self.faked(Reply(START), TurnFailed("overloaded"), STOP, mode="headless")
        self.assertEqual(
            (r.error, self.handle.calls), ("the client's turn failed: overloaded", [("stop",)])
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
                    (self.handle.calls, self.err.count("delegate: tui: gone\n")), ([("stop",)], 1)
                )
                self.assertTrue(
                    self.last("result")["error"].startswith(f"stopped: {type(error).__name__}")
                )

    def killed(self, **kw):
        """A tui run whose harness pane is killed at its first progress mark: stderr hung up,
        then SIGHUP's Stopped(129); the error it raised.
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

    def test_a_killed_harness_with_its_stderr_gone_stops_the_tui_and_closes_the_run(self):
        for stop_fail in (None, SessionError("tmux sessions unknown: tmux failed")):
            with self.subTest(stop_fail=stop_fail):
                self.fresh()
                error = self.killed(stop_fail=stop_fail)
                self.assertEqual((type(error), error.code), (Stopped, 129))
                self.assertEqual(self.handle.calls, [("stop",)])
                self.assert_closed_as_stopped()

    def test_a_killed_harness_whose_own_stop_fails_has_its_tui_stopped_by_the_retry(self):
        def hangup():
            yield turn(START)
            sys.stderr = DeadStream()
            yield Reply("a line for the hung-up terminal")
            try:  # the session's own stop on the signal, as HostedSession.events() does
                self.handle.stop()
            except SessionError:
                self.handle.stop_fail = None
            raise Stopped(128 + signal.SIGHUP)

        self.handle = FakeHandle(
            hangup(), stop_fail=SessionError("tmux sessions unknown: tmux failed")
        )
        with (
            mock.patch.object(self.driver, "start", side_effect=self.handle.start),
            self.assertRaises(Stopped) as raised,
        ):
            self.go(mode="tui")
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
        self.assertIn("delegate: tui: stop failed: RuntimeError: stop crashed\n", self.err)

    def test_a_failed_stop_is_printed_and_never_replaces_the_limit_or_stale_out_result(self):
        gone = SessionError("gone")
        r = self.faked(LimitHit(None), stop_fail=gone)
        self.assertEqual(r, Result(0, None, "usage limit; reset time unknown", Limit(None)))
        self.assertEqual(
            (self.handle.calls, self.err.count("delegate: tui: gone\n")), ([("stop",)], 1)
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
        self.assertEqual(self.err.count("delegate: tui: gone\n"), 1)

    def test_a_driver_failure_at_start_is_the_result_and_a_client_without_tui_a_config_error(self):
        os.makedirs(self.work)
        with mock.patch.object(self.driver, "start", side_effect=SessionError("no tmux")):
            self.assertEqual(self.go(mode="tui"), Result(1, None, "tui: no tmux"))
        self.assertEqual(self.kinds(), ["input", "result", "end"])
        self.fresh()
        self.configure_fake()
        with self.assertRaises(ConfigError) as cm:
            self.go(mode="tui")
        self.assertEqual(str(cm.exception), "client 'fake' does not support tui mode")

    def test_the_runner_records_into_an_existing_run_dir_only(self):
        composition = Composer()
        accepted = plan(
            composition.resolve("dummy-tester"),
            Spec("Echo me.", self.work, task="echo", client="fake", mode="tui"),
            driver=self.driver,
            composition=composition,
        )
        with (
            mock.patch.object(self.driver, "start", side_effect=SessionError("no tmux")),
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
            mode="headless",
        )
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", []))
        self.assertEqual(self.kinds(), ["input", "idle", "progress", "result", "end"])
        args, given = self.started.call_args
        self.assertEqual(
            (given, args[0].session.sid, args[0].session.resume, args[0].session.events),
            ({"idle_timeout": 7200}, self.events[0].sid, False, None),
        )


LOCKED = EnvironmentSpec(image="dummy-env:test", gateway="workers", lock="dummy-lock")


class LockCloseTest(TuiPolicy):
    """A locked run's session closes as soon as the run ends, before its Finished (FR-61)."""

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
        for name, events, mode in (
            ("outcome headless", (turn(START, block(DONE)), Exited(0)), "headless"),
            ("outcome tui", (turn(START, block(DONE)),), "tui"),
            ("no outcome", (turn(START), Exited(0)), "headless"),
            ("failed turn", (turn(START), TurnFailed("overloaded")), "tui"),
            ("give-up", (turn(START), *[STOP] * STOP_LIMIT), "tui"),
            ("idle", (turn(START), IdleTimeout(7200)), "tui"),
            ("usage limit", (turn(START), LimitHit(None), Exited(0)), "headless"),
        ):
            with self.subTest(name):
                self.locked(*events, mode=mode)
                self.assertEqual(self.handle.calls.count(("close",)), 1, self.handle.calls)
                self.assertIn(("close",), self.at_finish)
                self.assertIn(f"session {NAME} closed (lock dummy-lock)", self.err)
                self.assertNotIn("left open", self.err)

    def test_a_raised_error_closes_after_stopping(self):
        with self.assertRaises(AssertionError):
            self.locked(turn(START), mode="headless")
        self.assertEqual(self.handle.calls[-2:], [("stop",), ("close",)])

    def test_a_failed_close_is_printed_not_raised(self):
        r = self.locked(
            turn(START, block(DONE)), Exited(0), mode="headless", close_fail=SessionError("gone")
        )
        self.assertEqual(r.outcome.status, "done")
        self.assertIn("gone", self.err)
        self.assertNotIn("closed (lock", self.err)

    def test_without_a_lock_the_session_stays_open(self):
        unlocked = EnvironmentSpec(image="dummy-env:test", gateway="workers")
        for environment in (None, unlocked):
            with self.subTest(environment=environment):
                self.locked(turn(START), *[STOP] * STOP_LIMIT, environment=environment)
                self.assertNotIn(("close",), self.handle.calls)
                self.assertIn(LEFT_OPEN, self.err)


class InstructionRuns(InstructionCase):
    def test_each_role_gets_its_planned_prompt_and_finishes_without_an_outcome(self):
        for role in ROLES:
            with self.subTest(role=role):
                args = self.given(role)
                want, events = plan(*args, driver=self.driver, composition=Composer()).prompt, []
                with no_child(), redirect_stderr(io.StringIO()):
                    r = executed(*args, on_event=events.append, driver=self.driver)
                self.assertEqual((r.returncode, r.outcome, r.instruction), (0, None, args[1].out))
                with open(args[1].out) as f:
                    self.assertEqual(f.read(), want)
                self.assertEqual(marks(want), [("round", "a mark line in the input")])
                self.assertEqual(events, [Finished(r)])
                self.assertFalse(os.path.exists(os.path.join(args[1].workdir, "run.jsonl")))

    def test_an_unconfigured_client_does_not_start_a_session(self):
        args = self.given("dummy-tester", client="unconfigured")
        with no_child():
            result = executed(*args, driver=self.driver)
        self.assertEqual(
            (result.returncode, result.outcome, result.instruction), (0, None, args[1].out)
        )

    def test_a_resume_is_refused(self):
        args = self.given("dummy-tester", resume=True)
        with (
            no_child(),
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
