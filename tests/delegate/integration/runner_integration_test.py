"""Real process behavior in isolated integration fixtures."""

import dataclasses
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from ensemble.delegate import ConfigError, Progress
from ensemble.delegate.engine import runner
from ensemble.delegate.engine.runner import FIX
from ensemble.delegate.engine.types import Begun, Finished, Layout, Limit, Result, Stopped
from ensemble.utils import printable
from tests.delegate.replies import bare, block, running
from tests.delegate.runner import (
    FAILED,
    GH,
    INVALID,
    INVALID_ERROR,
    NEEDS,
    PR,
    SID,
    Base,
    UnstreamedClient,
    ended,
)
from tests.delegate.unit.runs import DONE, START, limit, said
from tests.session.modules import client_loader

UPDRAFT = {"status": "converged", "head": "a" * 40}
VIEW = {"state": "OPEN", "isDraft": True, "headRefOid": "a" * 40, "statusCheckRollup": []}


def fake_gh():
    """subprocess.run answers `gh` as an open draft PR at UPDRAFT's head with no checks; anything
    else runs.
    """
    run = subprocess.run

    def answer(argv, *args, **kw):
        if argv[0] != "gh":
            return run(argv, *args, **kw)
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(VIEW) if argv[2] == "view" else "", ""
        )

    return mock.patch("subprocess.run", side_effect=answer)


class Configures(Base):
    def test_the_callers_settings_reach_the_run(self):
        r = self.go(said(START, block(DONE)))
        self.assertEqual((r.outcome.status, len(self.launches())), ("done", 1))

    def test_a_fresh_driver_without_the_client_starts_nothing(self):
        self.configure_session({})
        with self.assertRaisesRegex(ConfigError, r"\[clients.fake\]"):
            self.go(said(START, block(DONE)))
        self.assertFalse(os.path.exists(self.log))


class Callbacks(Base):
    def test_begun_then_a_progress_per_mark_then_finished(self):
        r = self.go(
            said(START, "Working.", "[ensemble-progress:round] half way"), said(block(DONE))
        )
        sid = self.records()[0]["sid"]
        self.assertEqual(
            self.events,
            [Begun(sid), Progress("start", "go"), Progress("round", "half way"), Finished(r)],
        )
        self.assertEqual((r.returncode, r.outcome.status, r.error, r.limit), (0, "done", "", None))

    def test_a_turn_end_repeating_the_last_reply_is_read_once(self):
        text = "\n".join((START, block(DONE)))
        r = self.go(said(text), ended(text), ended(""))
        self.assertEqual(
            ([type(e) for e in self.events], r.outcome.status),
            ([Begun, Progress, Finished], "done"),
        )
        self.assertEqual((self.kinds().count("progress"), self.err.count(START)), (1, 0))
        self.assertEqual(self.err.count(f"{block(DONE)}\n"), 1)

    def test_a_differing_turn_end_is_a_reply(self):
        r = self.go(said(START), ended("[ensemble-progress:round] two\n" + block(DONE)))
        self.assertEqual(self.events[1:3], [Progress("start", "go"), Progress("round", "two")])
        self.assertEqual(r.outcome.status, "done")

    def test_a_deliverable_file_is_read_in_and_saved_to_out(self):
        self.write(os.path.join(Layout(self.work).agent, "notes", "doc.md"), "# From a file\n")
        data = {k: v for k, v in DONE.items() if k != "deliverable"} | {
            "deliverable_file": "notes/doc.md"
        }
        r = self.go(said(START, block(data)))
        self.assertEqual(
            (r.outcome.status, r.outcome.deliverable, r.outcome.url),
            ("done", "# From a file\n", self.out),
        )
        self.assertEqual(self.read(self.out), "# From a file\n")
        fields = {f.name for f in dataclasses.fields(r.outcome)} - {"deliverable"}
        self.assertEqual(set(self.last("result")["outcome"]), fields)


