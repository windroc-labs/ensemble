"""ensemble's manager side: hold, list and resume, and the recovery text crew points at."""

import contextlib
import fcntl
import io
import json
import os
import pty
import select
import signal
import struct
import termios
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.manager import Roster, Spec, State, ensemble_command
from ensemble.manager.ensemble_command import main
from ensemble.utils import Record
from tests.manager.cli_case import CliCase
from tests.manager.roster_case import mark

TAKEOVER = (
    "only the user takes over, on purpose:"
    " ensemble --force mgr -- <client resume command> (ensemble --help)"
)


class EnsembleCommandCase(CliCase):
    def invoke(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(argv)
        return result, stdout.getvalue(), stderr.getvalue()


class RecoveryTest(EnsembleCommandCase):
    def test_the_recovery_command_is_ensemble_with_the_name(self):
        self.assertEqual(
            ensemble_command.recovery("demo"),
            "ensemble demo -- <client resume command> (ensemble --help)",
        )
        self.assertEqual(
            ensemble_command.recovery("demo", force=True),
            "ensemble --force demo -- <client resume command> (ensemble --help)",
        )

    def test_a_takeover_names_the_holder_and_leaves_it_to_the_user(self):
        self.assertEqual(ensemble_command.takeover("mgr", "new"), f"use new's manager; {TAKEOVER}")
        self.assertEqual(
            ensemble_command.takeover("mgr", None), f"use the holding manager; {TAKEOVER}"
        )


class HoldTest(EnsembleCommandCase):
    def started(self, seen):
        """A Popen stand-in recording the command, its manager and the active holder."""

        def popen(argv, **options):
            seen.append((argv, options["env"]["ENSEMBLE_MANAGER"], self.roster.snapshot()[0]))
            child = mock.MagicMock()
            child.wait.return_value = 7
            return child

        return mock.patch("subprocess.Popen", side_effect=popen)

    def test_the_command_runs_under_a_hold_labelled_with_the_name(self):
        seen = []
        with self.started(seen):
            self.assertEqual(self.invoke("hold", "mgr", "--", "claude", "--help"), (7, "", ""))
        [(argv, manager, holder)] = seen
        self.assertEqual((argv, manager, holder.session), (("claude", "--help"), "mgr", "mgr"))
        self.assertIsNone(self.roster.snapshot()[0])

    def test_force_takes_a_held_place_over(self):
        seen = []
        with self.roster.hold("old"), self.started(seen):
            self.assertEqual(self.invoke("hold", "--force", "mgr", "--", "claude"), (7, "", ""))
        self.assertEqual([holder.session for _, _, holder in seen], ["mgr"])

    def test_a_held_place_exits_1_with_the_takeover(self):
        with self.roster.hold("new"):
            result, stdout, stderr = self.invoke("hold", "mgr", "--", "true")
        self.assertEqual((result, stdout), (1, ""))
        self.assertEqual(
            stderr,
            f"ensemble: {self.directory}: held by new\nensemble: use new's manager; {TAKEOVER}\n",
        )

    def test_an_invalid_name_exits_2_and_writes_nothing(self):
        for name in ("/tmp/x", "..", "a/b"):
            with self.subTest(name=name):
                result, stdout, stderr = self.invoke("hold", name, "--", "true")
                self.assertEqual((result, stdout), (2, ""))
                self.assertTrue(stderr.startswith("ensemble: invalid manager name"), stderr)
        self.assertFalse(os.path.exists(os.path.join(self.ensemble, "managers")))
        self.assertFalse(os.path.exists("/tmp/x/roster.json"))

    def test_hold_needs_a_command(self):
        for argv in (("hold", "mgr"), ("hold", "mgr", "--")):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                main(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_hold_and_resume_are_refused_inside_an_agent_session(self):
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}):
            for argv in (("hold", "mgr", "--", "true"), ("resume",)):
                with self.subTest(argv=argv):
                    result, stdout, stderr = self.invoke(*argv)
                    self.assertEqual((result, stdout), (2, ""))
                    self.assertTrue(stderr.startswith("ensemble: refused inside an agent session"))
        self.assertFalse(os.path.exists(self.directory))


