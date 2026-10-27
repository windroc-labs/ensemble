"""HostContract, FR-47's Host contract: a subclass also inherits Case and defines make_host() ->
Host. Each case runs short Python programs written against the Host ABC only, polling files under
self.home for events and output; the cannot-display case runs an in_process session on the Host.
"""

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from unittest import mock

from ensemble.session import Access, Host, Mode, NotShowable, SessionError, Spec, Viewer
from ensemble.session.engine.types import Key, KeyPress, Launch, Paste
from ensemble.session.viewer import display as displays
from ensemble.utils import Record
from tests.session import fake_client, harnesses
from tests.session.case import PY, REPO, child_env
from tests.session.modules import client_loader

OTHER_DISPLAY = "elsewhere:1.2"
TERMINAL_IDENTITY = ("TMUX", "TMUX_PANE", "TERM")
DEADLINE = 10
POLL = 0.05
PUT = (
    "import json, os, subprocess, sys, time\n"
    "def put(text):\n"
    "    with open(sys.argv[1] + '.tmp', 'w') as f:\n"
    "        f.write(text)\n"
    "    os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
)
EXIT_7 = (sys.executable, "-I", "-c", "import sys; sys.exit(7)")
KILLED = (sys.executable, "-I", "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
SLEEP = (sys.executable, "-I", "-c", "import time; time.sleep(30)")
READ_LINE = (sys.executable, "-I", "-c", PUT + "put(sys.stdin.readline())")
DUMP_ENV = PUT + "put(json.dumps(dict(os.environ)))"
SPAWN = (
    sys.executable,
    "-I",
    "-c",
    PUT + "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "put(f'{os.getpid()} {child.pid}')\n"
    "time.sleep(30)",
)
LEAVE = (
    sys.executable,
    "-I",
    "-c",
    PUT + "ready = sys.argv[1] + '.ready'\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import signal, sys, time; "
    'signal.signal(signal.SIGHUP, signal.SIG_IGN); open(sys.argv[1], "w").close(); '
    "time.sleep(30)', ready])\n"
    "while not os.path.exists(ready):\n"
    "    time.sleep(0.01)\n"
    "put(f'{os.getpid()} {child.pid}')\n",
)
RAW = (
    sys.executable,
    "-I",
    "-c",
    PUT + "import tty\n"
    "if os.isatty(0):\n"
    "    tty.setraw(0)\n"
    "seen = b''\n"
    "put(seen.hex())\n"
    "while not seen.endswith(b'q'):\n"
    "    seen += os.read(0, 64)\n"
    "    put(seen.hex())\n",
)
BRACKETED = (
    sys.executable,
    "-I",
    "-c",
    PUT + "import tty\n"
    "tty.setraw(0)\n"
    "os.write(1, b'\\x1b[?2004h')\n"
    "put('ready')\n"
    "seen = b''\n"
    "while not seen.endswith(b'\\x1b[201~'):\n"
    "    seen += os.read(0, 64)\n"
    "put(seen.hex())\n",
)
LEAVES = (
    sys.executable,
    "-I",
    "-c",
    PUT + "child = subprocess.Popen([sys.executable, '-c', "
    "'import signal, time; signal.signal(signal.SIGHUP, signal.SIG_IGN); time.sleep(30)'])\n"
    "put(str(child.pid))\n",
)
PRESSED = ("Escape", "3", "Up", "BTab", "C-c", "q")
RECEIVED = b"\x1b3\x1b[A\x1b[Z\x03q"
CHILD = """import json, sys
from ensemble.session import Host, Launch
from ensemble.utils import load_class
path, host, settings, name, argv, cwd, env = sys.argv[1:]
load_class(path, Host)(host, json.loads(settings)).terminal(
    name, Launch(tuple(json.loads(argv)), cwd=cwd, env=json.loads(env))
)
"""


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class HostContract:
    def make_host(self) -> Host:
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.host = self.make_host()

    def fresh(self) -> str:
        name = f"hc-{uuid.uuid4().hex[:12]}"
        self.addCleanup(self.forget, name)
        return name

    def forget(self, name: str) -> None:
        with contextlib.suppress(SessionError):
            self.host.attach(name).close()

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home, **extra}

    def creator_env(self) -> dict[str, str]:
        return child_env(None, REPO)

    def start(self, kind: str, name: str, argv: tuple[str, ...], *args: str, **kw):
        return getattr(self.host, kind)(
            name, Launch((*argv, *args), cwd=self.home, env=self.env()), **kw
        )

    def file(self, label: str) -> str:
        return os.path.join(self.home, f"{label}-{uuid.uuid4().hex[:8]}")

    def poll(self, check, what: str):
        deadline = time.monotonic() + DEADLINE
        while not (found := check()):
            if time.monotonic() > deadline:
                self.fail(f"no {what} within {DEADLINE} s")
            time.sleep(POLL)
        return found

    def event(self, events: str, name: str, tail: str) -> None:
        line = re.compile(rf"\d\d:\d\d:\d\d {re.escape(name)} {tail}")
        self.poll(
            lambda: any(line.fullmatch(each) for each in (read(events) or "").splitlines()),
            f"event {name} {tail} in {events}",
        )

    def a_record(self, name: str) -> Record:
        return Record(
            1,
            "fake",
            name,
            (name,),
            None,
            None,
            ("ensemble-session",),
            ("ensemble-session", "resume", name),
            None,
            None,
        )

    def test_detach_writes_exited_and_the_returncode_when_its_program_exits(self):
        name, events = self.fresh(), self.file("events")
        process = self.start("detach", name, EXIT_7, events=events)
        self.event(events, name, "exited 7")
        self.assertTrue(process.wait_exit(DEADLINE))
        self.assertFalse(process.alive())

    def test_detach_writes_exited_minus_the_signal_when_a_signal_ends_its_program(self):
        name, events = self.fresh(), self.file("events")
        process = self.start("detach", name, KILLED, events=events)
        self.event(events, name, f"exited -{int(signal.SIGKILL)}")
        self.assertTrue(process.wait_exit(DEADLINE))

    def test_a_terminal_writes_dead_when_its_program_exits(self):
        name, events = self.fresh(), self.file("events")
        self.start("terminal", name, (sys.executable, "-I", "-c", "pass"), events=events)
        self.event(events, name, "dead")

    def test_a_paste_and_the_enter_key_reach_the_terminal_programs_stdin_as_one_line(self):
        name, out = self.fresh(), self.file("line")
        terminal = self.start("terminal", name, READ_LINE, out)
        terminal.send(Paste("hello host"))
        terminal.send(Key("Enter"))
        self.poll(lambda: (read(out) or "").rstrip("\r\n") == "hello host", f"line in {out}")

    def test_keys_in_the_session_notation_reach_the_terminal_program_translated(self):
        name, out = self.fresh(), self.file("keys")
        terminal = self.start("terminal", name, RAW, out)
        self.poll(lambda: read(out) == "", f"raw input in {out}")
        terminal.press(tuple(map(KeyPress.parse, PRESSED)))
        self.poll(lambda: read(out) == RECEIVED.hex(), f"{RECEIVED!r} in {out}")

    def test_a_detached_program_gets_the_supplied_environment_verbatim(self):
        got, env = self.check_environment("detach")
        self.assertEqual(got, env, f"program env: {got!r}")

    def test_a_terminal_program_gets_the_hosts_terminal_identity_and_the_rest_verbatim(self):
        got, env = self.check_environment("terminal")
        self.assertEqual(
            {key: value for key, value in got.items() if key not in TERMINAL_IDENTITY},
            {key: value for key, value in env.items() if key not in TERMINAL_IDENTITY},
            f"program env: {got!r}",
        )
        for key in TERMINAL_IDENTITY:
            self.assertNotEqual(got.get(key), env[key], key)
        self.assert_terminal_identity(got)

    def assert_terminal_identity(self, env: dict[str, str]) -> None:
        """The Host's own TERMINAL_IDENTITY values in a terminal program's `env`."""

    def check_environment(self, kind):
        name, out = self.fresh(), self.file("env")
        gate = self.file("finish")
        env = self.env(
            HOST_CONTRACT_VALUE="a b 'c' \"$HOME\" ;*",
            TMUX="caller-server",
            TMUX_PANE="caller-pane",
            TERM="caller-terminal",
            PWD="/wrong-directory",
        )
        with mock.patch.dict(os.environ, {"HOST_CONTRACT_CREATOR_ONLY": "1"}):
            process = getattr(self.host, kind)(
                name,
                Launch(
                    (
                        "/usr/bin/perl",
                        "-e",
                        'open(my $out, ">", $ARGV[0]) or die $!; '
                        'print $out map { "$_=$ENV{$_}\\0" } keys %ENV; close($out); '
                        "select(undef, undef, undef, 0.01) until -e $ARGV[1];",
                        out,
                        gate,
                    ),
                    cwd=self.home,
                    env=env,
                ),
            )
        self.addCleanup(process.close)
        got = dict(
            entry.split("=", 1)
            for entry in self.poll(lambda: read(out), f"env in {out}").split("\0")
            if entry
        )
        with open(gate, "w", encoding="utf-8"):
            pass
        self.assertTrue(process.wait_exit(DEADLINE))
        return got, env

    def test_a_foreign_display_is_kept_and_listed_only_under_itself(self):
        name = self.fresh()
        self.start("detach", name, SLEEP, display=OTHER_DISPLAY)
        viewer = Viewer()
        self.assertIn(name, displays.names(viewer, self.host, OTHER_DISPLAY))
        self.assertEqual(self.host.attach(name).display(), OTHER_DISPLAY)
        for display in ("other", ""):
            with self.subTest(display=display):
                self.assertNotIn(name, displays.names(viewer, self.host, display))

    def test_list_without_a_display_names_every_session_sorted(self):
        first, second = self.fresh(), self.fresh()
        self.start("detach", first, SLEEP)
        self.start("terminal", second, SLEEP, display=OTHER_DISPLAY)
        names = self.host.list()
        self.assertLessEqual({first, second}, set(names))
        self.assertEqual(names, sorted(names))

    def test_attach_returns_the_record_given_verbatim(self):
        for kind in ("detach", "terminal"):
            with self.subTest(kind=kind):
                name = self.fresh()
                self.start(kind, name, SLEEP, record=self.a_record(name))
                self.assertEqual(self.host.attach(name).record(), self.a_record(name))
        name = self.fresh()
        self.start("detach", name, SLEEP)
        self.assertIsNone(self.host.attach(name).record())

    def test_detach_keeps_the_record_sid_as_private_identity(self):
        name = self.fresh()
        record = replace(self.a_record(name), sid=str(uuid.uuid4()))
        process = self.start("detach", name, SLEEP, record=record)
        self.assertEqual(process.session_identity()[0], record.sid)
        self.assertEqual(self.host.attach(name).session_identity()[0], record.sid)

    def test_kill_ends_the_program_and_its_child_process(self):
        for kind in ("detach", "terminal"):
            with self.subTest(kind=kind):
                name, pids = self.fresh(), self.file("pids")
                process = self.start(kind, name, SPAWN, pids)
                own, child = map(
                    int, self.poll(lambda pids=pids: read(pids), f"pids in {pids}").split()
                )
                self.assertFalse(process.wait_exit(0.2))
                process.kill()
                self.poll(
                    lambda own=own, child=child: gone(own) and gone(child),
                    f"end of {own} and {child}",
                )
                self.assertFalse(process.alive())

    def test_a_child_its_ended_program_left_is_waited_for_until_kill_or_close_ends_it(self):
        for kind in ("detach", "terminal"):
            for end in ("kill", "close"):
                with self.subTest(kind=kind, end=end):
                    self.left_child_ended_by(end, kind)

    def left_child_ended_by(self, end: str, kind: str) -> None:
        name, pids, events = self.fresh(), self.file("pids"), self.file("events")
        process = self.start(kind, name, LEAVE, pids, events=events)
        self.addCleanup(process.kill)
        own, child = map(int, self.poll(lambda pids=pids: read(pids), f"pids in {pids}").split())
        self.poll(lambda: gone(own), f"end of {own}")
        self.poll(lambda: not process.alive(), f"{name} no longer running its program")
        self.event(events, name, "exited 0" if kind == "detach" else "dead")
        if kind == "detach":
            self.poll(lambda: name not in self.host.list(), f"{name} session removed")
        else:
            self.assertIn(name, self.host.list())
        self.assertFalse(process.wait_exit(0.3))
        self.assertFalse(gone(child))
        getattr(process, end)()
        self.poll(lambda: gone(child), f"end of {child}")
        self.poll(lambda: process.wait_exit(0), f"{name}'s exit")

    def test_a_handle_never_acts_on_a_later_session_of_its_name(self):
        name = self.fresh()
        old = self.start("detach", name, SLEEP, display=OTHER_DISPLAY)
        old.close()
        self.poll(lambda: old.wait_exit(0), f"{name}'s exit")
        new = self.start("detach", name, SLEEP, record=self.a_record(name))
        self.assertEqual((old.alive(), old.record(), old.display()), (False, None, None))
        old.kill()
        old.close()
        self.assertTrue(new.alive())
        self.assertEqual((new.record(), new.display()), (self.a_record(name), None))
        self.assertEqual(self.host.attach(name).record(), self.a_record(name))

    def test_close_twice_is_fine_and_forgets_the_session(self):
        name = self.fresh()
        process = self.start("terminal", name, SLEEP)
        process.close()
        process.close()
        self.assertFalse(process.alive())
        self.assertNotIn(name, self.host.list())
        with self.assertRaises(SessionError):
            self.host.attach(name)

    def test_publish_stores_the_record_attach_returns_and_needs_a_live_session(self):
        name = self.fresh()
        terminal = self.start("terminal", name, SLEEP)
        self.assertIsNone(self.host.attach(name).record())
        published = self.a_record(name)
        terminal.publish(published)
        self.assertEqual(self.host.attach(name).record(), published)
        terminal.close()
        with self.assertRaises(SessionError):
            terminal.publish(published)

    def test_release_leaves_what_is_not_that_generations(self):
        """_release(name, generation) removes only that generation's leftovers: a session of
        another (here none) stays; an unknown name is no error.
        """
        name = self.fresh()
        process = self.start("terminal", name, SLEEP)
        self.host._release(name, uuid.uuid4().hex)
        self.assertTrue(process.alive())
        self.assertIn(name, self.host.list())
        self.host._release(f"nope-{uuid.uuid4().hex[:12]}", uuid.uuid4().hex)

    def test_attach_of_an_unknown_name_raises_driver_error(self):
        with self.assertRaises(SessionError):
            self.host.attach(f"nope-{uuid.uuid4().hex[:12]}")

    def test_a_live_name_is_refused(self):
        name = self.fresh()
        process = self.start("detach", name, SLEEP)
        for kind in ("detach", "terminal"):
            with self.subTest(kind=kind), self.assertRaises(SessionError):
                self.start(kind, name, SLEEP)
        self.assertTrue(process.alive())

    def test_a_terminal_started_by_another_process_is_alive_here_and_takes_a_paste(self):
        name, out = self.fresh(), self.file("line")
        cls = type(self.host)
        done = subprocess.run(
            [
                *PY,
                "-c",
                CHILD,
                f"{cls.__module__}:{cls.__qualname__}",
                self.host.name,
                json.dumps(self.host.settings),
                name,
                json.dumps([*READ_LINE, out]),
                self.home,
                json.dumps(self.env()),
            ],
            env=self.creator_env(),
            capture_output=True,
            text=True,
            timeout=DEADLINE * 3,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        terminal = self.host.attach(name)
        self.assertTrue(terminal.alive())
        terminal.send(Paste("from afar"))
        terminal.send(Key("Enter"))
        self.poll(lambda: (read(out) or "").rstrip("\r\n") == "from afar", f"line in {out}")

    def test_an_unattachable_host_names_no_attach_target(self):
        if self.host.attachable:
            self.skipTest("applies only to a Host that cannot show")
        name = self.fresh()
        self.assertIsNone(self.host.attach_target(name))
        self.start("detach", name, SLEEP)
        self.assertIsNone(self.host.attach_target(name))

    def test_a_host_that_cannot_show_returns_each_display_as_given(self):
        if self.host.attachable:
            self.skipTest("applies only to a Host that cannot show")
        for value in (None, "", OTHER_DISPLAY):
            with self.subTest(value=value):
                self.assertEqual(displays.display(Viewer(), value, self.host), value)

    def test_a_session_on_a_host_that_cannot_show_refuses_show_attach_command_and_screen(self):
        if self.host.attachable:
            self.skipTest("applies only to a Host that cannot show")
        scenario, kind = self.file("scenario"), type(self.host)
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump([{"out": {"type": "message", "text": "hi"}}], f)
        fake = "ensemble.session.clients.fake:InProcessFakeClient"
        configured = harnesses.Threaded.configured(
            clients={
                "fake": {"implementation": fake, "scenario": scenario, "log": self.file("log")}
            },
            hosts=Host.parse_hosts(
                {
                    self.host.name: {"implementation": f"{kind.__module__}:{kind.__qualname__}"},
                    "default": self.host.name,
                }
            ),
            class_loader=client_loader({"ensemble.session.clients.fake": fake_client}),
            host_factory=lambda *_: self.host,
        )
        work = self.file("work")
        session = configured.start(
            Spec(
                workdir=work,
                access=Access(cwd=work),
                client="fake",
                mode=Mode.IN_PROCESS,
                prompt="hi",
            )
        )
        self.addCleanup(session.stop)
        self.assertIs(configured.host(), self.host)
        self.assertFalse(session.showable)
        for call in ("show", "attach_command", "screen"):
            with self.subTest(call=call), self.assertRaises(NotShowable) as caught:
                getattr(session, call)()
            self.assertEqual(str(caught.exception), f"host {self.host.name} cannot show")
