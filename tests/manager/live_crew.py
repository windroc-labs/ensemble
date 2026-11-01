"""ensemble-crew over the real commands on a private tmux server."""

import fcntl
import json
import os
import signal
import subprocess
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from unittest import mock

from ensemble.manager import Entry, Roster, State
from ensemble.manager.engine.crew import RECOVERY_AGE
from ensemble.utils import ProcessControl, Record
from tests.delegate import drives, tester
from tests.manager.case import PY, REPO, SRC, Case
from tests.session import live_tmux
from tests.session.live_tmux import MANAGER, logged
from tests.utils.sandbox import scratch

OTHER = "mgr2"
ROLE = tester.ROLE
TIMEOUT = 90
CREW = (*PY, "-m", "ensemble.manager.cli")
BIN = os.path.join(REPO, "bin")
WRAP = ("sh", "-c", 'e=$1 g=$2; shift 2; "$@"; : > "$e"; until [ -e "$g" ]; do sleep 0.05; done')


class Owner:
    """A tui session's harness dir."""

    def __init__(self, ensemble: str, name: str):
        with open(os.path.join(ensemble, "tui", name, "session.json"), encoding="utf-8") as f:
            self.directory = json.load(f)["owner"]
        self.events = os.path.join(self.directory, "events.jsonl")

    def status(self) -> dict:
        with open(os.path.join(self.directory, "harness.json"), encoding="utf-8") as f:
            return json.load(f)

    def alive(self) -> bool:
        fd = os.open(os.path.join(self.directory, "harness.lock"), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(a == flag and b == value for a, b in zip(argv, argv[1:], strict=False))


class LiveCrew(live_tmux.Calls, Case):
    WAIT = TIMEOUT
    EVENT = "{count} x `{name} {word}`"

    def setUp(self):
        self.root = scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(drives.end, self.root)
        self.home = os.path.join(self.root, "home")
        self.ensemble = os.path.join(self.home, ".ensemble")
        self.cwd = os.path.join(self.root, "work")
        for path in (self.ensemble, self.cwd):
            os.makedirs(path)
        tester.configure(self.home)
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

    def bins(self) -> tuple:
        return self.spies, BIN

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
        """A crew call's env: utils and manager only."""
        crew_path = os.pathsep.join((SRC["utils"], SRC["manager"]))
        return {"ENSEMBLE_MANAGER": manager, "PYTHONPATH": crew_path}

    def session(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call("ensemble-session", *args, **kw)

    def ok_message(self, result: subprocess.CompletedProcess) -> str:
        return f"{result.args}\nstdout: {result.stdout}\nstderr: {result.stderr}"

    def launch(
        self, name: str, argv, *, steps=None, turns=(), note: str | None = None, **kw
    ) -> subprocess.CompletedProcess:
        """crew run `name` [--note] -- argv, with `name`'s scenario."""
        noted = ("--note", note) if note else ()
        return self.crew("run", name, *noted, "--", *argv, **self.scene(name, steps, turns), **kw)

    def worker(self, name: str, *flags: str, client: str = "claude", **kw):
        """crew run `name` -- ensemble-session start `name` (fake claude)."""
        return self.launch(name, self.worker_argv(name, *flags, client=client), **kw)

    def worker_argv(self, name: str, *flags: str, client: str = "claude") -> tuple[str, ...]:
        start = ("ensemble-session", "start", name, "--client", client, "--cwd", self.cwd)
        return (*start, "--prompt", "hi", *flags)

    def orphan(self, name: str, inside: str | None = MANAGER) -> Record:
        """A worker ensemble-session starts from `inside`."""
        argv = self.worker_argv(name)[1:]
        return self.record(self.session(*argv, inside=inside, **self.scene(name)))

    def role(self, name: str, *flags: str, mode: str = "tui", **kw):
        return self.launch(name, self.role_argv(name, *flags, mode=mode), **kw)

    def role_argv(self, name: str, *flags: str, mode: str = "tui") -> tuple[str, ...]:
        """ROLE's detached run in self.workdir(name)."""
        work = self.workdir(name)
        os.makedirs(work, exist_ok=True)
        run = ("ensemble-delegate", "run", "--role", ROLE, "--input", "Hello.")
        detach = ("--out", os.path.join(work, "out.md"), "--workdir", work, "--mode", mode)
        return (*run, *detach, "--detach", *flags)

    def workdir(self, name: str) -> str:
        return os.path.join(self.root, f"{name}-work")

    def crashed(self, name: str, argv, **scene: str) -> Record:
        """crew run `name` --note keep -- argv, killed after its Record."""
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

    def drive(self, record: Record) -> ProcessControl:
        with open(os.path.join(record.workdir, "run", "delegate.json"), encoding="utf-8") as f:
            return ProcessControl.attach(json.load(f)["drive"])

    def drive_running(self, record: Record) -> bool:
        return self.drive(record).running()

    def kill_drive(self, record: Record) -> None:
        """SIGKILL to record's drive's group, never this test's."""
        ps = self.ok(
            subprocess.run(
                ["ps", "-A", "-ww", "-o", "pid=,pgid=,args="], capture_output=True, text=True
            )
        )
        tail = f"-m ensemble.delegate.engine.harness {record.workdir}"
        rows = [line.split(None, 2) for line in ps.stdout.splitlines()]
        groups = [int(row[1]) for row in rows if row[2:] and row[2].endswith(tail)]
        self.assertEqual(len(groups), 1, ps.stdout)
        self.assertNotEqual(groups[0], os.getpgrp())
        os.killpg(groups[0], signal.SIGKILL)
        drives.stop(self.drive(record))

    def killed(self, name: str, record: Record) -> None:
        """SIGKILLs run `name`'s drive, harness and client."""
        (tui,) = record.sessions
        self.wait_event(tui, "done")
        self.kill_drive(record)
        live_tmux.crash_owner(self.ensemble, tui)  # its guard then ends the worker
        with suppress(ProcessLookupError):
            os.kill(logged(self.log(name))[-1]["pid"], signal.SIGKILL)
        self.until(lambda: self.gone(tui) and not self.server.pane_of(tui), f"{tui} gone")
        self.assertEqual(self.words(f"{tui}-drive"), [])
        out = self.ok(self.crew("attach")).stdout.splitlines()
        self.assertIn(f"resume: ensemble-crew resume --manager mgr {name}", out)
        self.assertEqual(self.roster()[name].state, State.GONE)
        self.assertFalse(self.drive_running(record))

    def spy(self, command: str) -> str:
        """A `command` before <repo>/bin logging its argv; its log."""
        os.makedirs(self.spies, exist_ok=True)
        log = os.path.join(self.root, f"{command}.calls")
        with open(os.path.join(self.spies, command), "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{BIN}/{command}" "$@"\n')
        os.chmod(os.path.join(self.spies, command), 0o755)
        return log

    def until(self, check, what: str):
        def poked():
            self.poke()
            return check()

        return live_tmux.wait(poked, TIMEOUT, what=what)

    def live(self, *names: str) -> bool:
        """Whether each name is a tmux session or a worker whose guard runs."""
        return all(
            self.server.tmux("has-session", "-t", f"={n}").returncode == 0 or live_tmux.running(n)
            for n in names
        )

    def gone(self, *names: str) -> bool:
        return not any(self.live(n) for n in names)

    def kill(self, *names: str) -> None:
        for name in names:
            live_tmux.crash_owner(self.ensemble, name)  # its guard then ends the worker
        self.until(lambda: self.gone(*names), f"{names} gone")
