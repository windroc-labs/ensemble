"""ensemble-session's batch commands through bin/ensemble-session on a private tmux server: status
answers each name or stdin Record in order (Q11), using answer_keys batch rules;
show, hide and close by name and by stdin Record, show placing a
session by its own display or by a Record's, close repeatable and only for the named (Q12), show
and close checking private session identity; on a Host that cannot show
(tests.session.fakes.host:FakeHost, the module entry) show, hide and tile exit 1
saying so, while read and status, which read workers through their harness and worker dir, find no
session in its process outside a harness.
"""

import dataclasses
import subprocess
import sys
import uuid

from ensemble.session.engine.types import Launch
from ensemble.utils import Record, Status
from tests.session import live_tmux
from tests.session.cli_support import COMMAND, MANAGER, CliCase, guard_command
from tests.session.fakes.host import FakeHost
from tests.utils.fakes.ctl import lines
from tests.utils.sandbox import pid_exists as pid_alive

PROG = "ensemble-session"
OTHER = "other"
SLEEP = "import time; time.sleep(120)"


def absent(name: str) -> Record:
    return Record(
        v=1,
        kind="worker",
        name=name,
        sessions=(name,),
        sid=None,
        workdir=None,
        ctl=(PROG,),
        resume=(PROG, "resume", name),
        display=None,
        detail=None,
    )


def gone(name: str) -> Status:
    return Status(name=name, alive=False, state="gone", outcome=None, attach=None, hidden=False)


def live(name: str, state: str, *, cwd: str | None = None, hidden: bool = False) -> Status:
    """A live worker's Status, including its guard attach command."""
    return Status(
        name=name,
        alive=True,
        state=state,
        outcome=None,
        attach=guard_command(name),
        cwd=cwd,
        hidden=hidden,
    )


