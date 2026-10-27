"""Crew's installed command talks only to executable fakes."""

import json
import os
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from ensemble.manager import Crew, Roster, Spec, State
from ensemble.utils import Record, Status
from tests.manager import fake_session
from tests.manager.case import Case, child_env, with_ctl
from tests.manager.roster_case import mark
from tests.utils.fakes import ctl


def _without_manager_identity():
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "ENSEMBLE_MANAGER",
            "ENSEMBLE_HOLDER_ID",
            "ENSEMBLE_HOLDER_GENERATION",
            "ENSEMBLE_HOLDER_PID",
        }
    }


class Commands(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.sessions = os.path.join(self.home, "session-list")
        self.crew_bin = self.plugin_root()
        fake_session.install(os.path.dirname(self.crew_bin), self.sessions)
        self.directory = os.path.join(self.ensemble, "managers", "mgr")

    def invoke(self, *argv, clean=False):
        env = dict(os.environ)
        if clean:
            env.pop("PYTHONPATH", None)
        return subprocess.run(
            (self.crew_bin, *argv),
            cwd=self.home,
            env=child_env(env),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def succeeded(self, *argv):
        result = self.invoke(*argv)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_help_from_an_unrelated_directory_without_pythonpath(self):
        result = self.invoke("--help", clean=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {
            "hold",
            "run",
            "worker",
            "reopen",
            "issue",
            "role",
            "attach",
            "status",
            "hide",
            "show",
            "resume",
            "close",
            "close-all",
            "forget",
            "note",
            "report",
            "next-event",
            "pending",
            "ack",
            "release",
        }
        self.assertEqual(set(result.stdout.split("{")[1].split("}")[0].split(",")), expected)
        self.assertFalse(Path(self.directory).exists())

    def test_unknown_and_removed_commands_exit_two(self):
        for args in (
            ("unknown",),
            ("start",),
            ("attach", "--resume"),
            ("run", "r"),
            ("resume", "--all"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(*args).returncode, 2)

    def test_invalid_manager_names_from_flag_and_environment_write_nowhere(self):
        env = _without_manager_identity()
        outside = Path(self.home, "outside")
        before = {str(path.relative_to(self.home)) for path in Path(self.home).rglob("*")}
        invalid = (
            ".",
            "..",
            "../outside",
            "../../outside",
            str(outside),
            "a/b",
            "a\\b",
            "name space",
            "name\n",
        )
        with mock.patch.dict(os.environ, env, clear=True):
            for source in ("flag", "environment"):
                for name in invalid:
                    with self.subTest(source=source, name=name):
                        if source == "flag":
                            result = self.invoke("attach", f"--manager={name}")
                        else:
                            with mock.patch.dict(os.environ, {"ENSEMBLE_MANAGER": name}):
                                result = self.invoke("attach")
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("manager name", result.stderr)
                        self.assertEqual(result.stdout, "")
                        after = {
                            str(path.relative_to(self.home)) for path in Path(self.home).rglob("*")
                        }
                        self.assertEqual(after, before)
                        self.assertFalse(Path(self.ensemble, "managers").exists())
                        self.assertFalse(outside.exists())

    def test_missing_manager_argument_and_environment_exit_two_without_writes(self):
        env = _without_manager_identity()
        with mock.patch.dict(os.environ, env, clear=True):
            result = self.invoke("attach")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--manager NAME", result.stderr)
        self.assertIn("ENSEMBLE_MANAGER", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(Path(self.ensemble, "managers").exists())

    def test_valid_manager_names_work_from_flag_and_environment(self):
        env = _without_manager_identity()
        names = ("a", "A_9-z", "_leading", "9", "-dash", "trailing-")
        with mock.patch.dict(os.environ, env, clear=True):
            for source in ("flag", "environment"):
                for name in names:
                    with self.subTest(source=source, name=name):
                        alias = f"sample-{source}"
                        command = ("--", self.command, "start", f"{source}-{name}")
                        if source == "flag":
                            result = self.invoke("run", alias, f"--manager={name}", *command)
                        else:
                            with mock.patch.dict(os.environ, {"ENSEMBLE_MANAGER": name}):
                                result = self.invoke("run", alias, *command)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        path = Path(self.ensemble, "managers", name, "roster.json")
                        self.assertTrue(path.is_file())
                        record = Record.parse(result.stdout)
                        self.assertEqual(
                            json.loads(path.read_text())["entries"][alias]["record"],
                            json.loads(record.to_line()),
                        )

    def test_run_status_resume_and_close_with_executable_fake(self):
        self.succeeded("attach", "--manager", "mgr")
        raw = self.succeeded(
            "run",
            "r",
            "--manager",
            "mgr",
            "--note",
            "kept",
            "--",
            self.command,
            "start",
            "worker",
            "--display",
            "opaque-value",
        )
        record = Record.parse(raw)
        roster = Roster(self.directory)
        self.assertEqual(roster.entries()["r"].record, record)
        self.assertEqual(roster.entries()["r"].note, "kept")
        self.assertEqual(record.display, "opaque-value")
        ctl.set_session(self.ctl_dir, "worker", alive=False)
        attached = self.succeeded("attach", "--manager", "mgr")
        self.assertIn("ensemble-crew resume --manager mgr r", attached)
        self.assertEqual(roster.entries()["r"].state, State.GONE)
        resumed = Record.parse(self.succeeded("resume", "r", "--manager", "mgr"))
        self.assertEqual(resumed.display, record.display)
        self.assertEqual(resumed.resume, record.resume)
        status = Status.parse(self.succeeded("status", "r", "--manager", "mgr"))
        self.assertEqual(status.name, "r")
        self.assertTrue(status.alive)
        self.succeeded("close", "r", "--manager", "mgr")
        self.assertEqual(roster.entries(), {})

    def test_resume_continues_after_client_failure_and_exits_one(self):
        roster = Roster(self.directory)
        first = with_ctl(self.fake_record("a"), "ensemble-delegate")  # not on PATH
        ctl.set_session(self.ctl_dir, "a", alive=False, sid=first.sid)
        roster.begin(Spec("a", (self.command, "start", "a")), sid=first.sid)
        roster.commit("a", first)
        mark(roster, "a", State.GONE)
        second = Crew(roster).run(Spec("b", (self.command, "start", "b")))
        ctl.set_session(self.ctl_dir, "b", alive=False)
        mark(roster, "b", State.GONE)
        with mock.patch.dict(os.environ, _without_manager_identity(), clear=True):
            result = self.invoke("resume", "a", "b", "--manager", "mgr")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ensemble-delegate", result.stderr)
        self.assertEqual([Record.parse(line) for line in result.stdout.splitlines()], [second])
        entries = roster.entries()
        self.assertEqual((entries["a"].state, entries["a"].record), (State.GONE, first))
        self.assertEqual((entries["b"].state, entries["b"].record), (State.LIVE, second))
        self.assertEqual(entries["b"].sid, second.sid)
        self.assertEqual(
            [call["argv"] for call in ctl.calls(self.ctl_dir) if call["argv"][0] == "resume"],
            [["resume", "b"]],
        )

    def test_status_batches_two_keys_and_skips_show_for_invisible_hosts(self):
        crew = Crew(Roster(self.directory))
        for name in ("a", "b"):
            crew.run(Spec(name, (self.command, "start", name)))
        self.succeeded("attach", "--manager", "mgr")
        lines = self.succeeded("status", "a", "b", "--manager", "mgr").splitlines()
        self.assertEqual([Status.parse(line).name for line in lines], ["a", "b"])
        calls = ctl.calls(self.ctl_dir)
        self.assertEqual(len([call for call in calls if call["argv"] == ["status"]]), 2)
        self.assertEqual([call for call in calls if call["argv"] == ["show"]], [])

    def test_close_all_closes_roster_and_listed_orphans(self):
        crew = Crew(Roster(self.directory))
        crew.run(Spec("a", (self.command, "start", "a")))
        orphan = self.fake_record("orphan")
        ctl.set_session(self.ctl_dir, "orphan", alive=True)
        fake_session.set_records(self.sessions, [orphan])
        self.succeeded("close-all", "--manager", "mgr")
        closed = [call["names"] for call in ctl.calls(self.ctl_dir) if call["argv"] == ["close"]]
        self.assertEqual(closed, [["a", "orphan"]])
        self.assertEqual(crew.roster.entries(), {})

    def test_a_lost_launch_is_adopted_once_and_release_supports_handoff(self):
        orphan = self.fake_record("orphan")
        ctl.set_session(self.ctl_dir, "orphan", alive=True)
        fake_session.set_records(self.sessions, [orphan])
        original = Roster(self.directory)
        original.begin(Spec("orphan", (self.command, "start", "orphan")), sid=orphan.sid)
        mark(original, "orphan", State.LOST)
        with original.hold("first") as first:
            result = self.invoke("attach", "--manager", "mgr")
            self.assertEqual(result.returncode, 1)
            self.assertIn("held by first", result.stderr)
            with mock.patch.dict(
                os.environ,
                {
                    "ENSEMBLE_HOLDER_ID": first.id,
                    "ENSEMBLE_HOLDER_GENERATION": str(first.generation),
                },
            ):
                self.succeeded("attach", "--manager", "mgr")
                self.succeeded("release", "--manager", "mgr")
            with Roster(self.directory).hold("second") as second:
                with mock.patch.dict(
                    os.environ,
                    {
                        "ENSEMBLE_HOLDER_ID": second.id,
                        "ENSEMBLE_HOLDER_GENERATION": str(second.generation),
                    },
                ):
                    self.succeeded("attach", "--manager", "mgr")
                self.assertGreater(second.generation, first.generation)
                with mock.patch.dict(
                    os.environ,
                    {
                        "ENSEMBLE_HOLDER_ID": first.id,
                        "ENSEMBLE_HOLDER_GENERATION": str(first.generation),
                    },
                ):
                    result = self.invoke("note", "orphan", "stale write", "--manager", "mgr")
                self.assertEqual(result.returncode, 1)
                self.assertIn("stale holder generation", result.stderr)
        self.assertEqual(set(Roster(self.directory).entries()), {"orphan"})
        listing = Path(self.sessions, "calls.jsonl").read_text().splitlines()
        self.assertTrue(all(json.loads(line) == ["ls", "--here", "--records"] for line in listing))

    def test_note_cursor_and_next_event_only_print_event_lines(self):
        crew = Crew(Roster(self.directory))
        crew.run(Spec("a", (self.command, "start", "a")))
        self.succeeded("note", "a", "new", "--manager", "mgr")
        self.assertEqual(crew.roster.entries()["a"].note, "new")
        Path(crew.roster.events).write_text("12:00:00 a done\n")
        self.assertEqual(
            self.succeeded("next-event", "--manager", "mgr", "--after", "0", "--gen", "0"),
            "1 12:00:00 a done\n",
        )
        self.succeeded("ack", "--manager", "mgr", "--through", "1", "--gen", "0")
        self.assertEqual(crew.roster.backlog(), (1, 0, []))
        with open(crew.roster.events, "a", encoding="utf-8") as stream:
            stream.write("diagnostic\n12:00:01 a blocked\ngarbage\n")
        attached = self.succeeded("attach", "--manager", "mgr")
        self.assertIn("3 12:00:01 a blocked\n", attached)
        self.assertNotIn("diagnostic", attached)
        self.assertNotIn("garbage", attached)
        result = self.invoke(
            "next-event", "--manager", "mgr", "--after", "1", "--gen", "0", "--follow"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(f"{crew.roster.events_path}: invalid event at line 2", result.stderr)
        self.assertNotIn("diagnostic", result.stderr)

    def test_attach_prints_one_roster_resume_hint_and_monitoring_before_trailer(self):
        crew = Crew(Roster(self.directory))
        crew.run(Spec("needs resume", (self.command, "start", "actual")))
        ctl.set_session(self.ctl_dir, "actual", alive=False)
        Path(crew.roster.events).write_text("noise\n12:00:00 actual dead\n")
        output = self.succeeded("attach", "--manager", "mgr")
        resume = shlex.join(("ensemble-crew", "resume", "--manager", "mgr", "needs resume"))
        self.assertEqual(output.count(resume), 1)
        self.assertNotIn("ensemble-orchestrator resume", output)
        self.assertIn("2 12:00:00 actual dead\n", output)
        self.assertNotIn("noise", output)
        self.assertIn(
            "monitor: ensemble-crew next-event --manager mgr --after 0 --gen 0 --content --follow\n",
            output,
        )
        self.assertIn("monitor expired: ensemble-crew attach --manager mgr\n", output)
        self.assertIn(
            "next-event: ensemble-crew next-event --manager mgr --after 0 --gen 0 --content\n",
            output,
        )
        self.assertTrue(output.endswith("N=0 GEN=0\n"))

    def test_plain_shell_background_reader_retains_completion_for_manual_handling(self):
        roster = Roster(self.directory)
        Path(roster.events).write_text("12:00:00 worker done\n")
        output = Path(self.home, "reader.stdout")
        reader = shlex.join(
            (self.crew_bin, "next-event", "--manager", "mgr", "--after", "0", "--gen", "0")
        )
        script = f"{reader} > {shlex.quote(str(output))} & wait $!"
        result = subprocess.run(
            ("/bin/sh", "-c", script),
            cwd=self.home,
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("worker done", output.read_text())
        self.assertEqual(roster.backlog(), (0, 0, ["12:00:00 worker done"]))
        self.assertIn("worker done", self.succeeded("pending", "--manager", "mgr", "--content"))
        for _ in range(2):
            self.succeeded("ack", "--manager", "mgr", "--through", "1", "--gen", "0")
        self.assertEqual(roster.backlog(), (1, 0, []))

    def test_follow_delivers_every_event_across_ack_close_and_issue_processes(self):
        self._follow_event_sequence(locked=False)

    def test_follow_delivers_every_event_with_locked_writers(self):
        self._follow_event_sequence(locked=True)

    def _follow_event_sequence(self, *, locked):
        # Keep the real crew/roster CLI; only the downstream board/client is fake.
        starter = Path(self.bin_dir, self.command)
        saved = starter.with_name("fake-ctl")
        starter.rename(saved)
        starter.write_text(
            '#!/bin/sh\nif [ "$1" = router ]; then\n'
            f'  exec {shlex.quote(str(saved))} start "$3"\nfi\n'
            f'exec {shlex.quote(str(saved))} "$@"\n'
        )
        starter.chmod(0o755)
        for worker in ("one", "two"):
            self.succeeded("run", worker, "--manager", "mgr", "--", self.command, "start", worker)
        path = Path(Roster(self.directory).events)
        path.write_text("12:00:00 history done\n" * 3320)
        identity = path.stat().st_ino

        def append(text):
            result = subprocess.run(
                (
                    sys.executable,
                    "-P",
                    "-S",
                    "-c",
                    "import sys; from ensemble.utils import append_line; "
                    "append_line(sys.argv[1], sys.argv[2], locked=sys.argv[3] == 'True')",
                    str(path),
                    text,
                    str(locked),
                ),
                env=child_env(),
                cwd=self.home,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        reader = subprocess.Popen(
            (
                self.crew_bin,
                "next-event",
                "--manager",
                "mgr",
                "--after",
                "3320",
                "--gen",
                "0",
                "--follow",
            ),
            cwd=self.home,
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        received = b""

        def expect(line):
            nonlocal received
            expected = line.encode()
            deadline = time.monotonic() + 10
            while len(received) < len(expected):
                remaining = max(0, deadline - time.monotonic())
                self.assertTrue(
                    select.select([reader.stdout], [], [], remaining)[0],
                    f"follow missed {line!r}; file has {path.read_bytes().count(10)} lines",
                )
                block = os.read(reader.stdout.fileno(), 65536)
                self.assertTrue(block, f"follow exited: {reader.poll()}")
                received += block
            self.assertEqual(received[: len(expected)], expected)
            received = received[len(expected) :]

        try:
            for number in range(3321, 3326):
                event = f"23:51:06 worker-{number} done\n"
                append(event)
                expect(f"{number} {event}")
            self.succeeded("ack", "--manager", "mgr", "--through", "3325", "--gen", "0")
            for worker in ("one", "two"):
                self.succeeded("close", worker, "--manager", "mgr")
            for issue in ("TASK-852", "TASK-853"):
                self.succeeded("issue", "--manager", "mgr", issue)
            # A partial final append must wait, then become exactly one physical line.
            append("23:56:58 engineer-TASK-850-5d2a1a93 done")
            self.assertFalse(select.select([reader.stdout], [], [], 0.6)[0])
            append("\n")
            expect("3326 23:56:58 engineer-TASK-850-5d2a1a93 done\n")
            self.assertEqual(received, b"")
            self.assertFalse(select.select([reader.stdout], [], [], 0.6)[0])
            self.assertIsNone(reader.poll())
            self.assertEqual(path.stat().st_ino, identity)
            self.assertEqual(path.read_bytes().count(b"\n"), 3326)
            self.assertEqual(set(Roster(self.directory).entries()), {"TASK-852", "TASK-853"})
            self.assertEqual(
                Roster(self.directory).backlog(),
                (3325, 0, ["23:56:58 engineer-TASK-850-5d2a1a93 done"]),
            )
        finally:
            reader.terminate()
            _, stderr = reader.communicate(timeout=10)
        self.assertEqual(stderr, b"")
