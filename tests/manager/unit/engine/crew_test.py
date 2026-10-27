"""Crew.run: the starter command's final Record becomes the live entry; a failed start is lost.
Crew.attach shows each live entry but a hidden one, whose view column says so.
"""

import contextlib
import io
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.manager.engine.crew import GRACE, HEADER, START_TIMEOUT, Crew, _run_start
from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import RosterError, Spec, State
from ensemble.utils import Record, Status, save_reply
from tests.manager.roster_case import RosterCase
from tests.manager.starters import answered, finished
from tests.session.hosted_support import HostedTuiBase
from tests.utils.process_fixtures import DEVNULL, TimeoutExpired

ARGV = ("fake-ctl", "start", "x")


class EventReading(RosterCase):
    def test_malformed_complete_line_fails_without_consuming_the_following_event(self):
        path = Path(self.roster.events)
        for invalid in ("", "diagnostic", "12:00 worker done", "not an event\r"):
            with self.subTest(invalid=invalid):
                path.write_text(f"12:00:00 worker done\n{invalid}\n12:00:01 worker blocked\n")
                with self.assertRaisesRegex(RosterError, f"{path}: invalid event at line 2"):
                    Crew(self.roster).next_event(after=1, gen=0)
                self.assertEqual(
                    self.roster.backlog(),
                    (0, 0, ["12:00:00 worker done", invalid, "12:00:01 worker blocked"]),
                )

    def test_malformed_line_at_eof_fails_instead_of_waiting_for_another_event(self):
        Path(self.roster.events).write_text("diagnostic\n")
        with (
            mock.patch("time.sleep", side_effect=TimeoutError("polled past malformed line")),
            self.assertRaisesRegex(RosterError, "invalid event at line 1"),
        ):
            Crew(self.roster).next_event(after=0, gen=0)

    def test_partial_tail_is_not_consumed_and_completion_is_returned(self):
        path = Path(self.roster.events)
        path.write_text("12:00:00 worker do")

        def complete(_delay):
            self.assertEqual(self.roster.backlog(), (0, 0, []))
            with path.open("a") as stream:
                stream.write("ne\n")

        with mock.patch("time.sleep", side_effect=complete) as waiting:
            self.assertEqual(
                Crew(self.roster).next_event(after=0, gen=0), (1, "12:00:00 worker done")
            )
        waiting.assert_called_once_with(0.5)
        self.assertEqual(self.roster.backlog()[0], 0)


class Run(RosterCase):
    def start(self, run, name="x"):
        stderr = io.StringIO()
        with (
            mock.patch("subprocess.Popen", side_effect=answered(run)) as called,
            contextlib.redirect_stderr(stderr),
        ):
            try:
                return Crew(self.roster).run(Spec(name, ARGV)), called, stderr.getvalue()
            except RuntimeError as error:
                return error, called, stderr.getvalue()

    def test_the_final_record_on_stdout_becomes_the_live_entry(self):
        def run(_argv, **options):
            self.assertEqual(options["timeout"], START_TIMEOUT)
            record = replace(self.record, sid=options["env"]["ENSEMBLE_SID"])
            os.write(options["stdout"].fileno(), f"progress\n{record.to_line()}\n".encode())
            return mock.Mock(returncode=0, stderr="warning\n")

        record, called, stderr = self.start(run)
        entry = self.roster.entries()["x"]
        self.assertEqual((entry.state, entry.record), (State.LIVE, record))
        self.assertEqual(record, replace(self.record, sid=entry.sid))
        self.assertEqual(called.call_args.args, (ARGV,))
        env = called.call_args.kwargs["env"]
        self.assertEqual(env["ENSEMBLE_EVENTS"], self.roster.events)
        self.assertEqual(env["ENSEMBLE_SID"], entry.sid)
        self.assertIs(called.call_args.kwargs["stdin"], DEVNULL)
        self.assertEqual(stderr, "warning\n")

    def test_a_failed_start_raises_and_marks_the_entry_lost(self):
        for name, effect, message in (
            ("stderr", mock.Mock(returncode=2, stderr=" boom \n"), "boom"),
            ("exit", mock.Mock(returncode=2, stderr=""), "exit 2"),
            ("missing", FileNotFoundError(2, "No such file"), "[Errno 2] No such file"),
            ("timeout", TimeoutExpired(ARGV, 30), "no response within 1920 seconds"),
        ):
            with self.subTest(name=name):
                error, _, _ = self.start(mock.Mock(side_effect=[effect]), name)
                self.assertEqual(str(error), f"fake-ctl start x: {message}")
                self.assertIs(self.roster.entries()[name].state, State.LOST)


