"""ensemble-session resume and restart of a worker (FR-20, FR-36; Q07, Q08) through
bin/ensemble-session on a private tmux server (tests/session/cli_support.py): a live worker's resume
prints its Record and starts nothing; a gone worker's resume, from another caller, and a live
worker's restart replay its saved sid, cwd, flags, env and display without its prompt; concurrent
resumes start it once; a start killed right after its launch resumes by name; bad saved state or a
saved Host no longer configured starts and closes nothing. A client that ignores SIGHUP and SIGTERM
and has a child: a restart, and a resume after its session is killed, start the new generation only
once the old one and its child are gone. Each Claude launch (start, restart, resume) gets one
/color, at least 1 s after its SessionStart; send, reply and read add none.
"""

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
from datetime import datetime

from tests.session import live_tmux
from tests.session.cli_support import COMMAND, MANAGER, TIMEOUT, CliCase, logged
from tests.session.integration.clients.claude import fake_claude

FLAGS = ("--model", "m-test")
ENV_DUMP = (
    "import json, os, sys; open(sys.argv[1], 'a')"
    ".write(json.dumps({'argv': sys.argv[2:], 'env': dict(os.environ)}) + '\\n')"
)
# Each generation: checks which earlier generation's client or child still runs, logs itself, runs
# the fake claude, then outlives it; it and its child ignore SIGHUP and SIGTERM, for at most CAP s.
GENERATION = """import json, os, signal, subprocess, sys, time

CAP = 60
CHILD = (
    "import signal, time\\n"
    "for s in (signal.SIGHUP, signal.SIGTERM): signal.signal(s, signal.SIG_IGN)\\n"
    f"time.sleep({CAP})\\n"
)


def running(pid):
    found = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    ).stdout.strip()
    return bool(found) and not found.startswith("Z")


log, fake, args = sys.argv[1], sys.argv[2], sys.argv[3:]
for each in (signal.SIGHUP, signal.SIGTERM):
    signal.signal(each, signal.SIG_IGN)
try:
    with open(log, encoding="utf-8") as f:
        before = [json.loads(line) for line in f]
except FileNotFoundError:
    before = []
start = time.time()
overlap = sorted(pid for gen in before for pid in (gen["pid"], gen["child"]) if running(pid))
child = subprocess.Popen([sys.executable, "-I", "-c", CHILD], stdin=subprocess.DEVNULL)
with open(log, "a", encoding="utf-8") as f:
    entry = {"pid": os.getpid(), "child": child.pid, "start": start, "overlap": overlap}
    f.write(json.dumps({**entry, "argv": args}) + "\\n")
subprocess.call([sys.executable, "-I", fake, *args])
time.sleep(max(0, start + CAP - time.time()))
"""

# A tmux for one start: it runs the fixture's, and that start's first call after new-session (its
# client let run, nothing saved since) SIGKILLs the start instead; other callers pass through
KILLER = """import os, signal, subprocess, sys
try:
    with open(LAUNCHED) as f:
        starter = int(f.read())
except FileNotFoundError:
    starter = None
if os.getppid() == starter:
    os.kill(starter, signal.SIGKILL)
    sys.exit(1)
code = subprocess.call([REAL, *sys.argv[1:]])
if "new-session" in sys.argv[1:] and starter is None:
    with open(LAUNCHED, "w") as f:
        f.write(str(os.getppid()))
sys.exit(code)
"""


def running(pid: int) -> bool:
    """pid runs and is no zombie."""
    found = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={"PATH": os.defpath, "LC_ALL": "C"},
        timeout=TIMEOUT,
    ).stdout.strip()
    return bool(found) and not found.startswith("Z")


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[i : i + 2] == [flag, value] for i in range(len(argv)))