class ControlCase(CliCase):
    def sessions(self) -> list[str]:
        result = self.server.tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(result.returncode, 0, result.stderr)
        return sorted(result.stdout.split())

    def tmux(self, *args: str) -> None:
        result = self.server.tmux(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def done(self, name: str) -> Record:
        record = self.record(self.start(name))
        self.wait_event(name, "done")
        return record

    def assert_printed(self, result, stdout: str) -> None:
        self.ok(result)
        self.assertEqual(result.stdout, stdout)


class ContentTest(ControlCase):
    def test_done_content_prints_the_last_reply(self):
        record = self.done("w1")
        for keys, stdin in ((("w1",), None), ((), lines(record))):
            with self.subTest(keys=keys):
                self.assert_printed(self.session("content", *keys, stdin=stdin), "Hi from w1.\n")

    def test_invalid_or_missing_or_multiple_keys_answer_nothing(self):
        record = self.done("w1")
        for keys, stdin in (
            ((), ""),
            (("w1", "w2"), ""),
            ((), lines(record, absent("w2"))),
            ((), "bad Record"),
            ((), lines(record) + "bad Record"),
        ):
            with self.subTest(keys=keys, stdin=stdin):
                result = self.session("content", *keys, stdin=stdin)
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertTrue(result.stderr)
        self.assertTrue(self.status("w1")[0].alive)


class StatusTest(ControlCase):
    def test_status_answers_each_name_or_record_in_input_order(self):
        w1 = self.done("w1")
        done = live("w1", "done", cwd=self.cwd)
        for argv, stdin, expected in (
            (("w1", "nope"), None, [done, gone("nope")]),
            (("nope", "w1"), None, [gone("nope"), done]),
            ((), lines(w1, absent("nope")), [done, gone("nope")]),
            ((), lines(absent("nope"), w1), [gone("nope"), done]),
        ):
            with self.subTest(argv=argv, stdin=stdin):
                result = self.session("status", *argv, stdin=stdin)
                self.assert_printed(result, "".join(s.to_line() + "\n" for s in expected))
                self.assertEqual(
                    [Status.parse(line) for line in result.stdout.splitlines()], expected
                )
        self.assertEqual(self.sessions(), [MANAGER, "w1"])
        self.assertEqual(len(self.calls("w1")), 1)

    def test_a_duplicate_or_a_bad_stdin_line_refuses_the_whole_batch_before_any_action(self):
        w1 = self.done("w1")
        pane = self.server.pane_of("w1")
        line = w1.to_line() + "\n"
        for cmd in ("status", "show", "close"):
            for argv, stdin, stderr in (
                (("w1", "w1"), None, f"{PROG}: name w1 given more than once\n"),
                ((), line + line, f"{PROG}: name w1 given more than once\n"),
                ((), line + "not json\n", f"{PROG}: stdin line 2: Record line: not JSON"),
                ((), line + '{"v": 1}\n', f"{PROG}: stdin line 2: kind: missing\n"),
            ):
                with self.subTest(cmd=cmd, argv=argv, stdin=stdin):
                    result = self.session(cmd, *argv, stdin=stdin)
                    self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
                    self.assertTrue(result.stderr.startswith(stderr), result.stderr)
                    self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertEqual(self.sessions(), [MANAGER, "w1"])
        self.assertEqual(self.server.pane_of("w1"), pane)
        self.assertEqual(self.status("w1"), [live("w1", "done", cwd=self.cwd)])
        self.assertEqual(len(self.calls("w1")), 1)

    def test_a_record_resolution_error_keeps_valid_keys(self):
        record = self.done("w1")
        nowhere = dataclasses.replace(record, name="w9", sessions=())
        result = self.session("close", stdin=lines(record, nowhere))
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertEqual(result.stderr, f"{PROG}: w9: names no session\n")
        self.assertEqual(self.sessions(), [MANAGER])

    def test_stdin_that_is_not_utf_8_is_a_usage_error(self):
        result = subprocess.run(
            [COMMAND, "status"],
            input=b"\xff\n",
            capture_output=True,
            env=self.env(),
            cwd=self.cwd,
            timeout=60,
        )
        self.assertEqual((result.returncode, result.stdout), (2, b""), result.stderr)
        self.assertTrue(result.stderr.startswith(f"{PROG}: stdin line 1: ".encode()), result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)


class ControlTest(ControlCase):
    def test_close_closes_only_the_named_sessions_and_again_is_fine(self):
        records = {name: self.done(name) for name in ("w1", "w2", "w3")}
        pids = {name: self.calls(name)[0]["pid"] for name in records}
        for _ in range(2):
            self.assert_printed(self.session("close", "w1"), "")
            self.assert_printed(self.session("close", stdin=lines(records["w2"])), "")
        self.assertEqual(self.sessions(), [MANAGER, "w3"])
        for name in ("w1", "w2"):
            live_tmux.wait(lambda name=name: not pid_alive(pids[name]), what=f"{name} to end")
        self.assertTrue(pid_alive(pids["w3"]))
        self.assertEqual(
            self.status("w1", "w2", "w3"),
            [gone("w1"), gone("w2"), live("w3", "done", cwd=self.cwd)],
        )

    def test_record_operations_check_private_sid(self):
        w1 = self.done("w1")
        pid = self.calls("w1")[0]["pid"]
        self.tmux("kill-pane", "-t", self.server.pane_of("w1"))
        # its client-detached hook marks it hidden and drops its @pane
        live_tmux.wait(lambda: self.status("w1")[0].hidden, what="w1 hidden")
        pane = self.server.pane_of("w1")
        reused = dataclasses.replace(w1, sid=str(uuid.uuid4()))
        for verb in ("show", "hide", "content"):
            with self.subTest(verb=verb):
                res = self.session(verb, stdin=lines(reused))
                self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
                self.assertEqual(
                    res.stderr,
                    f"{PROG}: w1: session w1: expected sid {reused.sid!r}, found {w1.sid!r}\n",
                )
        self.assert_printed(self.session("close", stdin=lines(reused)), "")
        self.assertEqual(self.sessions(), [MANAGER, "w1"])
        self.assertTrue(pid_alive(pid))
        self.assertEqual(self.server.pane_of("w1"), pane)
        self.tmux("set-option", "-u", "-t", "=w1:", "@record")
        self.assert_printed(self.session("close", stdin=lines(w1)), "")
        self.assertEqual(self.sessions(), [MANAGER])

    def shows(self, name: str, window: str) -> None:
        """name's @pane is in `window` and runs a client of name."""
        pane = self.server.pane_of(name)
        self.assertIn(pane, self.server.panes(window))
        tty = self.server.tmux("display-message", "-p", "-t", pane, "#{pane_tty}").stdout.strip()

        def attached():
            found = self.server.tmux("list-clients", "-t", f"={name}", "-F", "#{client_tty}")
            return tty in found.stdout.split()

        live_tmux.wait(attached, what=f"{pane} to attach {name}")

    def test_show_places_a_session_by_its_own_display_or_by_a_records(self):
        self.tmux("new-session", "-d", "-s", OTHER, "-x", "200", "-y", "50", *live_tmux.IDLE)
        self.server.attach(OTHER)
        w1 = self.done("w1")
        self.assertEqual(w1.display, MANAGER)
        self.shows("w1", f"={MANAGER}:")
        self.tmux("kill-pane", "-t", self.server.pane_of("w1"))
        moved = dataclasses.replace(w1, display=OTHER)
        self.assert_printed(self.session("show", stdin=lines(moved)), "")
        self.assertEqual(self.server.option("w1", "@opener"), OTHER)
        self.shows("w1", f"={OTHER}:")
        self.assert_printed(self.session("show", "w1"), "")
        self.assertEqual(self.server.option("w1", "@opener"), MANAGER)
        self.shows("w1", f"={MANAGER}:")
        self.assertEqual(len(self.calls("w1")), 1)

    def test_hide_closes_the_shown_pane_keeps_the_session_and_show_reopens_it(self):
        w1 = self.done("w1")
        self.shows("w1", f"={MANAGER}:")
        pane = self.server.pane_of("w1")
        for names, stdin in ((("w1",), None), ((), lines(w1))):
            self.assert_printed(self.session("hide", *names, stdin=stdin), "")
            self.assertNotIn(pane, self.server.panes(f"={MANAGER}:"))
            self.assertEqual(self.sessions(), [MANAGER, "w1"])
            self.assertEqual(
                self.status(stdin=lines(w1)), [live("w1", "done", cwd=self.cwd, hidden=True)]
            )
        self.assert_printed(self.session("show", stdin=lines(w1)), "")
        self.shows("w1", f"={MANAGER}:")
        self.assertEqual(self.status("w1"), [live("w1", "done", cwd=self.cwd)])
        self.assertEqual(len(self.calls("w1")), 1)

    def test_hide_of_an_absent_session_fails_with_its_reason(self):
        res = self.session("hide", "nope")
        self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
        self.assertEqual(res.stderr, f"{PROG}: nope: no tmux worker nope\n")

    def test_a_key_its_host_refuses_fails_alone_and_the_batch_goes_on(self):
        w1, w2 = self.done("w1"), self.done("w2")
        self.tmux("kill-pane", "-t", self.server.pane_of("w2"))
        bad = dataclasses.replace(w1, display="a/b/c/d")
        res = self.session("show", stdin=lines(bad, w2))
        self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
        self.assertEqual(
            res.stderr,
            f"{PROG}: w1: display 'a/b/c/d': not <opener>[/<split>[/<split_from>[/hidden]]]\n",
        )
        self.shows("w2", f"={MANAGER}:")


class NotShowableTest(ControlCase):
    def setUp(self):
        super().setUp()
        host = FakeHost("fake", {"root": self.fake_host(default="fake")})
        self.f1 = dataclasses.replace(absent("f1"), display=MANAGER)
        process = host.run_terminal(
            "f1",
            Launch((sys.executable, "-I", "-c", SLEEP), cwd=self.cwd),
            display=MANAGER,
            record=self.f1,
        )
        self.addCleanup(process.close)

    def test_show_hide_and_tile_exit_1_saying_the_host_cannot_show(self):
        refused = "session f1: host fake cannot show\n"
        batch_refused = f"{PROG}: f1: {refused}"
        for argv, stdin, stderr in (
            (("show", "f1"), None, batch_refused),
            (("show",), lines(self.f1), batch_refused),
            (("hide", "f1"), None, batch_refused),
            (("tile", MANAGER), None, f"session {MANAGER}: host fake cannot show\n"),
        ):
            with self.subTest(argv=argv, stdin=stdin):
                result = self.session(*argv, stdin=stdin, module=True)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", stderr))

    def test_read_and_status_find_no_session_in_a_host_process_outside_a_harness(self):
        for argv in (("read", "f1"), ("read", "f1", "--lines", "5")):
            with self.subTest(argv=argv):
                result = self.session(*argv, module=True)
                self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
                self.assertTrue(
                    result.stderr.startswith(f"{PROG}: no tui session f1: "), result.stderr
                )
        self.assertEqual(self.status("f1", "nope", module=True), [gone("f1"), gone("nope")])
        self.assertEqual(
            self.status(stdin=lines(absent("nope"), self.f1), module=True),
            [gone("nope"), gone("f1")],
        )