class RunStart(RosterCase):
    """_run_start: subprocess.run whose deadline sends SIGTERM, then SIGKILL GRACE s later."""

    def run_start(self, *answers):
        proc = finished(3)
        proc.communicate.side_effect = answers
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            try:
                return _run_start(ARGV, timeout=7, stderr=-1), proc, popen
            except TimeoutExpired as error:
                return error, proc, popen

    def test_its_result_is_the_exit_code_and_stderr(self):
        result, proc, popen = self.run_start((None, "warn\n"))
        self.assertEqual((result.returncode, result.stderr), (3, "warn\n"))
        self.assertEqual(popen.call_args, mock.call(ARGV, stderr=-1))
        proc.communicate.assert_called_once_with(timeout=7)
        proc.terminate.assert_not_called()

    def test_past_the_deadline_it_terminates_and_kills_only_after_the_grace(self):
        for answers, killed in (
            ((TimeoutExpired(ARGV, 7), (None, "")), False),
            ((TimeoutExpired(ARGV, 7), TimeoutExpired(ARGV, GRACE)), True),
        ):
            with self.subTest(killed=killed):
                error, proc, _ = self.run_start(*answers)
                self.assertIsInstance(error, TimeoutExpired)
                proc.terminate.assert_called_once_with()
                self.assertEqual(
                    proc.communicate.call_args_list,
                    [mock.call(timeout=7), mock.call(timeout=GRACE)],
                )
                self.assertEqual(proc.kill.called, killed)


class ResumeInput(RosterCase):
    def setUp(self):
        super().setUp()
        self.live()
        self.roster.mark("x", State.WAITING)

    def resume(self, *, code=0, output=None, input="answer", during=None):
        def run(argv, **options):
            self.assertEqual(argv, (*self.record.resume, "--input", input))
            self.assertEqual(options["env"]["ENSEMBLE_SID"], self.record.sid)
            self.assertEqual(options["env"]["ENSEMBLE_EVENTS"], self.roster.events)
            self.assertIs(self.roster.entries()["x"].state, State.RESUMING)
            if during is not None:
                during()
            text = self.record.to_line() + "\n" if output is None else output
            os.write(options["stdout"].fileno(), text.encode())
            return mock.Mock(returncode=code, stderr="refused" if code else "")

        with mock.patch("subprocess.Popen", side_effect=answered(run)):
            return Crew(self.roster).resume("x", input=input)

    def test_input_and_empty_input_resume_the_same_identity(self):
        for text in ("answer", ""):
            with self.subTest(text=text):
                self.assertEqual(self.resume(input=text), self.record)
                self.assertIs(self.roster.entries()["x"].state, State.LIVE)
                self.roster.mark("x", State.WAITING)

    def test_only_an_empty_exit_two_restores_waiting(self):
        with self.assertRaisesRegex(ValueError, "refused"):
            self.resume(code=2, output="")
        self.assertIs(self.roster.entries()["x"].state, State.WAITING)
        for code, output in ((1, ""), (2, "nonempty"), (0, "not a Record")):
            with self.subTest(code=code, output=output):
                with self.assertRaises((RosterError, RuntimeError)):
                    self.resume(code=code, output=output)
                self.assertIs(self.roster.entries()["x"].state, State.GONE)
                self.roster.mark("x", State.WAITING)

    def test_cached_adoption_cannot_promote_the_current_input_attempt(self):
        def adopt():
            def query(argv, **options):
                if tuple(argv) == ("ensemble-session", "ls", "--here", "--records"):
                    stdout = self.record.to_line() + "\n"
                else:
                    self.assertEqual(tuple(argv), (*self.record.ctl, "status"))
                    self.assertEqual(options["input"], self.record.to_line() + "\n")
                    stdout = (
                        Status(
                            name=self.record.name,
                            alive=False,
                            state="gone",
                            outcome="needs_input",
                            attach=None,
                            hidden=False,
                        ).to_line()
                        + "\n"
                    )
                return mock.Mock(returncode=0, stdout=stdout, stderr="")

            with mock.patch("subprocess.run", side_effect=query):
                Crew(self.roster).attach()
            self.assertIs(self.roster.entries()["x"].state, State.RESUMING)

        with self.assertRaises(ValueError):
            self.resume(code=2, output="", during=adopt)
        self.assertIs(self.roster.entries()["x"].state, State.WAITING)

    def test_stale_claims_can_still_be_adopted(self):
        self.roster.claim_resume("x", state=State.WAITING)
        self.roster.adopt(self.record, resuming_before="9999-01-01T00:00:00+00:00")
        self.assertIs(self.roster.entries()["x"].state, State.LIVE)

    def test_waiting_without_input_is_rejected_without_a_controller_call(self):
        with mock.patch("subprocess.run") as run, self.assertRaisesRegex(RuntimeError, "waiting"):
            Crew(self.roster).resume("x")
        run.assert_not_called()

    def test_input_rejects_other_states_without_queries_or_changes(self):
        for state in (State.GONE, State.DONE, State.RESUMING):
            with self.subTest(state=state):
                self.roster.remove("x")
                self.live()
                self.roster.mark("x", State.GONE if state == State.RESUMING else state)
                if state == State.RESUMING:
                    self.roster.claim_resume("x")
                before = self.roster.entries()
                with mock.patch("subprocess.run") as run, self.assertRaises(ValueError):
                    Crew(self.roster).resume("x", input="answer")
                run.assert_not_called()
                self.assertEqual(self.roster.entries(), before)