class Base(CliCase):
    def again(self, cmd: str, name: str, **kw) -> subprocess.CompletedProcess:
        """`CMD NAME` with the fake claude's scenario for it, so a client it starts logs."""
        return self.session(cmd, name, **self.scene(name), **kw)

    def first_on_path(self, script: str) -> str:
        """PATH with a `claude` running `script` (sh) ahead of the fixture's."""
        directory = os.path.join(self.root, "shim")
        os.mkdir(directory)
        path = os.path.join(directory, "claude")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(path, 0o755)
        return os.pathsep.join((directory, self.env()["PATH"]))

    def pane(self, name: str) -> str:
        """Its pane's id and process."""
        return self.ok(
            self.server.tmux("display-message", "-p", "-t", f"={name}:", "#{pane_id} #{pane_pid}")
        ).stdout.strip()

    def state_file(self, sid: str) -> str:
        return os.path.join(self.ensemble, "workers", sid, "worker.json")

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def kill_session(self, name: str) -> None:
        self.ok(self.server.tmux("kill-session", "-t", f"={name}"))

    def dead(self, name: str) -> None:
        """Kill worker `name`'s client and wait for its pane to show it dead."""
        os.kill(self.calls(name)[-1]["pid"], signal.SIGTERM)
        self.wait_event(name, "dead")