def _record(kind: str, name: str) -> Record:
    return Record(
        1,
        kind,
        name,
        (),
        str(uuid.uuid4()),
        None,
        ("ensemble-session",),
        ("ensemble-session", "resume", name),
        "opaque",
        None,
    )


class ListCase(EnsembleCommandCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.ensemble, "managers")

    def manager(self, name: str, *entries: tuple[str, str, State]) -> Roster:
        roster = Roster(os.path.join(self.root, name))
        os.makedirs(roster.directory, mode=0o700, exist_ok=True)
        for entry, kind, state in entries:
            record = _record(kind, entry)
            roster.begin(Spec(entry, ("fake-ctl", "start")), sid=record.sid)
            roster.commit(entry, record)
            if state != State.LIVE:
                mark(roster, entry, state)
        return roster

    def session(self, name: str, **fields) -> None:
        record = {"client": "kind-a", "sid": "s-1", "pid": 4101, "cwd": "/home/user/project"}
        Path(self.root, name, "session.json").write_text(json.dumps({**record, **fields}))


class ListTest(ListCase):
    def test_managers_without_records_print_dashes_sorted(self):
        for name in ("updraft-manager", "ensemble-manager", "agent-usage-manager"):
            self.manager(name)
        self.assertEqual(
            self.invoke("list"),
            (
                0,
                "agent-usage-manager\tnot-held\t-\t-\n  -\n"
                "ensemble-manager\tnot-held\t-\t-\n  -\n"
                "updraft-manager\tnot-held\t-\t-\n  -\n",
                "",
            ),
        )

    def test_a_record_prints_its_client_and_cwd(self):
        self.manager("demo")
        self.session("demo", cwd="/home/user/other project")
        self.assertEqual(
            self.invoke("list"), (0, "demo\tnot-held\tkind-a\t/home/user/other project\n  -\n", "")
        )

    def test_entries_print_kind_and_state_under_their_manager(self):
        self.manager("demo", ("w1", "worker", State.LIVE), ("r1", "role", State.GONE))
        self.assertEqual(
            self.invoke("list"),
            (0, "demo\tnot-held\t-\t-\n  r1\trole\tgone\n  w1\tworker\tlive\n", ""),
        )

    def test_an_entry_without_a_record_has_no_kind(self):
        roster = self.manager("demo")
        roster.begin(Spec("s1", ("fake-ctl", "start")), sid=str(uuid.uuid4()))
        self.assertEqual(self.invoke("list"), (0, "demo\tnot-held\t-\t-\n  s1\t-\tstarting\n", ""))

    def test_no_entries_print_one_dash_row(self):
        os.makedirs(os.path.join(self.root, "bare"))
        self.manager("empty")
        self.assertEqual(
            self.invoke("list"),
            (0, "bare\tnot-held\t-\t-\n  -\nempty\tnot-held\t-\t-\n  -\n", ""),
        )

    def test_a_held_manager_shows_its_state(self):
        roster = self.manager("demo")
        self.session("demo", pid=4101)
        with roster.hold("owner"), mock.patch("os.kill"):
            self.assertEqual(
                self.invoke("list")[1].splitlines()[0],
                "demo\tclient-running\tkind-a\t/home/user/project",
            )

    def test_a_corrupt_roster_shows_an_error_row_and_exits_1(self):
        broken = self.manager("broken")
        self.manager("ok", ("w1", "worker", State.LIVE))
        Path(broken.path).write_text("{")
        result, stdout, _ = self.invoke("list")
        lines = stdout.splitlines()
        self.assertEqual(result, 1)
        self.assertTrue(lines[0].startswith(f"broken\tnot-held\t-\t-\terror\t{broken.path}: "))
        self.assertEqual(lines[1:], ["ok\tnot-held\t-\t-", "  w1\tworker\tlive"])

    def test_an_invalid_record_shows_an_error_row_and_exits_1(self):
        self.manager("bad", ("w1", "worker", State.LIVE))
        self.manager("ok")
        Path(self.root, "bad", "session.json").write_text("[]")
        path = os.path.join(self.root, "bad", "session.json")
        self.assertEqual(
            self.invoke("list"),
            (
                1,
                f"bad\tnot-held\t-\t-\terror\t{path}: want exactly client, sid, pid and cwd\n"
                "  w1\tworker\tlive\nok\tnot-held\t-\t-\n  -\n",
                "",
            ),
        )

    def test_an_unreadable_manager_prints_dashes_and_its_error(self):
        self.manager("bad")
        self.manager("ok")
        lock = os.path.join(self.root, "bad", "roster.lock")
        os.mkdir(lock)
        self.assertEqual(
            self.invoke("list"),
            (1, f"bad\t-\t-\t-\terror\t{lock}: not a regular file\nok\tnot-held\t-\t-\n  -\n", ""),
        )

    def test_no_managers_prints_the_new_manager_usage(self):
        usage = (0, "no managers yet; start one: ensemble <name>\n", "")
        self.assertEqual(self.invoke("list"), usage)
        os.makedirs(self.root)
        Path(self.root, "file").touch()
        self.assertEqual(self.invoke("list"), usage)

    def test_list_takes_no_arguments_or_manager(self):
        for argv in (("list", "extra"), ("list", "--manager", "demo")):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(argv)
                self.assertEqual(caught.exception.code, 2)
        self.manager("demo")
        with mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1", "ENSEMBLE_MANAGER": "x"}):
            self.assertEqual(self.invoke("list"), (0, "demo\tnot-held\t-\t-\n  -\n", ""))

    def test_fields_cannot_break_a_row(self):
        roster = self.manager("demo")
        self.session("demo", cwd="/a\tb\x1bc\nd")
        roster.begin(Spec("s1", ("fake-ctl", "start")), sid=str(uuid.uuid4()))
        self.assertEqual(
            self.invoke("list"),
            (0, "demo\tnot-held\tkind-a\t/a bcd\n  s1\t-\tstarting\n", ""),
        )