class ResumeLive(RosterCase):
    """A live entry no attach has reconciled since its attempt ended: resume reads its Status
    first (FR-22: alive=false, outcome needs_input while its harness and tui live).
    """

    def setUp(self):
        super().setUp()
        self.live()

    def resume(self, *, alive: bool, outcome: str | None, input: str | None = "answer"):
        started = []

        def query(argv, **_options):
            self.assertEqual(tuple(argv), (*self.record.ctl, "status"))
            status = Status(
                name=self.record.name,
                alive=alive,
                state="working" if alive else "done",
                outcome=outcome,
                attach=None,
                hidden=False,
            )
            return mock.Mock(returncode=0, stdout=status.to_line() + "\n", stderr="")

        def run(argv, **options):
            started.append(tuple(argv))
            os.write(options["stdout"].fileno(), (self.record.to_line() + "\n").encode())
            return mock.Mock(returncode=0, stderr="")

        with (
            mock.patch("subprocess.run", side_effect=query),
            mock.patch("subprocess.Popen", side_effect=answered(run)),
        ):
            return Crew(self.roster).resume("x", input=input), started

    def test_input_answers_a_live_entry_whose_attempt_ended_needs_input(self):
        record, started = self.resume(alive=False, outcome="needs_input")
        self.assertEqual(record, self.record)
        self.assertEqual(started, [(*self.record.resume, "--input", "answer")])
        self.assertIs(self.roster.entries()["x"].state, State.LIVE)

    def test_input_still_refuses_a_live_entry_mid_attempt(self):
        before = self.roster.entries()
        with self.assertRaisesRegex(ValueError, r"--input requires waiting \(state live\)"):
            self.resume(alive=True, outcome=None)
        self.assertEqual(self.roster.entries(), before)

    def test_without_input_a_live_entry_whose_attempt_ended_needs_input_is_waiting(self):
        with self.assertRaisesRegex(RuntimeError, "waiting: give --input to answer"):
            self.resume(alive=False, outcome="needs_input", input=None)
        self.assertIs(self.roster.entries()["x"].state, State.WAITING)


class ReplyContent(RosterCase):
    ID = "0123456789abcdef0123456789abcdef"

    def content(self, event):
        with (
            mock.patch("subprocess.run", side_effect=AssertionError("no ctl call")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("no ctl call")),
        ):
            return Crew(self.roster).event_content(event)

    def test_reply_content_is_stored_answer_even_after_done(self):
        self.live()
        save_reply(self.roster.events_path, self.ID, "the answer\nin full")
        self.assertEqual(self.content(f"10:00:00 s1 reply {self.ID}"), "the answer\nin full\n")

    def test_reply_content_unavailable_missing_and_bad_id(self):
        self.live()
        self.assertEqual(
            self.content(f"10:00:00 s1 reply {self.ID}"),
            f"content unavailable: reply {self.ID}: missing\n",
        )
        for bad in ("x", self.ID + "/../x", "../" + self.ID, self.ID.upper()):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.content(f"10:00:00 s1 reply {bad}"),
                    f"content unavailable: reply {bad}: bad id\n",
                )
        self.assertEqual(self.content("10:00:00 s1 reply"), "content unavailable: reply : bad id\n")
        self.assertFalse(os.path.lexists(self.roster.events_path + ".replies"))

    def test_other_words_keep_ctl_content(self):
        self.live()
        with mock.patch(
            "subprocess.run", return_value=mock.Mock(returncode=0, stdout="screen", stderr="")
        ) as called:
            self.assertEqual(Crew(self.roster).event_content("10:00:00 s1 done"), "screen\n")
        self.assertEqual(called.call_args.args[0], ("ensemble-session", "content"))