class Fix(Base):
    """FR-21: one fix resume of the same session after an invalid outcome block, only when the
    client exited 0.
    """

    def test_an_invalid_block_is_fixed_by_one_resume_of_the_session(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(DONE))])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        first, second = (launch["argv"] for launch in self.launches())
        sid = self.events[0].sid
        self.assertEqual((first[first.index("--sid") + 1], "--resume" in first), (sid, False))
        self.assertEqual((second[second.index("--sid") + 1], "--resume" in second), (sid, True))
        self.assertEqual(second[second.index("--") + 1], FIX.format(error=INVALID_ERROR))
        self.assertEqual([type(e) for e in self.events], [Begun, Progress, Finished])
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )
        self.assertEqual(self.last("invalid")["error"], INVALID_ERROR)
        self.assertIn(f"delegate: invalid outcome: {INVALID_ERROR}; resuming once\n", self.err)
        self.assertEqual(self.read(self.out), "# Doc\n")

    def test_an_invalid_block_twice_is_the_error(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(INVALID))])
        self.assertEqual(r, Result(0, None, f"invalid outcome: {INVALID_ERROR}"))
        self.assertEqual(
            (len(self.launches()), [type(e) for e in self.events]), (2, [Begun, Progress, Finished])
        )
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )
        self.assertEqual(bare(self.last("result")), {"kind": "result", "error": r.error})

    def test_no_block_is_the_error_without_a_resume(self):
        r = self.go(said(START, "bye"))
        self.assertEqual(
            (r, len(self.launches())), (Result(0, None, "the agent run returned no outcome"), 1)
        )

    def test_an_invalid_block_from_a_failed_client_is_not_resumed(self):
        r = self.go(said(START, block(INVALID)), {"exit": 3})
        self.assertEqual((r, len(self.launches())), (Result(3, None, "the client exited 3"), 1))
        self.assertNotIn("invalid", self.kinds())

    def test_a_fix_resume_that_cannot_start_is_the_error(self):
        real, calls = subprocess.Popen, []

        def popen(*args, **kw):
            calls.append(args)
            if len(calls) > 1:
                raise FileNotFoundError(2, "No such file or directory", "x")
            return real(*args, **kw)

        with mock.patch("subprocess.Popen", popen):
            r = self.go(said(START, block(INVALID)))
        self.assertEqual((r.returncode, r.outcome), (1, None))
        self.assertTrue(
            r.error.startswith(f"invalid outcome: {INVALID_ERROR}; resume failed: cannot start "),
            r.error,
        )
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )


class Idle(Base):
    def test_silence_longer_than_the_idle_timeout_is_recorded_and_the_run_goes_on(self):
        self.write(
            os.path.join(self.ensemble, "config.local.toml"), "[delegate]\nidle_timeout = 0.3\n"
        )
        r = self.go({"sleep": 1.0}, said(START, block(DONE)))
        self.assertEqual(r.outcome.status, "done")
        idle = [e for e in self.records() if e["kind"] == "idle"]
        self.assertTrue(idle)
        self.assertEqual({e["seconds"] for e in idle}, {0.3})


class UsageLimit(Base):
    """A usage limit stops the session and returns a Finished result without outcome or repair."""

    def test_a_limit_stops_the_session_and_is_the_result(self):
        at = 1767225600.0
        began = time.monotonic()
        r = self.go(said(START), limit(at), {"sleep": 30}, said(block(DONE)))
        self.assertLess(time.monotonic() - began, 20)
        self.assertFalse(running(self.launches()[0]["pid"]))
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        self.assertEqual(
            r, Result(-signal.SIGKILL, None, f"usage limit; resets at {when}", Limit(at))
        )
        self.assertEqual(r.limit.resets_at, at)
        self.assertEqual(self.kinds(), ["session", "input", "progress", "limit", "result", "end"])
        self.assertEqual((self.last("limit")["resets_at"], len(self.launches())), (at, 1))
        self.assertEqual([type(e) for e in self.events], [Begun, Progress, Finished])

    def test_an_unknown_reset_time(self):
        r = self.go(said(START, block(INVALID)), limit(None))
        self.assertEqual(
            (r.outcome, r.error, r.limit), (None, "usage limit; reset time unknown", Limit(None))
        )
        self.assertEqual((self.last("limit")["resets_at"], len(self.launches())), (None, 1))


