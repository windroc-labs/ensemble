"""ensemble-crew over the real layer commands: a private tmux server (session/tests/live_tmux.py)
with manager MANAGER attached, <repo>/bin and the fake clients on PATH, HOME outside the temp
dirs (Codex's rule), and the crew run as a module with only utils and manager importable (FR-3).
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest import mock

from ensemble.manager import Entry, Roster, State
from ensemble.manager.engine.crew import RECOVERY_AGE
from ensemble.utils import Record

from manager.tests.case import PY, REPO, SRC, Case
from session.tests import live_tmux
from session.tests.integration.clients.claude import fake_claude

MANAGER = "mgr"
OTHER = "mgr2"
ROLE = "dummy-tester"
TIMEOUT = 90
CREW = (*PY, "-m", "ensemble.manager.cli")
BIN = os.path.join(REPO, "bin")
# Runs the starter argv, then marks $1 and waits for $2: its Record printed, its crew run waiting.
WRAP = ("sh", "-c", 'e=$1 g=$2; shift 2; "$@"; : > "$e"; until [ -e "$g" ]; do sleep 0.05; done')


def logged(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(a == flag and b == value for a, b in zip(argv, argv[1:], strict=False))


class LiveCrew(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home = os.path.join(self.root, "home")
        self.ensemble = os.path.join(self.home, ".ensemble")
        self.cwd = os.path.join(self.root, "work")
        for path in (self.ensemble, self.cwd):
            os.makedirs(path)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": self.home}))
        self.spies = os.path.join(self.root, "spies")
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.events = self.events_of(MANAGER)

    def events_of(self, manager: str) -> str:
        return os.path.join(self.ensemble, "managers", manager, "events")

    def roster(self, manager: str = MANAGER) -> dict[str, Entry]:
        return Roster(os.path.dirname(self.events_of(manager))).entries()

    def env(self, *, inside: str | None = MANAGER, **extra: str | None) -> dict[str, str]:
        """A caller's in session `inside` (None: outside tmux); an extra None drops its key."""
        env = self.server.env(**(self.server.inside(inside) if inside else {}))
        env["PATH"] = os.pathsep.join(
            (self.spies, BIN, self.server.bin, os.path.dirname(sys.executable), env["PATH"])
        )
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def call(
        self,
        *argv: str,
        inside: str | None = MANAGER,
        stdin: str = "",
        cwd: str | None = None,
        **extra: str | None,
    ) -> subprocess.CompletedProcess:
        with self.poking():
            return subprocess.run(
                argv,
                env=self.env(inside=inside, **extra),
                cwd=cwd or self.cwd,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )

    def crew(self, *args: str, manager: str = MANAGER, **kw) -> subprocess.CompletedProcess:
        return self.call(*CREW, *args, **self.crew_env(manager), **kw)

    def crew_env(self, manager: str = MANAGER) -> dict[str, str]:
        """What a crew call adds to its caller's env: its manager, only utils and manager."""
        crew_path = os.pathsep.join((SRC["utils"], SRC["manager"]))
        return {"ENSEMBLE_MANAGER": manager, "PYTHONPATH": crew_path}

    def session(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call("ensemble-session", *args, **kw)

    def ok(self, result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(
            result.returncode, 0, f"{result.args}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return result

    def record(self, result: subprocess.CompletedProcess) -> Record:
        lines = self.ok(result).stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        return Record.parse(lines[0])

    def scene(self, name: str, steps=None, turns=()) -> dict[str, str]:
        """`name`'s fake claude scenario, as env; its starts log to self.log(name)."""
        path = os.path.join(self.root, f"{name}.claude.json")
        steps = [f"Hi from {name}."] if steps is None else steps
        scenario = {"steps": steps, "turns": list(turns), "log": self.log(name)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**scenario, "commands": self.log(name, "commands")}, f)
        return {fake_claude.ENV: path}

    def log(self, name: str, kind: str = "claude") -> str:
        return os.path.join(self.root, f"{name}.{kind}.jsonl")

    def launch(
        self, name: str, argv, *, steps=None, turns=(), note: str | None = None, **kw
    ) -> subprocess.CompletedProcess:
        """crew run `name` [--note] -- argv, `name`'s fake claude scenario in its env."""
        noted = ("--note", note) if note else ()
        return self.crew("run", name, *noted, "--", *argv, **self.scene(name, steps, turns), **kw)

    def worker(self, name: str, *flags: str, client: str = "claude", **kw):
        """crew run `name` -- ensemble-session start `name`, a fake claude worker in self.cwd."""
        return self.launch(name, self.worker_argv(name, *flags, client=client), **kw)

    def worker_argv(self, name: str, *flags: str, client: str = "claude") -> tuple[str, ...]:
        start = ("ensemble-session", "start", name, "--client", client, "--cwd", self.cwd)
        return (*start, "--prompt", "hi", *flags)

    def orphan(self, name: str, inside: str | None = MANAGER) -> Record:
        """A fake claude worker started by ensemble-session itself, from session `inside`."""
        argv = self.worker_argv(name)[1:]
        return self.record(self.session(*argv, inside=inside, **self.scene(name)))

    def role(self, name: str, *flags: str, mode: str = "tui", **kw):
        """crew run `name` -- ensemble-delegate run … --detach, ROLE over the fake claude."""
        return self.launch(name, self.role_argv(name, *flags, mode=mode), **kw)

    def role_argv(self, name: str, *flags: str, mode: str = "tui") -> tuple[str, ...]:
        """ROLE's detached run, workdir self.workdir(name), no --sid or --events."""
        work = self.workdir(name)
        os.makedirs(work, exist_ok=True)
        run = ("ensemble-delegate", "run", "--role", ROLE, "--input", "Hello.")
        detach = ("--out", os.path.join(work, "out.md"), "--workdir", work, "--mode", mode)
        return (*run, *detach, "--detach", *flags)

    def workdir(self, name: str) -> str:
        return os.path.join(self.root, f"{name}-work")

    def crashed(self, name: str, argv, **scene: str) -> Record:
        """crew run `name` --note keep -- argv, killed after argv's Record; attach RECOVERY_AGE later."""
        emitted, gate = (os.path.join(self.root, f"{name}.{n}") for n in ("emitted", "gate"))
        crew = subprocess.Popen(
            (*CREW, "run", name, "--note", "keep", "--", *WRAP, "wrap", emitted, gate, *argv),
            env=self.env(**self.crew_env(), **scene),
            cwd=self.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(crew.wait)
        self.addCleanup(lambda: open(gate, "a").close())
        with self.poking():
            live_tmux.wait(lambda: os.path.exists(emitted), 60, what=f"{name}'s Record printed")
        crew.send_signal(signal.SIGKILL)
        crew.wait()
        intent = self.roster()[name]
        self.assertEqual((intent.state, intent.record), (State.STARTING, None))
        path = os.path.join(self.ensemble, "managers", MANAGER, "roster.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        old = datetime.now(UTC) - timedelta(seconds=RECOVERY_AGE + 1)
        data["entries"][name]["started"] = old.isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.ok(self.crew("attach"))
        listed = self.ok(self.session("ls", "--here", "--records")).stdout.splitlines()
        (record,) = {r for r in map(Record.parse, listed) if r.sid == intent.sid}
        entries = self.roster()
        self.assertEqual(list(entries), [name])
        self.assertEqual(
            (entries[name].state, entries[name].record, entries[name].note),
            (State.LIVE, record, "keep"),
        )
        (status,) = (
            json.loads(line) for line in self.ok(self.crew("status", name)).stdout.splitlines()
        )
        self.assertEqual((status["name"], status["alive"]), (name, True))
        return record

    def words(self, name: str) -> list[list[str]]:
        return [
            line.split(" ")[2:]
            for line in live_tmux.events(self.events)
            if line.split(" ")[1] == name
        ]

    def killed(self, name: str, record: Record) -> None:
        """Kills tui run `name`'s sessions after its first turn; attach then says gone."""
        harness, tui = record.sessions
        self.wait_event(tui, "done")
        shown = self.server.tmux("display-message", "-p", "-t", f"={harness}:", "#{pane_pid}")
        pid = int(shown.stdout)
        self.kill(harness)
        self.server.tmux(
            "kill-session", "-t", f"={tui}"
        )  # the harness, stopping, may close it first
        self.until(  # kill-session: no exited hook
            lambda: (
                self.gone(tui)
                and ["outcome", "error"] in self.words(harness)
                and not live_tmux.alive(pid)
            ),
            f"{tui} gone, the harness's outcome and exit",
        )
        out = self.ok(self.crew("attach")).stdout.splitlines()
        self.assertIn(f"resume: ensemble-crew resume --manager mgr {name}", out)
        self.assertEqual(self.roster()[name].state, State.GONE)
        self.assertTrue(self.gone(harness, tui))

    def spy(self, command: str) -> str:
        """Puts a `command` before <repo>/bin that logs its argv, then runs the real one; its log."""
        os.makedirs(self.spies, exist_ok=True)
        log = os.path.join(self.root, f"{command}.calls")
        with open(os.path.join(self.spies, command), "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{BIN}/{command}" "$@"\n')
        os.chmod(os.path.join(self.spies, command), 0o755)
        return log

    def wait_event(self, name: str, word: str, count: int = 1, events: str | None = None):
        def found():
            self.poke()
            lines = live_tmux.events(events or self.events)
            hits = [line for line in lines if line.split(" ")[1:3] == [name, word]]
            return lines if len(hits) >= count else None

        return live_tmux.wait(found, TIMEOUT, what=f"{count} x `{name} {word}`")

    def until(self, check, what: str):
        def poked():
            self.poke()
            return check()

        return live_tmux.wait(poked, TIMEOUT, what=what)

    def live(self, *names: str) -> bool:
        return all(self.server.tmux("has-session", "-t", f"={n}").returncode == 0 for n in names)

    def gone(self, *names: str) -> bool:
        return not any(self.live(n) for n in names)

    def kill(self, *names: str) -> None:
        for name in names:
            self.ok(self.server.tmux("kill-session", "-t", f"={name}"))
        self.until(lambda: self.gone(*names), f"{names} gone")

    def poke(self) -> None:
        # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so no pane-died, until
        # another child of the server exits: a no-op job is one
        self.server.tmux("run-shell", "-b", "true")

    @contextmanager
    def poking(self):
        stop = threading.Event()

        def run():
            while not stop.wait(live_tmux.POLL * 4):
                self.poke()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