class RecordlessStatus(RosterCase):
    def test_a_launch_without_a_record_is_starting_then_gone_once_lost(self):
        starting = self.roster.begin(Spec("s", ARGV), sid=str(uuid.uuid4()))
        lost = self.roster.begin(Spec("l", ARGV), sid=str(uuid.uuid4()))
        self.assertTrue(self.roster.mark_if("l", lost, State.LOST))
        self.assertIsNone(starting.record)
        self.assertEqual(
            Crew(self.roster).status(),
            [
                Status(
                    name="l", alive=False, state="gone", outcome=None, attach=None, hidden=False
                ),
                Status(
                    name="s", alive=False, state="starting", outcome=None, attach=None, hidden=False
                ),
            ],
        )


class Attach(RosterCase):
    def test_a_hidden_live_entry_stays_closed_and_its_view_says_hidden(self):
        shown, hidden = (
            replace(self.record, name=name, sessions=(name,), sid=str(uuid.uuid4()))
            for name in ("v", "h")
        )
        self.live("v", record=shown)
        self.live("h", record=hidden)
        shows = []

        def run(argv, **options):
            records = [Record.parse(line) for line in options["input"].splitlines()]
            if argv[1:] == ("show",):
                shows.append(records)
            statuses = (
                Status(
                    name=record.name,
                    alive=True,
                    state="working",
                    outcome=None,
                    attach=f"attach {record.name}",
                    hidden=record.name == "h",
                )
                for record in records
            )
            stdout = "".join(s.to_line() + "\n" for s in statuses) if argv[1] == "status" else ""
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with mock.patch("subprocess.run", side_effect=run):
            table = Crew(self.roster).attach().splitlines()
        self.assertEqual(shows, [[shown]])
        self.assertEqual(table[0], HEADER)
        self.assertIn(f"h\tcustom\t{hidden.sid}\tworking\thidden\t-\t", table[1])
        self.assertIn(f"v\tcustom\t{shown.sid}\tworking\t-\t-\t", table[2])


class Entries(RosterCase):
    """Live entries a, b and c; `answer` stands in for their ctl."""

    def setUp(self):
        super().setUp()
        self.records = {}
        for name in ("a", "b", "c"):
            self.records[name] = replace(
                self.record, name=name, sessions=(name,), sid=str(uuid.uuid4())
            )
            self.live(name, record=self.records[name])
        self.calls = []
        self.alive = {"a": True, "b": True, "c": False}
        self.attach = {"a": "attach a", "b": None, "c": "attach c"}
        self.answers = {}
        self.listed = []

    def answer(self, argv, **options):
        if argv[1] == "ls":
            listed = "".join(record.to_line() + "\n" for record in self.listed)
            return mock.Mock(returncode=0, stdout=listed, stderr="")
        records = [Record.parse(line) for line in options["input"].splitlines()]
        if argv[-1] == "status":
            stdout = "".join(
                Status(
                    name=r.name,
                    alive=self.alive[r.name],
                    state="working",
                    outcome=None,
                    attach=self.attach[r.name],
                    hidden=False,
                ).to_line()
                + "\n"
                for r in records
            )
            return mock.Mock(returncode=0, stdout=stdout, stderr="")
        self.calls.append((argv, records))
        return self.answers.get(records[0].name, mock.Mock(returncode=0, stdout="", stderr=""))