class Stops(Base):
    """
    Main's HostFailure cases: a session stopped on an exception, its record closed `stopped: …`.
    """

    def test_an_on_event_raise_stops_the_session_and_propagates(self):
        failure = RuntimeError("boom")

        def boom(event):
            if isinstance(event, Progress):
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            self.go(said(START), {"sleep": 30}, said(block(DONE)), on_event=boom)
        self.assertIs(caught.exception, failure)
        self.assertFalse(running(self.launches()[0]["pid"]))
        result, end = self.records()[-2:]
        self.assertEqual((result["error"], end["rc"]), ("stopped: RuntimeError: boom", 1))

    def test_an_exception_ends_the_record_stopped(self):
        for error, rc, text in (
            (Stopped(128 + signal.SIGTERM), 143, "stopped: Stopped: 143"),
            (SystemExit(129), 129, "stopped: SystemExit: 129"),
            (SystemExit("bye"), 1, "stopped: SystemExit: bye"),
            (KeyboardInterrupt(), 1, "stopped: KeyboardInterrupt"),
            (RuntimeError("one\ntwo"), 1, "stopped: RuntimeError: one two"),
        ):

            def raise_(event, error=error):
                if isinstance(event, Progress):
                    raise error

            with self.subTest(error=error):
                self.fresh()
                with self.assertRaises(type(error)) as cm:
                    self.go(said(START), {"sleep": 30}, on_event=raise_)
                self.assertIs(cm.exception, error)
                self.assertFalse(running(self.launches()[-1]["pid"]))
                self.assertEqual(self.kinds(), ["session", "input", "progress", "result", "end"])
                result, end = self.records()[-2:]
                self.assertEqual(
                    (bare(result), bare(end)),
                    (
                        {"kind": "result", "error": text},
                        {"kind": "end", "sid": end["sid"], "rc": rc},
                    ),
                )

    def test_the_closing_writes_never_mask_the_runs_exception(self):
        def swap(_event):
            os.remove(self.channel)
            os.mkdir(self.channel)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.go({"sleep": 30}, on_event=swap)
        self.assertEqual(len(re.findall(r"^delegate: .*run\.jsonl", self.err, re.M)), 2, self.err)

    def test_a_spawn_failure_is_the_result(self):
        with mock.patch(
            "subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", "x")
        ):
            r = self.go(said(START, block(DONE)))
        self.assertEqual((r.returncode, r.outcome, self.events), (1, None, [Finished(r)]))
        self.assertTrue(r.error.startswith("headless: cannot start "), r.error)
        self.assertEqual(self.kinds(), ["session", "input", "result", "end"])
        self.assertEqual((self.last("result")["error"], self.last("end")["rc"]), (r.error, 1))

    def test_a_driver_refusal_is_a_config_error_and_nothing_is_recorded(self):
        with self.assertRaises(ConfigError) as cm:
            self.go(said(START, block(DONE)), client="nope")
        self.assertIn("nope", str(cm.exception))
        self.assertEqual(
            (os.path.lexists(self.channel), os.path.lexists(self.log), self.events),
            (False, False, []),
        )

    def test_a_stream_setup_failure_is_the_result(self):
        self.session_dependencies = {
            "class_loader": client_loader(
                {"ensemble.session.clients.fake": SimpleNamespace(FakeClient=UnstreamedClient)}
            )
        }
        self.configure_session(
            {
                "fake": {
                    "implementation": "ensemble.session.clients.fake:FakeClient",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )
        proc = mock.Mock(stdout=io.StringIO(), stderr=io.StringIO())
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            r = self.go(said(START, block(DONE)))
        popen.assert_called_once()
        error = f"headless: cannot start {sys.executable}: no stream"
        self.assertEqual((r, self.events), (Result(1, None, error), [Finished(r)]))
        self.assertEqual(self.kinds(), ["session", "input", "result", "end"])
        self.assertEqual((self.last("result")["error"], self.last("end")["rc"]), (error, 1))


class Terminal(Base):
    """Main's Sinks case: what reaches the terminal is printable."""

    def test_printable_is_the_utils_one(self):
        self.assertIs(runner.printable, printable)

    def test_replies_progress_and_stderr_lines_are_printed_printable(self):
        self.go(
            {"err": "w\x1b[2Jarn"},
            said(
                "a\x1b]52;c;ZXZpbA==\x07b\tc",
                "",
                "[ensemble-progress:round] x\x1b[2J",
                START,
            ),
            said(block(DONE)),
        )
        lines = self.err.splitlines()
        for want in (
            "w[2Jarn",
            "a]52;c;ZXZpbA==b\tc",
            "[ensemble-progress:round] x[2J",
            "Progress (round): x[2J",
            "Progress (start): go",
        ):
            self.assertIn(want, lines)
        self.assertLess(lines.index("a]52;c;ZXZpbA==b\tc"), lines.index("Progress (round): x[2J"))
        self.assertNotIn("", lines)
        self.assertFalse([c for c in self.err if not c.isprintable() and c not in "\t\n"])


class Start(Base):
    """Main's drive_test Start cases."""

    def test_the_record_is_session_input_the_marks_result_and_end(self):
        r = self.go(said(START, block(DONE)), input="  Answers:\n- B\n")
        records, sid = self.records(), self.events[0].sid
        self.assertEqual(
            [e["kind"] for e in records], ["session", "input", "progress", "result", "end"]
        )
        mine = records[1:]
        self.assertEqual(
            [list(e) for e in mine],
            [
                ["ts", "kind", "text"],
                ["ts", "kind", "name", "text"],
                ["ts", "kind", "outcome"],
                ["ts", "kind", "sid", "rc"],
            ],
        )
        for e in mine:
            self.assertIsNotNone(datetime.fromisoformat(e["ts"]).utcoffset(), e["ts"])
        kept = {k: v for k, v in dataclasses.asdict(r.outcome).items() if k != "deliverable"}
        self.assertEqual(
            [bare(e) for e in mine],
            [
                {"kind": "input", "text": "  Answers:\n- B\n"},
                {"kind": "progress", "name": "start", "text": "go"},
                {"kind": "result", "outcome": kept},
                {"kind": "end", "sid": sid, "rc": 0},
            ],
        )
        self.assertEqual(
            (records[0]["sid"], kept["url"], r.outcome.deliverable), (sid, self.out, "# Doc\n")
        )
        self.assertEqual(
            (sorted(os.listdir(self.work)), os.listdir(Layout(self.work).run)),
            (["agent", "out.md", "run"], ["run.jsonl"]),
        )

    def test_a_destination_decides_the_url_and_each_saves_a_deliverable(self):
        url, pr_url = "https://github.com/o/docs/blob/main/x.md", "https://github.com/o/n/pull/7"
        for output, data, want_url, saved in (
            ({"type": "local"}, DONE, self.out, "# Doc\n"),
            ({"type": "orchestrator"}, DONE, "", "# Doc\n"),
            (GH, {**DONE, "url": url}, url, "# Doc\n"),
            (PR, {**DONE, "url": pr_url, "updraft": UPDRAFT}, pr_url, "# Doc\n"),
            ({"type": "local"}, NEEDS, "", None),
            (GH, NEEDS, "", None),
        ):
            with self.subTest(output=output, status=data["status"]):
                self.fresh()
                with fake_gh():
                    r = self.go(said(START, block(data)), output=output)
                self.assertEqual(r.outcome.status, data["status"])
                self.assertEqual(r.outcome.url, want_url)
                self.assertEqual(
                    self.read(self.out) if saved else os.path.lexists(self.out), saved or False
                )
                self.assertEqual(
                    self.last("result")["outcome"]["questions"], data.get("questions", [])
                )

    def test_a_failed_save_raises_where_out_is_the_deliverables_home_else_is_noted(self):
        denied = PermissionError(13, "Permission denied")
        replace = os.replace

        def replace_out(source, destination):
            if os.fspath(destination) == self.out:
                raise denied
            return replace(source, destination)

        for output in ({"type": "local"}, {"type": "orchestrator"}):
            with self.subTest(output=output):
                self.fresh()
                with mock.patch("os.replace", side_effect=replace_out), self.assertRaises(OSError):
                    self.go(said(START, block(DONE)), output=output)
                self.assertEqual(
                    (self.last("result")["error"], os.path.lexists(self.out)),
                    (f"stopped: PermissionError: {denied}", False),
                )
        for output, url, gated in (
            (GH, "https://github.com/o/docs/blob/main/x.md", []),
            (PR, "https://github.com/o/n/pull/7", ["ready_gate"]),
        ):
            with self.subTest(output=output):
                self.fresh()
                data = {**DONE, "url": url, "updraft": UPDRAFT}
                with mock.patch("os.replace", side_effect=replace_out), fake_gh():
                    r = self.go(said(START, block(data)), output=output)
                self.assertEqual(
                    (r.returncode, r.outcome.status, r.outcome.url, r.error), (0, "done", url, "")
                )
                self.assertEqual(self.events[-1], Finished(r))
                self.assertEqual(
                    self.kinds(),
                    ["session", "input", "progress", "save_error", *gated, "result", "end"],
                )
                self.assertEqual(
                    bare(self.last("save_error")), {"kind": "save_error", "error": str(denied)}
                )
                self.assertIn(
                    f"delegate: cannot save the deliverable to {self.out}: {denied}\n", self.err
                )
                self.assertFalse(os.path.lexists(self.out))

    def test_without_out_the_deliverable_is_saved_in_the_run_dir(self):
        r = self.go(said(START, block(DONE)), out=None)
        run_dir = Layout(self.work).run
        self.assertEqual(
            (r.outcome.status, r.outcome.url, r.outcome.deliverable),
            ("done", os.path.join(run_dir, "out.md"), "# Doc\n"),
        )
        self.assertEqual(
            (sorted(os.listdir(self.work)), sorted(os.listdir(run_dir))),
            (["agent", "run"], ["out.md", "run.jsonl"]),
        )

    def test_a_stale_out_never_stands_for_the_deliverable(self):
        save_spy = self.spy_save()
        self.write(self.out, "# Doc\n")
        seen = []
        self.go(
            said(START, block(DONE)),
            on_event=lambda e: (
                seen.append(os.path.lexists(self.out)) if isinstance(e, Begun) else None
            ),
        )
        self.assertEqual((seen, save_spy.call_count, self.read(self.out)), ([False], 1, "# Doc\n"))

    def test_a_failed_client_does_not_reuse_a_previous_runs_out(self):
        self.go(said(START, block(DONE)))
        r = self.go(said(START), {"exit": 143})
        self.assertEqual(
            (r, os.path.lexists(self.out)), (Result(143, None, "the client exited 143"), False)
        )

    def test_a_driver_refusal_leaves_out_as_it_was(self):
        for name, kw in (
            ("an unknown client", {"client": "nope"}),
            ("a bad sid", {"sid": "a/b"}),
        ):
            with self.subTest(name):
                self.write(self.out, "# Earlier\n")
                with self.assertRaises(ConfigError):
                    self.go(said(START, block(DONE)), **kw)
                self.assertEqual(self.read(self.out), "# Earlier\n")

    def test_a_stale_out_that_cannot_be_removed_stops_the_session_as_the_result(self):
        blocker, procs, popen = os.path.join(self.tmp, "file"), [], subprocess.Popen
        self.write(blocker, "")

        def spawn(*args, **kw):
            procs.append(popen(*args, **kw))
            return procs[-1]

        with mock.patch("subprocess.Popen", side_effect=spawn):
            r = self.go(
                {"sleep": 30}, said(START, block(DONE)), out=os.path.join(blocker, "out.md")
            )
        self.assertEqual((r.returncode, r.outcome, self.events), (1, None, [Finished(r)]))
        self.assertRegex(r.error, r"^cannot remove the stale out: .*Not a directory")
        self.assertEqual(
            (procs[0].returncode, self.kinds()),
            (-signal.SIGKILL, ["session", "input", "result", "end"]),
        )
        self.assertEqual(self.last("result")["error"], r.error)

    def test_a_resume_keeps_the_draft_its_session_wrote_to_out(self):
        save_spy = self.spy_save()
        self.write(self.out, "# Draft\n")
        seen = []
        r = self.go(
            said(block({**DONE, "deliverable": "# Draft\n"})),
            sid=SID,
            resume=True,
            on_event=lambda e: seen.append(self.read(self.out)) if isinstance(e, Begun) else None,
        )
        self.assertEqual(
            (seen, save_spy.call_count, self.read(self.out)), (["# Draft\n"], 0, "# Draft\n")
        )
        argv = self.launches()[0]["argv"]
        self.assertEqual(
            (argv[argv.index("--sid") + 1], "--resume" in argv, r.outcome.status),
            (SID, True, "done"),
        )

    def test_a_deliverable_the_agent_wrote_to_out_is_not_saved_again(self):
        save_spy, written = self.spy_save(), []

        def write(event):
            if isinstance(event, Begun):
                self.write(self.out, "# Doc\n")
                written.append(os.stat(self.out))

        for dest in ("local", "orchestrator"):
            with self.subTest(dest):
                r = self.go(said(START, block(DONE)), output={"type": dest}, on_event=write)
                st = os.stat(self.out)
                self.assertEqual(
                    (st.st_ino, st.st_mtime_ns), (written[-1].st_ino, written[-1].st_mtime_ns)
                )
                self.assertEqual((save_spy.called, self.read(self.out)), (False, "# Doc\n"))
                self.assertEqual(
                    (r.outcome.url, r.outcome.deliverable),
                    (self.out if dest == "local" else "", "# Doc\n"),
                )

    def test_output_replaces_unusable_files_without_following_or_waiting(
        self,
    ):
        save_spy, target = self.spy_save(), os.path.join(self.tmp, "zshrc")

        def write(text):
            return lambda: self.write(self.out, text)

        def link(text):
            def plant():
                self.write(target, text)
                os.symlink(target, self.out)

            return plant

        cases = [(f"differs: {w!r}", write(w), None) for w in ("", "# Do", "# Doc\n\n", "# Dac\n")]
        cases += [
            ("fifo", lambda: os.mkfifo(self.out), None),
            ("symlink", link("mine"), "mine"),
        ]
        for name, plant, kept in cases:
            with self.subTest(name):
                save_spy.reset_mock()
                self.go(
                    said(START, block(DONE)),
                    on_event=lambda e, plant=plant: plant() if isinstance(e, Begun) else None,
                )
                self.assertEqual(
                    (
                        save_spy.call_count,
                        stat.S_ISREG(os.lstat(self.out).st_mode),
                        self.read(self.out),
                    ),
                    (1, True, "# Doc\n"),
                )
                if kept is not None:
                    self.assertEqual(self.read(target), kept)

    def test_a_valid_outcome_survives_a_nonzero_exit_with_its_diagnostic(self):
        r = self.go(said(START, block(DONE)), {"exit": 7})
        self.assertEqual(
            (r.returncode, r.outcome.status, r.error), (7, "done", "the client exited 7")
        )
        self.assertEqual(
            ([type(e) for e in self.events], self.read(self.out)),
            ([Begun, Progress, Finished], "# Doc\n"),
        )
        saved = self.last("result")
        self.assertEqual(
            (saved["outcome"]["url"], saved["error"], self.last("end")["rc"]),
            (self.out, r.error, 7),
        )

    def test_a_run_without_a_valid_outcome_records_its_error_and_finishes(self):
        for steps, rc, error in (
            ([said("bye")], 0, "the agent run returned no outcome"),
            ([said("bye"), {"exit": 143}], 143, "the client exited 143"),
            ([said(block(INVALID)), {"exit": 143}], 143, "the client exited 143"),
        ):
            with self.subTest(steps=steps):
                self.fresh()
                r = self.go(*steps)
                self.assertEqual(
                    (r, [type(e) for e in self.events]),
                    (Result(rc, None, error), [Begun, Finished]),
                )
                result, end = self.records()[-2:]
                self.assertEqual(
                    (bare(result), bare(end)),
                    (
                        {"kind": "result", "error": error},
                        {"kind": "end", "sid": self.events[0].sid, "rc": rc},
                    ),
                )

    def test_done_or_failed_without_start_reports_it_missing(self):
        for data in (DONE, FAILED):
            with self.subTest(data["status"]):
                self.fresh()
                r = self.go(said("[ensemble-progress:round] one", block(data)))
                self.assertEqual(r.outcome.status, data["status"])
                self.assertIn("delegate: missing progress mark: start\n", self.err)
                self.assertEqual(
                    self.kinds(), ["session", "input", "progress", "missing", "result", "end"]
                )
                self.assertEqual(bare(self.last("missing")), {"kind": "missing", "name": "start"})

    def test_no_missing_report_when_start_was_seen_needs_input_resumed_or_no_outcome(self):
        for steps, kw in (
            ([said(START, block(DONE))], {}),
            ([said(block(NEEDS))], {}),
            ([said(block(DONE))], {"sid": SID, "resume": True}),
            ([said("bye")], {}),
        ):
            with self.subTest(steps=steps, **kw):
                self.fresh()
                self.go(*steps, **kw)
                self.assertNotIn("missing", self.err)
                self.assertNotIn("missing", self.kinds())