class ResumeTest(Base):
    def test_resume_of_a_live_worker_prints_its_record_and_starts_and_closes_nothing(self):
        record = self.record(self.start("w1"))
        self.assertEqual(record.display, MANAGER)
        self.wait_event("w1", "done")
        (call,) = self.calls("w1")
        pane, state = self.pane("w1"), self.read_bytes(self.state_file(record.sid))
        stored = self.server.option("w1", "@record")
        res = self.again("resume", "w1", inside=None, cwd=self.root)
        self.assertEqual((self.record(res), res.stderr), (record, ""))
        self.assertEqual(self.calls("w1"), [call])
        self.assertTrue(running(call["pid"]))
        self.assertEqual(self.pane("w1"), pane)
        self.assertEqual(self.read_bytes(self.state_file(record.sid)), state)
        self.assertEqual(self.server.option("w1", "@record"), stored)

    def test_resume_of_a_gone_worker_from_another_caller_replays_its_saved_launch(self):
        dump = os.path.join(self.root, "env.jsonl")
        q = shlex.quote
        path = self.first_on_path(
            f'#!/bin/sh\n{q(sys.executable)} -I -c {q(ENV_DUMP)} {q(dump)} "$@"\n'
            f'exec {q(sys.executable)} -I {q(fake_claude.__file__)} "$@"\n'
        )
        record = self.record(self.start("w1", "--env", "K=v", "--", *FLAGS, PATH=path))
        self.wait_event("w1", "done")
        self.dead("w1")
        self.kill_session("w1")
        res = self.again("resume", "w1", inside=None, cwd=self.root, PATH=path)
        self.assertEqual(self.record(res), record)
        self.assertEqual(res.stderr, "session w1: tmux attach -t '=w1'\n")
        first, again = self.calls("w1")
        self.assertEqual(again["cwd"], self.cwd)
        self.assertTrue(has_pair(again["argv"], "--resume", record.sid), again["argv"])
        self.assertNotIn("--session-id", again["argv"])
        self.assertTrue(has_pair(again["argv"], *FLAGS), again["argv"])
        self.assertEqual(first["argv"][-2:], ["--", "hi"])
        self.assertNotIn("hi", again["argv"])
        self.assertEqual([entry["env"].get("K") for entry in logged(dump)], ["v", "v"])
        self.assertEqual(self.server.option("w1", "@record"), record.to_line())
        self.assertIn(self.server.pane_of("w1"), self.server.panes(f"={MANAGER}:"))

    def test_a_start_killed_right_after_its_launch_is_resumed_by_name(self):
        directory = os.path.join(self.root, "killer")
        os.mkdir(directory)
        tmux = os.path.join(directory, "tmux")
        names = f"REAL, LAUNCHED = {self.server.program!r}, {os.path.join(directory, 'launched')!r}"
        with open(tmux, "w", encoding="utf-8") as f:
            f.write(f"#!{sys.executable} -I\n{names}\n{KILLER}")
        os.chmod(tmux, 0o700)
        killed = self.start("w1", PATH=os.pathsep.join((directory, self.env()["PATH"])))
        self.assertEqual((killed.returncode, killed.stdout), (-signal.SIGKILL, ""), killed.stderr)
        record = self.record(self.again("resume", "w1", inside=None, cwd=self.root))
        self.assertEqual(record.resume, ("ensemble-session", "resume", "w1"))
        self.assertTrue(os.path.isfile(self.state_file(record.sid)))
        self.assertEqual(self.server.option("w1", "@record"), record.to_line())
        calls = live_tmux.wait(lambda: self.calls("w1") or None, what="w1's client")
        self.assertIn(record.sid, calls[-1]["argv"])
        dead = self.server.tmux("display-message", "-p", "-t", "=w1:", "#{pane_dead}")
        self.assertEqual(self.ok(dead).stdout, "0\n")

    def test_restart_of_a_live_worker_resumes_its_sid_in_a_new_client_without_its_prompt(self):
        record = self.record(self.start("w1", "--", *FLAGS))
        self.wait_event("w1", "done")
        pane = self.pane("w1")
        res = self.again("restart", "w1")
        self.assertEqual(self.record(res), record)
        self.assertEqual(res.stderr, "session w1: tmux attach -t '=w1'\n")
        first, again = self.calls("w1")
        self.assertTrue(has_pair(first["argv"], "--session-id", record.sid), first["argv"])
        self.assertTrue(has_pair(again["argv"], "--resume", record.sid), again["argv"])
        self.assertNotIn("--session-id", again["argv"])
        self.assertTrue(has_pair(again["argv"], *FLAGS), again["argv"])
        self.assertNotIn("hi", again["argv"])
        self.assertEqual(again["cwd"], self.cwd)
        self.assertFalse(running(first["pid"]))
        self.assertTrue(running(again["pid"]))
        self.assertNotEqual(self.pane("w1"), pane)
        self.assertEqual(self.server.option("w1", "@record"), record.to_line())

    def test_two_concurrent_resumes_of_a_gone_worker_start_one_client(self):
        record = self.record(self.start("w1"))
        self.wait_event("w1", "done")
        self.dead("w1")
        self.kill_session("w1")
        env = self.env(inside=None, **self.scene("w1"))
        procs = []
        with self.poking():
            for _ in range(2):
                proc = subprocess.Popen(
                    [COMMAND, "resume", "w1"],
                    env=env,
                    cwd=self.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(proc.kill)
                procs.append(proc)
            outs = [proc.communicate(timeout=TIMEOUT) for proc in procs]
        self.assertEqual([proc.returncode for proc in procs], [0, 0], outs)
        self.assertEqual([out for out, _ in outs], [record.to_line() + "\n"] * 2)
        _, again = self.calls("w1")
        self.assertTrue(has_pair(again["argv"], "--resume", record.sid), again["argv"])
        self.assertTrue(running(again["pid"]))

    def test_bad_saved_state_exits_1_and_leaves_the_live_worker_alone(self):
        record = self.record(self.start("w1"))
        self.wait_event("w1", "done")
        (call,) = self.calls("w1")
        pane = self.pane("w1")
        with open(self.state_file(record.sid), "w", encoding="utf-8") as f:
            f.write("{")
        for cmd in ("resume", "restart"):
            with self.subTest(cmd=cmd):
                res = self.again(cmd, "w1")
                self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
                head = f"ensemble-session: w1: worker {record.sid}: bad worker.json: "
                self.assertTrue(res.stderr.startswith(head), res.stderr)
                self.assertEqual(self.calls("w1"), [call])
                self.assertTrue(running(call["pid"]))
                self.assertEqual(self.pane("w1"), pane)

    def test_a_saved_host_no_longer_configured_exits_2_before_touching_the_worker(self):
        record = self.record(self.start("w1"))
        self.wait_event("w1", "done")
        (call,) = self.calls("w1")
        pane = self.pane("w1")
        path = self.state_file(record.sid)
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**state, "host": "gone"}, f)
        for cmd in ("resume", "restart"):
            with self.subTest(cmd=cmd):
                res = self.again(cmd, "w1")
                self.assertEqual(
                    (res.returncode, res.stdout, res.stderr),
                    (2, "", "ensemble-session: unknown host gone\n"),
                )
                self.assertEqual(self.calls("w1"), [call])
                self.assertTrue(running(call["pid"]))
                self.assertEqual(self.pane("w1"), pane)


