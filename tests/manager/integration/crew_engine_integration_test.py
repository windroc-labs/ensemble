"""Crew starts and controls Records through real fake ctl processes, never lower-layer APIs."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest import mock

from ensemble.manager.engine.crew import (
    DESCENDANTS,
    GRACE,
    HERE,
    RECOVERY_AGE,
    RECOVERY_MARGIN,
    START_TIMEOUT,
    Crew,
)
from ensemble.manager.engine.roster import LOCK_TIMEOUT, Roster
from ensemble.manager.engine.types import Held, RosterError, Spec, Stale, State, StatusError
from tests.manager.case import Case, own, status, with_ctl
from tests.manager.starters import answered
from tests.utils.fakes import ctl

MISSING = "ensemble-delegate"  # a layer command off setup_ctl's PATH


class CrewTest(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.directory = os.path.join(self.home, "roster")
        self.roster = Roster(self.directory)
        self.crew = Crew(self.roster)
        with self.listed():
            self.crew.attach()

    def start(self, name, *, note=None, display=None):
        argv = (self.command, "start", name)
        if display is not None:
            argv += ("--display", display)
        return self.crew.run(Spec(name, argv, note))

    def gone(self, name):
        record = self.start(name)
        ctl.set_session(self.ctl_dir, name, alive=False)
        with self.listed():
            self.crew.attach()
        self.assertEqual(self.roster.entries()[name].state, State.GONE)
        return record

    def calls(self, verb):
        return [call for call in ctl.calls(self.ctl_dir) if call["argv"][0] == verb]

    def age(self, name, seconds=RECOVERY_AGE + 1, *, instant=None):
        path = os.path.join(self.directory, "roster.json")
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        now = datetime.now(UTC) if instant is None else instant
        value["entries"][name]["started"] = (now - timedelta(seconds=seconds)).isoformat()
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(value, stream)

    def test_run_commits_the_last_line_and_keeps_intent_note_and_display(self):
        record = self.start("worker", note="investigate", display="opaque value")
        entry = self.roster.entries()["worker"]
        self.assertEqual(entry.record, record)
        self.assertEqual(entry.record.sessions, ("worker",))
        self.assertEqual(entry.record.resume, (self.command, "resume", "worker"))
        self.assertEqual(entry.record.display, "opaque value")
        self.assertEqual((entry.state, entry.note), (State.LIVE, "investigate"))

    def test_start_process_observes_starting_entry_unlocked_events_and_caller_path(self):
        record = self.fake_record("worker")
        code = (
            "import fcntl,json,os,stat,sys\n"
            "path,caller_path,line=sys.argv[1:]\n"
            "with open(path+'/roster.json') as f: entry=json.load(f)['entries']['worker']\n"
            "assert entry['state']=='starting' and entry['record'] is None\n"
            "assert stat.S_ISREG(os.fstat(1).st_mode)\n"
            "fd=os.open(path+'/roster.lock',os.O_RDWR)\n"
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "assert os.environ['ENSEMBLE_EVENTS']==path+'/events'\n"
            "assert os.environ['PATH']==caller_path\n"
            "print('starter diagnostics')\n"
            "assert os.environ['ENSEMBLE_SID']==entry['sid']\n"
            "record=json.loads(line)\n"
            "record['sid']=os.environ['ENSEMBLE_SID']\n"
            "print(json.dumps(record))"
        )
        argv = (
            sys.executable,
            "-I",
            "-S",
            "-c",
            code,
            self.directory,
            os.environ["PATH"],
            record.to_line(),
        )
        started = self.crew.run(Spec("worker", argv))
        self.assertEqual(started, replace(record, sid=self.roster.entries()["worker"].sid))

    def test_bad_empty_nonzero_and_missing_command_runs_become_lost(self):
        cases = {
            "empty": (sys.executable, "-I", "-S", "-c", "pass"),
            "invalid": (sys.executable, "-I", "-S", "-c", "print('{}')"),
            "failed": (sys.executable, "-I", "-S", "-c", "import sys;sys.exit(7)"),
            "missing": ("missing-starter-task371",),
        }
        for name, argv in cases.items():
            with self.subTest(name=name), self.assertRaises((RuntimeError, RosterError)):
                self.crew.run(Spec(name, argv))
            self.assertEqual(self.roster.entries()[name].state, State.LOST)
            with self.assertRaisesRegex(RuntimeError, "lost.*no Record"):
                self.crew.resume(name)
        with self.listed():
            table = self.crew.attach()
        for name in cases:
            self.assertIn(f"{name}\t-\t-\tlost\t", table)

    def test_timeout_marks_run_lost_and_resume_gone(self):
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == HERE:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise subprocess.TimeoutExpired(argv, options["timeout"])

        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch(
                "subprocess.Popen", side_effect=answered(run, (self.command, "start", "lost"))
            ),
            self.assertRaisesRegex(RuntimeError, "1920 seconds"),
        ):
            self.crew.run(Spec("lost", (self.command, "start", "lost")))
        self.assertEqual(self.roster.entries()["lost"].state, State.LOST)
        with mock.patch("subprocess.run", side_effect=real_run):
            self.gone("gone")
        resume = (self.command, "resume", "gone")

        def timeout_resume(argv, **options):
            if tuple(argv) == resume:
                raise subprocess.TimeoutExpired(argv, options["timeout"])
            return real_run(argv, **options)

        with (
            mock.patch("subprocess.run", side_effect=timeout_resume),
            mock.patch("subprocess.Popen", side_effect=answered(timeout_resume, resume)),
            self.assertRaisesRegex(RuntimeError, "1920 seconds"),
        ):
            self.crew.resume("gone")
        self.assertEqual(self.roster.entries()["gone"].state, State.GONE)

    def test_inherited_sid_is_overwritten_on_both_start_and_resume(self):
        inherited = str(uuid.uuid4())
        with mock.patch.dict(os.environ, {"ENSEMBLE_SID": inherited}):
            started = self.gone("worker")
            self.assertNotEqual(started.sid, inherited)
            self.assertEqual(str(uuid.UUID(started.sid)), started.sid)
            resumed = self.crew.resume("worker")
        self.assertEqual(resumed, started)
        self.assertEqual(self.roster.entries()["worker"].sid, started.sid)

    def test_launch_uses_own_events_and_control_commands_scrub_inherited_events_and_sid(self):
        calls = []
        native_run = subprocess.run

        def run(argv, **options):
            verb = "ls" if tuple(argv) == HERE else argv[1]
            calls.append((verb, options["env"]))
            if tuple(argv) == HERE:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return native_run(argv, **options)

        with (
            mock.patch.dict(
                os.environ, {"ENSEMBLE_EVENTS": "/parent/events", "ENSEMBLE_SID": "parent"}
            ),
            mock.patch("subprocess.run", side_effect=run),
            mock.patch(
                "subprocess.Popen",
                side_effect=answered(
                    run, (self.command, "start", "worker"), (self.command, "resume", "worker")
                ),
            ),
        ):
            record = self.start("worker")
            ctl.set_session(self.ctl_dir, "worker", showable=True)
            self.crew.attach()
            ctl.set_session(self.ctl_dir, "worker", alive=False)
            self.crew.attach()
            self.assertEqual(self.crew.resume("worker"), record)
            self.crew.close("worker")
        self.assertEqual(
            {verb for verb, _env in calls}, {"start", "resume", "ls", "status", "show", "close"}
        )
        for verb, env in calls:
            with self.subTest(verb=verb):
                if verb in {"start", "resume"}:
                    self.assertEqual(env["ENSEMBLE_EVENTS"], os.path.join(self.directory, "events"))
                    self.assertEqual(env["ENSEMBLE_SID"], record.sid)
                else:
                    self.assertNotIn("ENSEMBLE_EVENTS", env)
                    self.assertNotIn("ENSEMBLE_SID", env)

    def test_null_or_mismatched_sid_keep_launch_lost(self):
        cases = {
            "null": replace(self.fake_record("null"), sid=None).to_line() + "\n",
            "mismatch": self.fake_record("mismatch").to_line() + "\n",
        }
        for name, text in cases.items():
            command = (
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import sys;sys.stdout.write(sys.argv[1])",
                text,
            )
            with self.subTest(name=name), self.assertRaises(RosterError):
                self.crew.run(Spec(name, command))
            entry = self.roster.entries()[name]
            self.assertEqual(entry.state, State.LOST)
            self.assertIsNone(entry.record)
            self.assertIsNotNone(entry.receipt)
            self.assertTrue(os.path.exists(os.path.join(self.directory, entry.receipt)))

    def test_final_json_without_newline_commits_but_truncated_json_stays_lost(self):
        code = (
            "import json,os,sys\n"
            "record=json.loads(sys.argv[1])\n"
            "record['sid']=os.environ['ENSEMBLE_SID']\n"
            "text=json.dumps(record)\n"
            "sys.stdout.write(text[:len(text)-int(sys.argv[2])])"
        )
        for name, cut in (("valid", 0), ("truncated", 1)):
            record = self.fake_record(name)
            argv = (sys.executable, "-I", "-S", "-c", code, record.to_line(), str(cut))
            if cut:
                with self.assertRaises(RosterError):
                    self.crew.run(Spec(name, argv))
                self.assertEqual(self.roster.entries()[name].state, State.LOST)
            else:
                started = self.crew.run(Spec(name, argv))
                self.assertEqual(started, replace(record, sid=self.roster.entries()[name].sid))
                self.assertEqual(self.roster.entries()[name].state, State.LIVE)

    def test_attach_reports_sid_ownership_conflicts_without_replacing_entries(self):
        original = self.start("worker")
        conflicting = replace(original, name="different-record")
        error = io.StringIO()
        with self.listed((conflicting,)), contextlib.redirect_stderr(error):
            self.crew.attach()
        self.assertIn("adopt different-record:", error.getvalue())
        self.assertEqual(self.roster.entries()["worker"].record, original)
        self.assertNotIn("different-record", self.roster.entries())

    def test_resume_sid_mismatch_restores_gone_with_previous_identity(self):
        original = self.gone("worker")
        wrong = replace(original, sid=str(uuid.uuid4()))
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == original.resume:
                options["stdout"].write((wrong.to_line() + "\n").encode())
                options["stdout"].flush()
                return subprocess.CompletedProcess(argv, 0, None, "")
            return real_run(argv, **options)

        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch("subprocess.Popen", side_effect=answered(run, original.resume)),
            self.assertRaises(RosterError),
        ):
            self.crew.resume("worker")
        entry = self.roster.entries()["worker"]
        self.assertEqual(
            (entry.state, entry.record, entry.sid), (State.GONE, original, original.sid)
        )

    def test_attach_batches_twenty_records_and_shows_only_showable_live_entries(self):
        records = []
        for number in range(20):
            name = f"role{number}"
            records.append(self.start(name))
            ctl.set_session(self.ctl_dir, name, showable=number < 2)
        with self.listed(records), mock.patch("subprocess.run", wraps=subprocess.run) as run:
            table = self.crew.attach()
        self.assertEqual(len(self.calls("status")), 1)
        self.assertEqual(len(self.calls("status")[0]["names"]), 20)
        self.assertEqual(self.calls("show")[0]["names"], ["role0", "role1"])
        self.assertEqual(
            len([call for call in run.call_args_list if tuple(call.args[0]) == HERE]), 1
        )
        self.assertIn(f"role0\tfake\t{records[0].sid}\tworking", table)
        self.assertIn(f"role19\tfake\t{records[19].sid}\tworking", table)

    def test_a_hidden_entry_stays_closed_after_crew_resume(self):
        self.gone("worker")
        ctl.set_session(self.ctl_dir, "worker", showable=True, hidden=True)
        resumed = self.crew.resume("worker")
        self.assertEqual(self.calls("show"), [])
        self.assertTrue(self.crew.status(("worker",))[0].hidden)
        with self.listed([resumed]):
            table = self.crew.attach()
        self.assertEqual(self.calls("show"), [])
        self.assertIn(f"worker\tfake\t{resumed.sid}\tworking\thidden\t", table)

    def test_attach_renders_its_status_snapshot_during_a_concurrent_launch(self):
        self.start("old")
        with self.listed():
            real_run = subprocess.run

            def run(argv, **options):
                result = real_run(argv, **options)
                if tuple(argv) == (self.command, "status"):
                    self.start("new")
                return result

            with mock.patch("subprocess.run", side_effect=run):
                table = self.crew.attach()
        self.assertIn("old\tfake", table)
        self.assertNotIn("new\tfake", table)
        self.assertEqual(set(self.roster.entries()), {"old", "new"})

    def test_attach_maps_outcomes_and_keeps_terminal_states_stable(self):
        for name, outcome in (
            ("done", "done"),
            ("failed", "failed"),
            ("waiting", "needs_input"),
            ("gone", None),
        ):
            self.start(name)
            ctl.set_session(self.ctl_dir, name, alive=False, outcome=outcome)
        with self.listed():
            self.crew.attach()
        self.assertEqual(
            {name: entry.state for name, entry in self.roster.entries().items()},
            {
                "done": State.DONE,
                "failed": State.DONE,
                "waiting": State.WAITING,
                "gone": State.GONE,
            },
        )
        for name in ("done", "failed", "waiting"):
            ctl.set_session(self.ctl_dir, name, alive=True, outcome=None, showable=True)
        with self.listed():
            self.crew.attach()
        self.assertEqual(self.calls("show"), [])
        self.assertEqual(self.roster.entries()["waiting"].state, State.WAITING)
        self.assertEqual(self.roster.entries()["done"].state, State.DONE)

    def test_manual_resume_is_detected_without_crew_starting_anything(self):
        self.gone("worker")
        ctl.set_session(self.ctl_dir, "worker", alive=True)
        with self.listed():
            self.crew.attach()
        self.assertEqual(self.roster.entries()["worker"].state, State.LIVE)
        self.assertEqual(self.calls("resume"), [])

    def test_attach_tolerates_concurrent_resume_after_status_snapshot(self):
        self.gone("resume")
        ctl.set_session(self.ctl_dir, "resume", alive=True)
        real_run = subprocess.run

        def run(argv, **options):
            result = real_run(argv, **options)
            if tuple(argv) == (self.command, "status"):
                Roster(self.directory).claim_resume("resume")
            return result

        with mock.patch("subprocess.run", side_effect=run), self.listed():
            table = self.crew.attach()
        self.assertEqual(self.roster.entries()["resume"].state, State.RESUMING)
        self.assertIn("resume\t", table)

    def test_attach_tolerates_concurrent_close_after_status_snapshot(self):
        self.gone("stop")
        ctl.set_session(self.ctl_dir, "stop", alive=True)
        real_run = subprocess.run

        def run(argv, **options):
            result = real_run(argv, **options)
            if tuple(argv) == (self.command, "status"):
                Roster(self.directory).remove("stop")
            return result

        with mock.patch("subprocess.run", side_effect=run), self.listed():
            table = self.crew.attach()
        self.assertNotIn("stop", self.roster.entries())
        self.assertNotIn("stop\t", table)

    def test_attach_recovery_age_below_at_and_above_boundary_for_both_pending_states(self):
        instant = datetime(2026, 10, 13, 12, tzinfo=UTC)
        for state in (State.STARTING, State.RESUMING):
            for offset in (-1, 0, 1):
                name = f"{state}-{offset}"
                with self.subTest(state=state, offset=offset):
                    if state == State.STARTING:
                        self.roster.begin(
                            Spec(name, (self.command, "start", name)), sid=str(uuid.uuid4())
                        )
                    else:
                        self.gone(name)
                        self.roster.claim_resume(name)
                    self.age(name, RECOVERY_AGE + offset, instant=instant)
                    with self.listed(), mock.patch("datetime.datetime", wraps=datetime) as clock:
                        clock.now.return_value = instant
                        self.crew.attach()
                    expired = State.LOST if state == State.STARTING else State.GONE
                    self.assertEqual(
                        self.roster.entries()[name].state, expired if offset > 0 else state
                    )

    def test_attach_preserves_commit_during_legitimate_delayed_launch_and_resume(self):
        origin = datetime(2026, 10, 13, 12, microsecond=999999, tzinfo=UTC)
        duration = START_TIMEOUT + GRACE + 4 * LOCK_TIMEOUT
        for state in (State.STARTING, State.RESUMING):
            name = f"delayed-{state}"
            original = None if state == State.STARTING else self.gone(name)
            entered, proceed = Event(), Event()
            with self.listed():
                native_run = subprocess.run
                with mock.patch("datetime.datetime", wraps=datetime) as clock:
                    clock.now.return_value = origin

                    def run(
                        argv,
                        *,
                        name=name,
                        entered=entered,
                        proceed=proceed,
                        native_run=native_run,
                        **options,
                    ):
                        if tuple(argv) in {
                            (self.command, "start", name),
                            (self.command, "resume", name),
                        }:
                            clock.now.return_value = origin + timedelta(
                                seconds=duration + RECOVERY_MARGIN / 2
                            )
                            entered.set()
                            if not proceed.wait(5):
                                raise AssertionError("pending launch was not released")
                        return native_run(argv, **options)

                    with (
                        mock.patch("subprocess.run", side_effect=run),
                        mock.patch(
                            "subprocess.Popen",
                            side_effect=answered(
                                run, (self.command, "start", name), (self.command, "resume", name)
                            ),
                        ),
                        ThreadPoolExecutor(max_workers=1) as pool,
                    ):
                        crew = Crew(Roster(self.directory))
                        future = (
                            pool.submit(crew.run, Spec(name, (self.command, "start", name)))
                            if state == State.STARTING
                            else pool.submit(crew.resume, name)
                        )
                        try:
                            self.assertTrue(entered.wait(5), "launch did not reach its starter")
                            observed = Roster(self.directory).entries()[name]
                            age = (
                                clock.now.return_value - datetime.fromisoformat(observed.started)
                            ).total_seconds()
                            self.assertGreater(age, duration)
                            self.assertGreater(age - duration, RECOVERY_MARGIN / 2)
                            self.assertLess(age, RECOVERY_AGE)
                            Crew(Roster(self.directory)).attach()
                            self.assertEqual(Roster(self.directory).entries()[name].state, state)
                        finally:
                            proceed.set()
                        record = future.result(5)
            entry = self.roster.entries()[name]
            self.assertEqual((entry.state, entry.record), (State.LIVE, record))
            self.assertIsNone(entry.receipt)
            if original is not None:
                self.assertEqual(record, original)

    def test_attach_adopts_only_the_sid_matched_orphan_under_its_alias_and_note(self):
        sid = str(uuid.uuid4())
        self.roster.begin(Spec("alias", (self.command, "start", "actual-name"), "keep"), sid=sid)
        self.age("alias")
        orphan = self.fake_record("actual-name", sid=sid)
        extra = self.fake_record("extra")
        ctl.set_session(self.ctl_dir, "actual-name", alive=True, sid=sid)
        ctl.set_session(self.ctl_dir, "extra", alive=True, sid=extra.sid)
        with self.listed((orphan, orphan, extra)):
            self.crew.attach()
        self.assertEqual(set(self.roster.entries()), {"alias"})
        self.assertEqual(self.roster.entries()["alias"].note, "keep")
        self.assertEqual(self.roster.entries()["alias"].record, orphan)
        self.assertEqual(self.roster.entries()["alias"].state, State.LIVE)
        self.assertEqual(self.crew.status(("alias",))[0].name, "alias")

    def test_resume_replays_exact_argv_and_preserves_display(self):
        original = self.start("role", display="opaque")
        ctl.set_session(self.ctl_dir, "role", alive=False)
        with self.listed():
            table = self.crew.attach()
        self.assertNotIn("resume role: ensemble-orchestrator resume role", table)
        self.assertIn(f"role\tfake\t{original.sid}\tgone\t", table)
        record = self.crew.resume("role")
        self.assertEqual(record.display, "opaque")
        self.assertEqual(self.calls("resume"), [{"argv": ["resume", "role"], "names": ["role"]}])
        self.assertEqual(self.roster.entries()["role"].state, State.LIVE)

    def test_attach_during_resume_retains_receipt_until_original_record_is_parsed(self):
        original = self.gone("worker")
        with self.listed((original,)):
            real_run = subprocess.run
            receipts = []

            def run(argv, **options):
                result = real_run(argv, **options)
                if tuple(argv) == original.resume:
                    other = Crew(Roster(self.directory))
                    other.attach()
                    entry = other.roster.entries()["worker"]
                    self.assertEqual((entry.state, entry.record), (State.RESUMING, original))
                    self.assertIsNotNone(entry.receipt)
                    path = os.path.join(self.directory, entry.receipt)
                    self.assertTrue(os.path.exists(path))
                    receipts.append(path)
                return result

            with (
                mock.patch("subprocess.run", side_effect=run),
                mock.patch("subprocess.Popen", side_effect=answered(run, original.resume)),
            ):
                resumed = self.crew.resume("worker")
        self.assertEqual(resumed, original)
        self.assertEqual(self.roster.entries()["worker"].state, State.LIVE)
        self.assertIsNone(self.roster.entries()["worker"].receipt)
        self.assertEqual(len(receipts), 1)
        self.assertFalse(os.path.exists(receipts[0]))
        self.assertEqual(len(self.calls("resume")), 1)

    def test_resume_live_and_in_progress_start_nothing(self):
        record = self.start("live")
        self.assertEqual(self.crew.resume("live"), record)
        self.gone("resuming")
        self.roster.claim_resume("resuming")
        self.assertIsNone(self.crew.resume("resuming"))
        self.assertEqual(self.calls("resume"), [])

    def test_concurrent_resume_starts_once(self):
        self.gone("worker")

        def resume():
            return Crew(Roster(self.directory)).resume("worker")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: resume(), range(2)))
        self.assertTrue(any(record is not None for record in results))
        self.assertEqual(len(self.calls("resume")), 1)
        self.assertEqual(self.roster.entries()["worker"].state, State.LIVE)

    def test_status_returns_aliases_batches_and_never_shows_or_writes_state(self):
        self.start("one")
        self.start("two")
        ctl.set_session(self.ctl_dir, "one", showable=True)
        ctl.set_session(self.ctl_dir, "two", alive=False, outcome="done")
        statuses = self.crew.status(("two", "one"))
        self.assertEqual([status.name for status in statuses], ["two", "one"])
        self.assertEqual(statuses[0].outcome, "done")
        self.assertEqual(len(self.calls("status")), 1)
        self.assertEqual(self.calls("show"), [])
        self.assertEqual(self.roster.entries()["two"].state, State.LIVE)

    def test_attach_shows_the_cwd_each_status_reports(self):
        record = self.start("one")
        self.start("two")
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == (self.command, "status"):
                lines = (
                    status(name, cwd=cwd).to_line()
                    for name, cwd in (("one", "/w/repo one"), ("two", None))
                )
                return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")
            return real_run(argv, **options)

        with mock.patch("subprocess.run", side_effect=run), self.listed():
            table = self.crew.attach().splitlines()
        rows = {row.split("\t")[0]: row.split("\t") for row in table[1:] if "\t" in row}
        self.assertEqual(table[0].split("\t")[6], "cwd")
        self.assertEqual((rows["one"][6], rows["two"][6]), ("/w/repo one", "-"))
        self.assertNotEqual(rows["one"][6], record.workdir)

    def test_a_key_its_ctl_fails_is_the_only_status_lost(self):
        self.start("one")
        self.start("two")
        ctl.set_session(self.ctl_dir, "one", alive=True)
        one = status("one")
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == (self.command, "status"):
                return subprocess.CompletedProcess(argv, 1, one.to_line() + "\n", "two: boom\n")
            return real_run(argv, **options)

        with mock.patch("subprocess.run", side_effect=run):
            with self.assertRaises(StatusError) as caught:
                self.crew.status(("two", "one"))
            self.assertEqual(caught.exception.statuses, [one])
            self.assertIn("two:", str(caught.exception))
            self.assertIn("boom", str(caught.exception))
            self.assertNotIn("one:", str(caught.exception))
            stderr = io.StringIO()
            with self.listed(), contextlib.redirect_stderr(stderr):
                table = self.crew.attach()
        self.assertIn("one\tfake\t", table)
        self.assertIn(f"one\tfake\t{self.roster.entries()['one'].sid}\tworking\t", table)
        self.assertIn(f"two\tfake\t{self.roster.entries()['two'].sid}\tunknown\t", table)
        self.assertIn("crew: two: ", stderr.getvalue())

    def test_status_rejects_missing_duplicate_and_extra_responses(self):
        self.start("worker")
        responses = (
            "",
            (status("worker").to_line() + "\n") * 2,
            status("extra").to_line() + "\n",
        )
        for raw in responses:
            with (
                self.subTest(raw=raw),
                mock.patch(
                    "subprocess.run", return_value=subprocess.CompletedProcess([], 0, raw, "")
                ),
                self.assertRaises(RuntimeError),
            ):
                self.crew.status()

    def test_close_by_session_closes_then_forgets_and_forget_does_not_close(self):
        record = self.start("one")
        self.start("two")
        self.crew.close(record.sessions[0])
        ctl.set_session(self.ctl_dir, "two", alive=False)
        self.crew.forget("two")
        self.assertEqual(self.roster.entries(), {})
        self.assertEqual(self.calls("close"), [{"argv": ["close"], "names": ["one"]}])

    def test_close_prefers_exact_roster_alias_over_another_entries_session_name(self):
        first = self.crew.run(Spec("first", (self.command, "start", "actual")))
        self.crew.run(Spec("actual", (self.command, "start", "other")))
        self.crew.close("actual")
        self.assertEqual(set(self.roster.entries()), {"first"})
        self.assertEqual(self.roster.entries()["first"].record, first)
        self.assertTrue(self.crew.status(("first",))[0].alive)
        self.assertEqual(self.calls("close"), [{"argv": ["close"], "names": ["other"]}])

    def test_attach_keeps_failed_ctl_unknown_and_processes_healthy_group(self):
        healthy = self.start("healthy", display="view")
        ctl.set_session(self.ctl_dir, "healthy", showable=True)
        bad = with_ctl(self.fake_record("broken"), MISSING)
        own(self.roster, bad)
        stderr = io.StringIO()
        with self.listed((healthy, bad)), contextlib.redirect_stderr(stderr):
            table = self.crew.attach()
        self.assertIn(MISSING, stderr.getvalue())
        self.assertIn("broken\tfake\t" + bad.sid + "\tunknown\t", table)
        self.assertEqual(self.roster.entries()["broken"].state, State.LIVE)
        self.assertEqual(self.calls("show"), [{"argv": ["show"], "names": ["healthy"]}])

    def test_attach_reports_nonzero_status_and_show_without_aborting_other_groups(self):
        self.start("healthy", display="view")
        ctl.set_session(self.ctl_dir, "healthy", showable=True)
        bad = with_ctl(self.fake_record("broken"), "ensemble-session")
        command = os.path.join(self.bin_dir, bad.ctl[0])
        with open(command, "w", encoding="utf-8") as stream:
            stream.write("#!/bin/sh\necho broken-controller >&2\nexit 7\n")
        os.chmod(command, 0o700)
        own(self.roster, bad)
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == (self.command, "show"):
                return subprocess.CompletedProcess(argv, 1, "", "show failed")
            return real_run(argv, **options)

        stderr = io.StringIO()
        with (
            mock.patch("subprocess.run", side_effect=run),
            self.listed(),
            contextlib.redirect_stderr(stderr),
        ):
            table = self.crew.attach()
        self.assertIn("broken-controller", stderr.getvalue())
        self.assertIn("show failed", stderr.getvalue())
        self.assertIn("healthy\t", table)
        self.assertEqual(self.roster.entries()["broken"].state, State.LIVE)

    def test_force_forget_drops_bookkeeping_without_any_controller_call(self):
        record = self.start("worker")
        stderr = io.StringIO()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("unavailable")):
            with self.assertRaises(RuntimeError):
                self.crew.forget("worker")
            self.assertIn("worker", self.roster.entries())
            with contextlib.redirect_stderr(stderr):
                self.crew.forget("worker", force=True)
        self.assertEqual(self.roster.entries(), {})
        self.assertEqual(self.calls("close"), [])
        with open(os.path.join(self.ctl_dir, ctl.SESSIONS), encoding="utf-8") as stream:
            self.assertTrue(json.load(stream)[record.name]["alive"])

    def test_failed_orphan_listing_keeps_resume_unknown_and_checks_owned_records(self):
        self.gone("resuming")
        self.roster.claim_resume("resuming")
        self.age("resuming")
        self.start("healthy")
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == HERE:
                return subprocess.CompletedProcess(argv, 1, "", "listing unavailable")
            return real_run(argv, **options)

        stderr = io.StringIO()
        with mock.patch("subprocess.run", side_effect=run), contextlib.redirect_stderr(stderr):
            table = self.crew.attach()
        self.assertIn("listing unavailable", stderr.getvalue())
        self.assertEqual(self.roster.entries()["resuming"].state, State.RESUMING)
        self.assertIn("healthy\t", table)
        self.assertIn("healthy", self.calls("status")[-1]["names"])

    def test_close_all_includes_here_orphans_deduplicates_and_excludes_unlisted_records(self):
        owned = self.start("owned")
        orphan = self.fake_record("orphan")
        ctl.set_session(self.ctl_dir, "orphan", alive=True)
        ctl.set_session(self.ctl_dir, "outside", alive=True)
        with self.listed((owned, orphan)):
            self.assertEqual(self.crew.close_all(dry_run=True), ["orphan", "owned"])
        self.assertEqual(self.calls("close"), [])
        with self.listed((owned, orphan)):
            self.assertEqual(self.crew.close_all(), ["orphan", "owned"])
        self.assertEqual(self.roster.entries(), {})
        self.assertEqual(self.calls("close"), [{"argv": ["close"], "names": ["owned", "orphan"]}])

    def test_each_ctl_is_queried_once_and_answers_are_mapped_to_their_names(self):
        self.start("one")
        second = "ensemble-delegate"
        shutil.copyfile(
            os.path.join(self.bin_dir, self.command), os.path.join(self.bin_dir, second)
        )
        os.chmod(os.path.join(self.bin_dir, second), 0o700)
        record = self.fake_record("two")
        own(self.roster, with_ctl(record, second))
        ctl.set_session(self.ctl_dir, "two", alive=True)
        statuses = self.crew.status()
        self.assertEqual([status.name for status in statuses], ["one", "two"])
        self.assertEqual([call["names"] for call in self.calls("status")], [["one"], ["two"]])

    def test_a_live_holder_blocks_attach_and_launch_without_starting_a_process(self):
        with Roster(self.directory).hold("owner"), self.listed():
            other = Crew(Roster(self.directory))
            with self.assertRaises(Held):
                other.attach()
            with self.assertRaises(Held):
                other.run(Spec("worker", (self.command, "start", "worker")))
        self.assertEqual(self.calls("start"), [])
        self.assertNotIn("worker", self.roster.entries())

    def test_run_failure_remains_primary_when_takeover_prevents_marking_lost(self):
        command = (self.command, "start", "worker")
        native_run = subprocess.run
        with self.roster.hold("original") as lease:
            crew = Crew(self.roster, lease=lease)

            def run(argv, **options):
                if tuple(argv) == command:
                    with Roster(self.directory).hold("successor", force=True) as successor:
                        self.assertGreater(successor.generation, lease.generation)
                    return subprocess.CompletedProcess(argv, 7, None, "starter failed")
                return native_run(argv, **options)

            with (
                mock.patch("subprocess.run", side_effect=run),
                mock.patch("subprocess.Popen", side_effect=answered(run, command)),
                self.assertRaisesRegex(RuntimeError, "starter failed") as raised,
            ):
                crew.run(Spec("worker", command))
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(
                raised.exception.__notes__,
                [
                    f"worker: could not mark lost: {self.directory}: stale holder generation; start a new manager holder"
                ],
            )
        entry = self.roster.entries()["worker"]
        self.assertEqual(entry.state, State.STARTING)
        self.assertIsNone(entry.record)
        self.assertIsNotNone(entry.receipt)

    def test_resume_failure_remains_primary_when_unsafe_ledger_prevents_marking_gone(self):
        original = self.gone("worker")
        backup = os.path.join(self.home, "ledger-backup.json")
        native_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == original.resume:
                os.replace(self.roster.path, backup)
                os.mkdir(self.roster.path)
                return subprocess.CompletedProcess(argv, 7, None, "resume starter failed")
            return native_run(argv, **options)

        try:
            with (
                mock.patch("subprocess.run", side_effect=run),
                mock.patch("subprocess.Popen", side_effect=answered(run, original.resume)),
                self.assertRaisesRegex(RuntimeError, "resume starter failed") as raised,
            ):
                self.crew.resume("worker")
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(
                raised.exception.__notes__,
                [f"worker: could not mark gone: {self.roster.path}: not a readable regular file"],
            )
        finally:
            if os.path.isdir(self.roster.path):
                os.rmdir(self.roster.path)
                os.replace(backup, self.roster.path)
        entry = self.roster.entries()["worker"]
        self.assertEqual((entry.state, entry.record), (State.RESUMING, original))
        self.assertIsNotNone(entry.receipt)

    def test_run_preserves_native_start_failure_cause_when_takeover_prevents_marking(self):
        command = ("missing-starter-cause-task371",)
        native_run = subprocess.run
        failures = []
        with self.roster.hold("original") as lease:
            crew = Crew(self.roster, lease=lease)

            def run(argv, **options):
                if tuple(argv) == command:
                    with Roster(self.directory).hold("successor", force=True) as successor:
                        self.assertGreater(successor.generation, lease.generation)
                    try:
                        return native_run(argv, **options)
                    except FileNotFoundError as error:
                        failures.append(error)
                        raise
                return native_run(argv, **options)

            with (
                mock.patch("subprocess.run", side_effect=run),
                mock.patch("subprocess.Popen", side_effect=answered(run, command)),
                self.assertRaisesRegex(RuntimeError, command[0]) as raised,
            ):
                crew.run(Spec("worker", command))
            self.assertEqual(len(failures), 1)
            self.assertIs(raised.exception.__cause__, failures[0])
            self.assertIsInstance(failures[0], FileNotFoundError)
            self.assertEqual(
                raised.exception.__notes__,
                [
                    f"worker: could not mark lost: {self.directory}: stale holder generation; start a new manager holder"
                ],
            )
        self.assertEqual(self.roster.entries()["worker"].state, State.STARTING)

    def test_resume_preserves_native_start_failure_cause_when_unsafe_ledger_prevents_marking(self):
        original = self.gone("worker")
        backup = os.path.join(self.home, "ledger-backup.json")
        missing = ("missing-resume-cause-task371",)
        native_run = subprocess.run
        failures = []

        def run(argv, **options):
            if tuple(argv) == original.resume:
                os.replace(self.roster.path, backup)
                os.mkdir(self.roster.path)
                try:
                    return native_run(missing, **options)
                except FileNotFoundError as error:
                    failures.append(error)
                    raise
            return native_run(argv, **options)

        try:
            with (
                mock.patch("subprocess.run", side_effect=run),
                mock.patch("subprocess.Popen", side_effect=answered(run, original.resume)),
                self.assertRaisesRegex(RuntimeError, missing[0]) as raised,
            ):
                self.crew.resume("worker")
            self.assertEqual(len(failures), 1)
            self.assertIs(raised.exception.__cause__, failures[0])
            self.assertIsInstance(failures[0], FileNotFoundError)
            self.assertEqual(
                raised.exception.__notes__,
                [f"worker: could not mark gone: {self.roster.path}: not a readable regular file"],
            )
        finally:
            if os.path.isdir(self.roster.path):
                os.rmdir(self.roster.path)
                os.replace(backup, self.roster.path)
        entry = self.roster.entries()["worker"]
        self.assertEqual((entry.state, entry.record), (State.RESUMING, original))

    def test_failed_resume_restores_gone_and_keeps_previous_record(self):
        original = self.gone("worker")
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == original.resume:
                return subprocess.CompletedProcess(argv, 1, None, "cannot resume")
            return real_run(argv, **options)

        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch("subprocess.Popen", side_effect=answered(run, original.resume)),
            self.assertRaisesRegex(RuntimeError, "cannot resume"),
        ):
            self.crew.resume("worker")
        entry = self.roster.entries()["worker"]
        self.assertEqual((entry.state, entry.record), (State.GONE, original))

    def test_forget_refuses_live_record_without_closing_or_removing_it(self):
        self.start("worker")
        with self.assertRaisesRegex(RuntimeError, "still alive"):
            self.crew.forget("worker")
        self.assertIn("worker", self.roster.entries())
        self.assertEqual(self.calls("close"), [])

    def test_note_updates_and_release_allows_new_manager_to_attach(self):
        self.start("worker", note="before")
        self.crew.note("worker", "after")
        self.assertEqual(self.roster.entries()["worker"].note, "after")
        self.crew.release()
        other = Crew(Roster(self.directory))
        with self.listed():
            other.attach()

    def test_next_event_retains_every_line_until_explicit_ack(self):
        path = self.roster.events
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("12:00:00 worker done\ndiagnostic\n")
        observed = []

        def wait(delay):
            self.assertEqual(delay, 0.5)
            observed.append(Roster(self.directory).backlog())
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("12:00:01 worker blocked\n")

        with mock.patch("time.sleep", side_effect=wait):
            result = self.crew.next_event(after=2, gen=0)
        self.assertEqual(observed, [(0, 0, ["12:00:00 worker done", "diagnostic"])])
        self.assertEqual(result, (3, "12:00:01 worker blocked"))
        self.assertEqual(len(self.roster.backlog()[2]), 3)
        successor = Crew(Roster(self.directory))
        self.assertEqual(successor.next_event(after=2, gen=0), result)
        self.assertEqual(self.roster.backlog()[0], 0)
        self.roster.advance(2, 0)
        self.assertEqual(self.roster.backlog(), (2, 0, ["12:00:01 worker blocked"]))

    def test_next_event_refuses_replacement_deletion_shrink_and_unsafe_file_while_waiting(self):
        path = self.roster.events
        replacement = os.path.join(self.home, "replacement")
        for action in ("replace", "delete", "shrink", "fifo"):
            with self.subTest(action=action):
                if os.path.lexists(path):
                    os.unlink(path)
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write("diagnostic\n")

                def wait(delay, action=action):
                    self.assertEqual(delay, 0.5)
                    if action == "replace":
                        with open(replacement, "w", encoding="utf-8") as stream:
                            stream.write("diagnostic\n12:00:00 worker done\n")
                        os.replace(replacement, path)
                    elif action == "shrink":
                        with open(path, "w", encoding="utf-8"):
                            pass
                    else:
                        os.unlink(path)
                        if action == "fifo":
                            os.mkfifo(path)

                with (
                    mock.patch("time.sleep", side_effect=wait),
                    self.assertRaises((Stale, RosterError)),
                ):
                    self.crew.next_event(after=1, gen=0)
                self.assertFalse(Path(self.roster.path).exists())

    def test_next_event_refuses_an_event_file_that_becomes_unreadable_while_waiting(self):
        path = self.roster.events
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("diagnostic\n")
        waiting = []
        native_open = os.open

        def open_file(file, *args, **kwargs):
            if file == path and waiting:
                raise PermissionError("events unreadable")
            return native_open(file, *args, **kwargs)

        with (
            mock.patch("time.sleep", side_effect=waiting.append),
            mock.patch("os.open", side_effect=open_file),
            self.assertRaisesRegex(RosterError, "not a readable regular file"),
        ):
            self.crew.next_event(after=1, gen=0)
        self.assertEqual(waiting, [0.5])
        self.assertFalse(Path(self.roster.path).exists())

    def test_attach_filters_diagnostics_but_next_event_reports_their_physical_line(self):
        with open(self.roster.events, "a", encoding="utf-8") as stream:
            stream.write("diagnostic\n12:00:00 worker outcome done\ngarbage\n")
        with self.listed():
            table = self.crew.attach()
        self.assertIn("2 12:00:00 worker outcome done\n", table)
        self.assertNotIn("diagnostic", table)
        self.assertNotIn("garbage", table)
        self.assertTrue(table.endswith("N=0 GEN=0\n"))
        with self.assertRaisesRegex(RosterError, "invalid event at line 1"):
            self.crew.next_event(after=0, gen=0)

    def test_launch_does_not_pass_its_managers_lease_to_the_worker(self):
        with self.roster.hold("owner") as lease:
            crew = Crew(self.roster, lease=lease)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "ENSEMBLE_MANAGER": "parent",
                        "ENSEMBLE_HOLDER_ID": lease.id,
                        "ENSEMBLE_HOLDER_GENERATION": str(lease.generation),
                        "ENSEMBLE_HOLDER_PID": str(os.getpid()),
                    },
                ),
                mock.patch("subprocess.Popen", wraps=subprocess.Popen) as popen,
            ):
                record = crew.run(Spec("worker", (self.command, "start", "worker")))
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["ENSEMBLE_SID"], record.sid)
            for key in (
                "ENSEMBLE_MANAGER",
                "ENSEMBLE_HOLDER_ID",
                "ENSEMBLE_HOLDER_GENERATION",
                "ENSEMBLE_HOLDER_PID",
            ):
                self.assertNotIn(key, env)


class CloseAllTest(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.roster = Roster(os.path.join(self.ensemble, "managers", "mgr"))
        self.crew = Crew(self.roster)

    @contextlib.contextmanager
    def unavailable_listing(self):
        native_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == DESCENDANTS:
                return subprocess.CompletedProcess(argv, 1, "", "listing unavailable")
            return native_run(argv, **options)

        with mock.patch("subprocess.run", side_effect=run):
            yield

    def test_failed_listing_empty_roster_reports_failure_without_creating_state(self):
        stderr = io.StringIO()
        with self.unavailable_listing(), contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(RuntimeError, "close-all partial"):
                self.crew.close_all()
            with self.assertRaisesRegex(RuntimeError, "close-all partial"):
                self.crew.close_all(dry_run=True)
        self.assertEqual(stderr.getvalue().count("listing unavailable"), 2)
        self.assertFalse(Path(self.roster.directory).exists())
        self.assertEqual(ctl.calls(self.ctl_dir), [])