def _mode(fd: int) -> list:
    """fd's terminal attributes but PENDIN, which BSD sets itself on a return to canonical input."""
    mode = termios.tcgetattr(fd)
    mode[3] &= ~termios.PENDIN
    return mode


def _dead(pid, _signal):
    raise ProcessLookupError(pid)


class _InterruptedFlush(io.StringIO):
    """A terminal Ctrl-C lands while the prompt is being written."""

    def flush(self):
        raise KeyboardInterrupt


class SelectTest(ListCase):
    PROMPT = "select a manager [1-{}], Enter cancels: "

    def select(self, answers, *, kill=_dead):
        with (
            mock.patch("sys.stdin", io.StringIO(answers)),
            mock.patch("os.kill", side_effect=kill),
        ):
            return self.invoke("list", "--select")

    def test_rows_are_numbered_on_stderr_and_a_number_selects(self):
        self.manager("alpha", ("w1", "worker", State.LIVE))
        self.manager("demo")
        self.assertEqual(
            self.select("2\n"),
            (
                0,
                "open demo\n",
                "1) alpha\tnot-held\t-\t-\n  w1\tworker\tlive\n2) demo\tnot-held\t-\t-\n  -\n"
                + self.PROMPT.format(2),
            ),
        )

    def test_a_listed_name_selects(self):
        self.manager("alpha")
        self.manager("demo")
        self.assertEqual(self.select("demo\n")[:2], (0, "open demo\n"))

    def test_an_empty_line_eof_or_interrupt_cancels(self):
        self.manager("demo")
        for answers in ("\n", "", "  \n"):
            with self.subTest(answers=answers):
                self.assertEqual(self.select(answers)[:2], (0, ""))
        with mock.patch("sys.stdin", mock.Mock(readline=mock.Mock(side_effect=KeyboardInterrupt))):
            self.assertEqual(self.invoke("list", "--select")[:2], (0, ""))

    def test_an_interrupt_while_the_prompt_is_written_cancels(self):
        self.manager("demo")
        stdout, stderr = io.StringIO(), _InterruptedFlush()
        with (
            mock.patch("sys.stdin", io.StringIO("1\n")),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = main(("list", "--select"))
        self.assertEqual((result, stdout.getvalue()), (0, ""))
        self.assertTrue(stderr.getvalue().endswith(self.PROMPT.format(1) + "\n"), stderr.getvalue())

    def test_an_unknown_answer_prompts_again(self):
        self.manager("demo")
        result, stdout, stderr = self.select("9\nno\x1bpe\n1\n")
        self.assertEqual((result, stdout), (0, "open demo\n"))
        self.assertIn("ensemble: no manager 9; give 1-1 or a name\n", stderr)
        self.assertIn("ensemble: no manager nope; give 1-1 or a name\n", stderr)
        self.assertEqual(stderr.count(self.PROMPT.format(1)), 3)

    def test_cancel_after_a_listing_error_exits_1(self):
        Path(self.manager("broken").path).write_text("{")
        self.assertEqual(self.select("\n")[:2], (1, ""))

    def test_no_managers_prints_the_usage_without_a_prompt(self):
        self.assertEqual(
            self.select("1\n"), (0, "", "no managers yet; start one: ensemble <name>\n")
        )

    def test_each_holder_state_and_record_gives_its_plan(self):
        roster = self.manager("demo")
        self.assertEqual(self.select("1\n")[1], "open demo\n")
        self.session("demo")
        self.assertEqual(self.select("1\n")[1], "resume-new demo\n")
        with roster.hold("demo"):
            self.assertEqual(self.select("1\n")[1], "resume-held demo\n")
            self.assertEqual(self.select("1\n", kill=None)[1], "attach demo\n")

    def test_refusals_open_nothing_and_exit_1(self):
        roster = self.manager("demo")
        self.session("demo")
        refused = "ensemble: manager demo: {}; nothing opened\n"
        result, stdout, stderr = self.select("1\n", kill=None)
        self.assertEqual((result, stdout), (1, ""))
        self.assertTrue(
            stderr.endswith(refused.format("its client 4101 runs without a holder; exit it first"))
        )
        with roster.hold("work"):
            result, stdout, stderr = self.select("1\n")
        self.assertEqual((result, stdout), (1, ""))
        self.assertTrue(stderr.endswith(refused.format("held by work")))
        lock = os.path.join(self.root, "demo", "roster.lock")
        os.unlink(lock)
        os.mkdir(lock)
        result, stdout, stderr = self.select("1\n")
        self.assertEqual((result, stdout), (1, ""))
        self.assertTrue(stderr.endswith(refused.format(f"{lock}: not a regular file")))

    def test_an_unreadable_record_warns_and_opens_without_resuming(self):
        self.manager("demo")
        Path(self.root, "demo", "session.json").write_text("[]")
        path = os.path.join(self.root, "demo", "session.json")
        result, stdout, stderr = self.select("1\n")
        self.assertEqual((result, stdout), (0, "open demo\n"))
        self.assertTrue(
            stderr.endswith(
                f"ensemble: manager demo: {path}: want exactly client, sid, pid and cwd;"
                " its session is not resumed\n"
            )
        )


class MenuTest(ListCase):
    """list --select with stdin and stderr on a terminal and stdout captured, as ensemble runs it."""

    PROMPT = "select a manager [1-{}] or up/down, Enter cancels: "

    def setUp(self):
        super().setUp()
        for name in ("alpha", "demo", "zeta"):
            self.manager(name)

    def menu(
        self, *chunks: bytes, interrupt: bool = False, columns: int = 0
    ) -> tuple[int, str, str]:
        """Type chunks once the prompt shows, pausing between them: exit, stdout, terminal output.

        interrupt sends SIGINT to this thread once the prompt shows, as a terminal's Ctrl-C would.
        """
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        before = _mode(slave)
        shown = bytearray()
        caller = threading.get_ident()

        def terminal():
            with contextlib.suppress(OSError):  # EIO once every slave end is closed
                while not shown.endswith(b"Enter cancels: "):
                    if not (chunk := os.read(master, 4096)):
                        return
                    shown.extend(chunk)
                if interrupt:
                    signal.pthread_kill(caller, signal.SIGINT)
                for index, chunk in enumerate(chunks):
                    if index:
                        time.sleep(0.5)  # longer than a lone Esc's wait for its sequence
                    os.write(master, chunk)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if not select.select([master], [], [], 2)[0]:
                        os.write(master, b"\x04")  # still waiting for keys: end its input
                    elif not (chunk := os.read(master, 4096)):
                        return
                    else:
                        shown.extend(chunk)

        typist = threading.Thread(target=terminal)
        stdout = io.StringIO()
        handler = signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            with (
                open(slave, encoding="utf-8") as stdin,
                open(os.dup(slave), "w", encoding="utf-8") as stderr,
                mock.patch("sys.stdin", stdin),
                mock.patch("sys.stderr", stderr),
                mock.patch("os.kill", side_effect=_dead),
                contextlib.redirect_stdout(stdout),
            ):
                typist.start()
                result = main(("list", "--select"))
                self.assertEqual(_mode(slave), before, "terminal mode restored")
        finally:
            signal.signal(signal.SIGINT, handler)
            typist.join(timeout=30)
        self.assertFalse(typist.is_alive())
        return result, stdout.getvalue(), shown.decode(errors="replace")

    def test_down_and_up_move_through_the_managers_and_wrap(self):
        cases = {
            b"\x1b[B\r": "alpha",
            b"\x1b[B\x1b[B\r": "demo",
            b"\x1b[B\x1b[B\x1b[B\x1b[B\r": "alpha",
            b"\x1b[A\r": "zeta",
            b"\x1b[A\x1b[A\x1b[A\x1b[A\r": "zeta",
            b"\x1bOB\x1bOB\r": "demo",
            b"\x1b[1;5B\r": "alpha",
        }
        for keys, chosen in cases.items():
            with self.subTest(keys=keys):
                self.assertEqual(self.menu(keys)[:2], (0, f"open {chosen}\n"))

    def test_an_all_digit_name_opens_the_manager_it_names(self):
        self.manager("3")
        self.assertEqual(self.menu(b"\x1b[B\r")[:2], (0, "open 3\n"))
        self.assertEqual(self.menu(b"3\r")[:2], (0, "open demo\n"))

    def test_the_prompt_line_shows_the_selected_name(self):
        result, _, shown = self.menu(b"\x1b[B", b"\x1b[B", b"\r")
        self.assertEqual(result, 0)
        self.assertIn("1) alpha\tnot-held\t-\t-\r\n  -\r\n2) demo", shown)
        self.assertIn("\r\x1b[K" + self.PROMPT.format(3) + "alpha", shown)
        self.assertIn("\r\x1b[K" + self.PROMPT.format(3) + "demo", shown)
        self.assertNotIn("^[", shown)

    def test_a_narrow_terminal_shows_the_end_of_the_line_that_fits(self):
        prompt = self.PROMPT.format(3)
        result, _, shown = self.menu(b"\x1b[A", b"\r", columns=len(prompt) + 3)
        self.assertEqual(result, 0)
        self.assertIn(f"\r\x1b[K{prompt}ta\r\n", shown)
        result, _, shown = self.menu(b"\x1b[A", b"\r", columns=40)
        self.assertEqual(result, 0)
        self.assertIn(f"\r\x1b[K{prompt}\r\n", shown)

    def test_a_typed_number_or_name_still_selects(self):
        for keys, chosen in ((b"2\r", "demo"), (b"zeta\r", "zeta"), (b"\x1b[Bx\x7f\r", "alpha")):
            with self.subTest(keys=keys):
                self.assertEqual(self.menu(keys)[:2], (0, f"open {chosen}\n"))

    def test_typing_edits_the_selected_name(self):
        cases = {
            b"\x1b[B\x7f\x7f\x7f\x7f\x7f3\r": "zeta",
            b"\x1b[A\x15demo\r": "demo",
            b"x\b2\r": "demo",
            b"1\x04\r": "alpha",
            b"z\x1b[B\x1b[B\r": "demo",
        }
        for keys, chosen in cases.items():
            with self.subTest(keys=keys):
                self.assertEqual(self.menu(keys)[:2], (0, f"open {chosen}\n"))

    def test_keys_that_are_not_arrows_are_ignored(self):
        self.assertEqual(self.menu(b"\x1b[C\x1b[D\x01\t1\r")[:2], (0, "open alpha\n"))
        self.assertEqual(self.menu(b"\x1b", b"1\r")[:2], (0, "open alpha\n"))
        self.assertEqual(self.menu(b"\x1b2\r")[:2], (0, "open demo\n"))

    def test_an_unknown_answer_prompts_again(self):
        result, stdout, shown = self.menu(b"9\r", b"\xc3\xa9\r", b"\x1b[B\r")
        self.assertEqual((result, stdout), (0, "open alpha\n"))
        self.assertIn("ensemble: no manager 9; give 1-3 or a name\r\n", shown)
        self.assertIn("ensemble: no manager é; give 1-3 or a name\r\n", shown)

    def test_enter_with_nothing_typed_ctrl_d_or_ctrl_c_cancels(self):
        for keys in (b"\r", b"\x04", b"\x1b[B\x15\r"):
            with self.subTest(keys=keys):
                self.assertEqual(self.menu(keys)[:2], (0, ""))
        self.assertEqual(self.menu(interrupt=True)[:2], (0, ""))

    def test_a_terminal_mode_that_cannot_be_set_falls_back_to_the_line_prompt(self):
        with mock.patch("tty.setcbreak", side_effect=termios.error(25, "not a tty")):
            result, stdout, shown = self.menu(b"2\r")
        self.assertEqual((result, stdout), (0, "open demo\n"))
        self.assertIn(SelectTest.PROMPT.format(3), shown)


class ResumeTest(EnsembleCommandCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.directory, mode=0o700)
        self.cwd = os.path.join(self.home, "a b'c\x15d")
        os.mkdir(self.cwd)
        record = {"client": "kind-a", "sid": "s-1", "pid": 1 << 40, "cwd": self.cwd}
        Path(self.directory, "session.json").write_text(json.dumps(record))
        argv = '["kind-a", "--resume", "s-1"]'
        self.run = self.enterContext(
            mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=argv))
        )
        self.chdir = self.enterContext(mock.patch("os.chdir"))
        self.execvp = self.enterContext(mock.patch("os.execvp"))

    def under(self, lease):
        return mock.patch.dict(
            os.environ,
            {
                "ENSEMBLE_MANAGER": "mgr",
                "ENSEMBLE_HOLDER_ID": lease.id,
                "ENSEMBLE_HOLDER_GENERATION": str(lease.generation),
            },
        )

    def test_under_its_hold_it_execs_the_recorded_client_in_its_cwd(self):
        with self.roster.hold("mgr") as lease, self.under(lease):
            self.invoke("resume")
        self.chdir.assert_called_once_with(self.cwd)
        self.execvp.assert_called_once_with("kind-a", ("kind-a", "--resume", "s-1"))

    def test_it_records_its_own_pid_before_it_execs(self):
        path = Path(self.directory, "session.json")
        seen = []
        self.execvp.side_effect = lambda *_: seen.append(json.loads(path.read_text()))
        with self.roster.hold("mgr") as lease, self.under(lease):
            self.invoke("resume")
        self.assertEqual(
            seen, [{"client": "kind-a", "sid": "s-1", "pid": os.getpid(), "cwd": self.cwd}]
        )

    def test_a_failed_record_write_still_execs(self):
        before = Path(self.directory, "session.json").read_text()
        with (
            self.roster.hold("mgr") as lease,
            self.under(lease),
            mock.patch("os.replace", side_effect=PermissionError(13, "denied")),
        ):
            self.invoke("resume")
        self.execvp.assert_called_once_with("kind-a", ("kind-a", "--resume", "s-1"))
        self.assertEqual(Path(self.directory, "session.json").read_text(), before)

    def test_without_a_manager_or_holder_pair_it_is_a_usage_error(self):
        message = "ensemble: run ensemble resume under the manager's hold: ensemble list\n"
        for env in ({}, {"ENSEMBLE_MANAGER": "mgr"}, {"ENSEMBLE_MANAGER": "../x"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertEqual(self.invoke("resume"), (2, "", message))
        self.execvp.assert_not_called()

    def test_resume_takes_no_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(("resume", "mgr"))
        self.assertEqual(caught.exception.code, 2)

    def test_an_ended_holder_is_refused(self):
        with self.roster.hold("mgr") as lease:
            pass
        with self.under(lease):
            result, _, stderr = self.invoke("resume")
        self.assertEqual(result, 1)
        self.assertIn("holder", stderr)
        self.execvp.assert_not_called()

    def test_a_refused_target_execs_nothing(self):
        Path(self.directory, "session.json").unlink()
        with self.roster.hold("mgr") as lease, self.under(lease):
            result, _, stderr = self.invoke("resume")
        self.assertEqual(result, 1)
        self.assertIn("no session record", stderr)
        self.chdir.assert_not_called()
        self.execvp.assert_not_called()