class View(Entries):
    def view(self, verb, names=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch("subprocess.run", side_effect=self.answer),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            failures = Crew(self.roster).view(verb, names)
        return failures, stdout.getvalue(), stderr.getvalue()

    def test_each_named_entry_goes_once_to_its_ctl_in_the_given_order(self):
        failures, _, _ = self.view("hide", ("c", "a", "c"))
        self.assertEqual(failures, {})
        self.assertEqual(
            self.calls,
            [
                (("ensemble-session", "hide"), [self.records["c"]]),
                (("ensemble-session", "hide"), [self.records["a"]]),
            ],
        )

    def test_all_takes_the_alive_showable_entries(self):
        self.view("show")
        self.assertEqual(self.calls, [(("ensemble-session", "show"), [self.records["a"]])])

    def test_the_layers_output_is_relayed(self):
        self.answers["a"] = mock.Mock(returncode=0, stdout="tmux attach -t a\n", stderr="note\n")
        _, stdout, stderr = self.view("show", ("a",))
        self.assertEqual((stdout, stderr), ("tmux attach -t a\n", "note\n"))

    def test_a_failure_leaves_the_rest_and_names_its_reason(self):
        self.answers["a"] = mock.Mock(returncode=1, stdout="", stderr=" boom \n")
        self.answers["b"] = mock.Mock(returncode=1, stdout="", stderr="")
        failures, _, _ = self.view("hide", ("a", "b", "c"))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(
            failures,
            {"a": "ensemble-session hide: boom", "b": "ensemble-session hide: exit 1"},
        )

    def test_all_reports_an_entry_whose_status_failed(self):
        def run(argv, **options):
            if argv[-1] == "status":
                return mock.Mock(returncode=2, stdout="", stderr="down")
            return self.answer(argv, **options)

        stdout = io.StringIO()
        with mock.patch("subprocess.run", side_effect=run), contextlib.redirect_stdout(stdout):
            failures = Crew(self.roster).view("hide")
        self.assertEqual(failures, dict.fromkeys(("a", "b", "c"), "ensemble-session status: down"))
        self.assertEqual(self.calls, [])

    def test_a_named_entry_without_a_record_fails(self):
        self.roster.begin(Spec("s", ARGV), sid=str(uuid.uuid4()))
        failures, _, _ = self.view("show", ("s",))
        self.assertEqual(failures, {"s": "starting: no Record"})
        self.assertEqual(self.calls, [])

    def test_an_unknown_name_raises_before_any_call(self):
        with self.assertRaisesRegex(ValueError, "not in roster: x, y"):
            self.view("hide", ("a", "y", "x"))
        self.assertEqual(self.calls, [])


class Close(Entries):
    def failures(self) -> list[dict]:
        path = Path(self.directory, "close-failures.jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def close(self, name):
        with mock.patch("subprocess.run", side_effect=self.answer):
            Crew(self.roster).close(name)

    def close_all(self, *, dry_run=False):
        with mock.patch("subprocess.run", side_effect=self.answer):
            return Crew(self.roster).close_all(dry_run=dry_run)

    def test_a_confirmed_close_removes_the_entry_and_logs_nothing(self):
        self.close("a")
        self.assertNotIn("a", self.roster.entries())
        self.assertEqual(self.failures(), [])

    def test_a_failed_close_keeps_the_entry_and_logs_one_line(self):
        entry = self.roster.entries()["a"]
        self.answers["a"] = mock.Mock(
            returncode=1, stdout="", stderr="w1: worker exit not confirmed\n"
        )
        with self.assertRaises(RuntimeError) as caught:
            self.close("a")
        path = os.path.join(self.directory, "close-failures.jsonl")
        self.assertEqual(
            str(caught.exception),
            f"ensemble-session close: w1: worker exit not confirmed; logged to {path}",
        )
        self.assertEqual(self.roster.entries()["a"], entry)
        (line,) = self.failures()
        self.assertEqual(
            {k: v for k, v in line.items() if k != "at"},
            {
                "name": "a",
                "sid": entry.sid,
                "ctl": ["ensemble-session"],
                "reason": "ensemble-session close: w1: worker exit not confirmed",
            },
        )

    def test_a_long_ctl_stderr_is_whole_in_the_error_and_cut_in_its_line(self):
        self.answers["a"] = mock.Mock(returncode=1, stdout="", stderr="x" * 600 + "\n")
        with self.assertRaises(RuntimeError) as caught:
            self.close("a")
        reason = "ensemble-session close: " + "x" * 600
        path = os.path.join(self.directory, "close-failures.jsonl")
        self.assertEqual(str(caught.exception), f"{reason}; logged to {path}")
        self.assertIn("a", self.roster.entries())
        (line,) = self.failures()
        self.assertEqual(line["reason"], reason[:500])

    def test_a_trace_that_cannot_be_written_never_hides_the_close_failure(self):
        self.answers["a"] = mock.Mock(returncode=1, stdout="", stderr="boom\n")
        os.mkfifo(os.path.join(self.directory, "close-failures.jsonl"))
        with self.assertRaisesRegex(RuntimeError, "ensemble-session close: boom") as caught:
            self.close("a")
        self.assertTrue(any("close-failures" in note for note in caught.exception.__notes__))
        self.assertIn("a", self.roster.entries())

    def delegated(self, name):
        return replace(
            self.records["a"],
            name=name,
            sessions=(name,),
            sid=str(uuid.uuid4()),
            ctl=("ensemble-delegate",),
            resume=("ensemble-delegate", "resume", name),
        )

    def test_close_all_logs_each_failed_entry_once_and_removes_the_rest(self):
        for name in ("d", "e"):
            self.records[name] = self.delegated(name)
            self.live(name, record=self.records[name])
        orphan = self.delegated("orphan")
        self.listed = [orphan]
        self.answers["d"] = mock.Mock(returncode=1, stdout="", stderr="down\n")
        with self.assertRaisesRegex(RuntimeError, "ensemble-delegate close: down"):
            self.close_all()
        delegated = [records for argv, records in self.calls if argv[0] == "ensemble-delegate"]
        self.assertEqual(delegated, [[self.records["d"], self.records["e"], orphan]])
        self.assertEqual(list(self.roster.entries()), ["d", "e"])
        self.assertEqual(
            [(line["name"], line["reason"]) for line in self.failures()],
            [("d", "ensemble-delegate close: down"), ("e", "ensemble-delegate close: down")],
        )

    def test_close_all_names_a_listed_orphan_and_logs_no_line_for_it(self):
        orphan = self.delegated("orphan")
        self.listed = [orphan]
        self.answers["orphan"] = mock.Mock(returncode=1, stdout="", stderr="down\n")
        self.roster.remove("b")
        self.roster.remove("c")
        self.roster.remove("a")
        with self.assertRaisesRegex(RuntimeError, "ensemble-delegate close: down"):
            self.close_all()
        self.assertEqual((self.roster.entries(), self.failures()), ({}, []))
        self.assertIn(([orphan]), [records for _, records in self.calls])

    def test_close_all_keeps_the_failure_when_its_trace_cannot_be_written(self):
        self.records["d"] = self.delegated("d")
        self.live("d", record=self.records["d"])
        self.answers["d"] = mock.Mock(returncode=1, stdout="", stderr="down\n")
        os.mkfifo(os.path.join(self.directory, "close-failures.jsonl"))
        with self.assertRaisesRegex(RuntimeError, "ensemble-delegate close: down") as caught:
            self.close_all()
        self.assertTrue(any("d: close-failures" in note for note in caught.exception.__notes__))
        self.assertEqual(list(self.roster.entries()), ["d"])

    def test_a_dry_run_logs_nothing(self):
        self.answers["a"] = mock.Mock(returncode=1, stdout="", stderr="boom\n")
        self.close_all(dry_run=True)
        self.assertEqual(self.failures(), [])


class CloseAllCapacityTest(HostedTuiBase):
    def test_close_all_succeeds_when_another_session_ends_during_inventory(self):
        self.configure(tui=True, max_runs=2)
        sessions = [self.start(mode="tui", name=name) for name in ("one", "two")]
        roster = Roster(os.path.join(self.ensemble, "managers", "manager"))
        for session in sessions:
            record = Record(
                1,
                "fake",
                session.name,
                (session.name,),
                session.sid,
                self.home,
                ("ensemble-session",),
                ("ensemble-session", "resume", session.name),
                None,
                None,
            )
            roster.begin(Spec(record.name, ("unused",)), sid=record.sid)
            roster.commit(record.name, record)
        ended = self.host.create("z-ending", metadata={"client": "fake", "sid": "old"})

        def ending(name):
            if name == "z-ending":
                ended.close()

        self.host.calls.attach.side_effect = ending
        closed = []

        def control(argv, **options):
            if argv[-1] == "--descendants":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertEqual(argv[-1], "close")
            for line in options["input"].splitlines():
                record = Record.parse(line)
                self.session.attach(record.name, sid=record.sid).close()
                closed.append(record.name)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=control):
            self.assertEqual(Crew(roster).close_all(), ["one", "two"])
        self.assertEqual(closed, ["one", "two"])
        self.assertEqual(roster.entries(), {})
        self.start(mode="tui", name="next").close()  # its admission takes the inventory
        self.assertNotIn("z-ending", self.host.list())
        ended.calls.close.assert_called_once_with()