class ColorTest(Base):
    def session_start(self, name: str) -> float:
        """When its launch's one SessionStart record was written, in epoch seconds."""
        hooks = logged(os.path.join(self.ensemble, "tui", name, "hooks.jsonl"))
        (stamp,) = [hook["ts"] for hook in hooks if hook["label"] == "SessionStart"]
        return datetime.fromisoformat(stamp).timestamp()

    def test_each_launch_gets_one_color(self):
        self.record(self.start("w1"))
        starts = [self.session_start("w1")]
        self.record(self.again("restart", "w1"))
        starts.append(self.session_start("w1"))
        self.dead("w1")
        self.kill_session("w1")
        self.record(self.again("resume", "w1", inside=None, cwd=self.root))
        starts.append(self.session_start("w1"))
        pids = [call["pid"] for call in self.calls("w1")]
        colored = live_tmux.wait(
            lambda: len(self.commands("w1")) == 3 and self.commands("w1"), what="3 /color lines"
        )
        self.assertEqual(
            [(c["pid"], c["line"], c["dialog"]) for c in colored],
            [(pid, "/color", False) for pid in pids],
        )
        for command, start in zip(colored, starts, strict=True):
            self.assertGreaterEqual(command["time"], start + 1.0)
        self.ok(self.session("send", "w1", "hi"))
        self.wait_event("w1", "done", 2)
        self.assertEqual(self.ok(self.session("reply", "w1")).stdout, "Hi from w1.\n")
        self.ok(self.session("read", "w1"))
        self.assertEqual(self.commands("w1"), colored)


class OldGenerationTest(Base):
    """FR-36: the new client starts only once the old one's process group is gone."""

    def setUp(self):
        super().setUp()
        self.generations = os.path.join(self.root, "generations.jsonl")
        program = os.path.join(self.root, "generation.py")
        with open(program, "w", encoding="utf-8") as f:
            f.write(GENERATION)
        q = shlex.quote
        self.path = self.first_on_path(
            f"#!/bin/sh\nexec {q(sys.executable)} -I {q(program)} {q(self.generations)} "
            f'{q(fake_claude.__file__)} "$@"\n'
        )
        self.addCleanup(self.kill_generations)

    def kill_generations(self) -> None:
        for gen in logged(self.generations):
            for pid in (gen["pid"], gen["child"]):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    def assert_replaced(self, old: dict, new: dict, sid: str) -> None:
        self.assertEqual(new["overlap"], [])
        self.assertFalse(running(old["pid"]) or running(old["child"]), old)
        self.assertTrue(running(new["pid"]) and running(new["child"]), new)
        self.assertTrue(has_pair(new["argv"], "--resume", sid), new["argv"])

    def test_a_restart_starts_the_new_client_after_the_old_one_and_its_child_exit(self):
        record = self.record(self.start("w1", PATH=self.path))
        self.wait_event("w1", "done")
        (old,) = logged(self.generations)
        self.assertTrue(running(old["pid"]) and running(old["child"]), old)
        self.assertEqual(self.record(self.again("restart", "w1", PATH=self.path)), record)
        old, new = logged(self.generations)
        self.assert_replaced(old, new, record.sid)

    def test_a_resume_after_the_session_is_killed_first_ends_its_surviving_group(self):
        record = self.record(self.start("w1", PATH=self.path))
        self.wait_event("w1", "done")
        (old,) = logged(self.generations)
        self.kill_session("w1")
        (call,) = self.calls("w1")
        live_tmux.wait(lambda: not running(call["pid"]), what="the fake claude's exit on hangup")
        self.assertTrue(running(old["pid"]) and running(old["child"]), old)
        res = self.again("resume", "w1", inside=None, PATH=self.path)
        self.assertEqual(self.record(res), record)
        old, new = logged(self.generations)
        self.assert_replaced(old, new, record.sid)
