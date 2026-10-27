"""ensemble-session as its users run it: bin/ensemble-session in a subprocess (no PYTHONPATH; the
running interpreter's directory supplies python3), a fresh cwd and HOME, a private tmux server
(live_tmux.Server, a control-mode client on MANAGER) and the fake claude on PATH. `module=True` runs
`python3 -P -m ensemble.session.cli` with this tree's roots instead, for a config naming a test
Host (tests.session.fakes.host:FakeHost, not attachable).
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager

from ensemble.session.harness.channel import Channel
from ensemble.utils import Record, Status
from tests.session import live_tmux
from tests.session.case import REPO, ROOTS, Case
from tests.session.integration.clients.claude import fake_claude

COMMAND = os.path.join(REPO, "bin", "ensemble-session")
MANAGER = "mgr"
TIMEOUT = 60
FAKE_HOST = "tests.session.fakes.host:FakeHost"
CLAUDE_HOOKS = {
    "SessionStart",
    "Stop",
    "StopFailure",
    "PermissionRequest",
    "Notification",
    "PostToolUse",
    "UserPromptSubmit",
}
POLICY = ("--allowedTools", "--add-dir", "--dangerously-skip-permissions")


def logged(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


class CliCase(Case):
    ATTACH = True

    def setUp(self):
        parent = os.environ.get("ENSEMBLE_TEST_TMP")
        tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.cwd = os.path.join(self.root, "work")
        os.mkdir(self.cwd)
        self.events = os.path.join(self.root, "events")
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        if self.ATTACH:
            self.server.attach(MANAGER)

    def owner(self, name: str) -> Channel:
        """Worker `name`'s session harness, as its session.json names it."""
        path = os.path.join(self.ensemble, "tui", name, "session.json")
        with open(path, encoding="utf-8") as f:
            return Channel(json.load(f)["owner"])

    def settled(self, channel: Channel) -> dict:
        """Its harness.json once it no longer runs a worker (exited or failed)."""
        return live_tmux.wait(
            lambda: (found := channel.status()).get("state") in ("exited", "failed") and found,
            timeout=40,
            what=f"harness {channel.directory} to leave running",
        )

    def env(self, *, inside: str | None = MANAGER, **extra: str | None) -> dict[str, str]:
        """A caller's in session `inside` (None: outside tmux); an extra None drops its key."""
        env = self.server.env(**(self.server.inside(inside) if inside else {}))
        env["PATH"] = os.pathsep.join(
            (self.server.bin, os.path.dirname(sys.executable), env["PATH"])
        )
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def session(
        self,
        *argv: str,
        stdin: str | None = None,
        inside: str | None = MANAGER,
        cwd: str | None = None,
        module: bool = False,
        **extra: str | None,
    ) -> subprocess.CompletedProcess:
        env = self.env(inside=inside, **extra)
        if module:
            env["PYTHONPATH"] = os.pathsep.join((*ROOTS, REPO))
            command = [sys.executable, "-P", "-m", "ensemble.session.cli", *argv]
        else:
            command = [COMMAND, *argv]
        with self.poking():
            return subprocess.run(
                command,
                env=env,
                cwd=cwd or self.cwd,
                input="" if stdin is None else stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )

    def ok(self, result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(
            result.returncode, 0, f"{shlex.join(result.args)}\n{result.stdout}\n{result.stderr}"
        )
        return result

    def scene(self, name: str, steps=None, turns=(), **more) -> dict[str, str]:
        """Worker `name`'s fake claude scenario, as env."""
        path = os.path.join(self.root, f"{name}.claude.json")
        steps = [f"Hi from {name}."] if steps is None else steps
        scenario = {"steps": steps, "turns": list(turns), "log": self.log(name)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**scenario, "commands": self.log(name, "commands"), **more}, f)
        return {"FAKE_CLAUDE_SCENARIO": path}

    def log(self, name: str, kind: str = "claude") -> str:
        return os.path.join(self.root, f"{name}.{kind}.jsonl")

    def calls(self, name: str) -> list[dict]:
        return logged(self.log(name))

    def commands(self, name: str) -> list[dict]:
        return logged(self.log(name, "commands"))

    def start(
        self, name: str, *args: str, steps=None, turns=(), prompt: str = "hi", scene=None, **extra
    ) -> subprocess.CompletedProcess:
        """A fake claude worker, its events self.events; scene: more scenario keys."""
        found = self.scene(name, steps, turns, **(scene or {}))
        env = {"ENSEMBLE_EVENTS": self.events, **found, **extra}
        return self.session(
            "start",
            name,
            "--client",
            "claude",
            "--cwd",
            self.cwd,
            "--prompt",
            prompt,
            *args,
            **env,
        )

    def record(self, result: subprocess.CompletedProcess) -> Record:
        self.ok(result)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        return Record.parse(lines[0])

    def status(self, *names: str, stdin: str | None = None, **kw) -> list[Status]:
        result = self.ok(self.session("status", *names, stdin=stdin, **kw))
        return [Status.parse(line) for line in result.stdout.splitlines()]

    def wait_event(self, name: str, word: str, count: int = 1, events: str | None = None):
        def found():
            self.poke()
            lines = live_tmux.events(events or self.events)
            hits = [line for line in lines if line.split(" ")[1:3] == [name, word]]
            return lines if len(hits) >= count else None

        return live_tmux.wait(found, what=f"{count} x {name} {word}")

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

    def local_config(self, text: str) -> None:
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def fake_host(self, default: str = "tmux") -> str:
        """Adds Host `fake` (not attachable); its root."""
        root = os.path.join(self.root, "fake-host")
        self.local_config(
            f'[session.hosts]\ndefault = "{default}"\n[session.hosts.fake]\n'
            f'implementation = "{FAKE_HOST}"\nroot = "{root}"\n'
        )
        return root


ENV_DUMP = (
    "import json, os, sys; open(sys.argv[1], 'a')"
    ".write(json.dumps({'argv': sys.argv[2:], 'env': dict(os.environ)}) + '\\n')"
)


class WorkerCase(CliCase):
    def dumping(self) -> str:
        """A `claude` logging argv and env before the fake; its log."""
        path = os.path.join(self.root, "claude.env.jsonl")
        q = shlex.quote
        with open(os.path.join(self.server.bin, "claude"), "w", encoding="utf-8") as f:
            f.write(
                f'#!/bin/sh\n{q(sys.executable)} -I -c {q(ENV_DUMP)} {q(path)} "$@"\n'
                f'exec {q(sys.executable)} -I {q(fake_claude.__file__)} "$@"\n'
            )
        return path

    def claude_argv(self, argv: list[str], sid: str, name: str, *tail: str) -> None:
        """A worker's argv on a non-isolated Host, then `tail`."""
        self.assertEqual(
            argv[:9] + argv[10:],
            [
                "--session-id",
                sid,
                "--name",
                name,
                "--permission-mode",
                "auto",
                "--setting-sources",
                "user,project,local",
                "--settings",
                *tail,
            ],
        )
        settings = json.loads(argv[9])
        self.assertEqual(set(settings), {"enabledPlugins", "hooks"})
        self.assertEqual(settings["enabledPlugins"], {"ensemble@windroc": False})
        self.assertEqual(set(settings["hooks"]), CLAUDE_HOOKS)
        self.assertFalse([word for word in argv if word.startswith(POLICY)], argv)

    def harness_files(self, name: str) -> str:
        """Its session harness's log, status and events, for a failure message."""
        try:
            with open(
                os.path.join(self.ensemble, "tui", name, "session.json"), encoding="utf-8"
            ) as f:
                owner = json.load(f).get("owner")
        except (OSError, ValueError):
            return f"{name}: no session.json"
        found = []
        for file in ("harness.log", "harness.json", "events.jsonl"):
            try:
                with open(os.path.join(owner, file), encoding="utf-8", errors="replace") as f:
                    found.append(f"--- {file}\n{f.read()[-4000:]}")
            except (OSError, TypeError):
                found.append(f"--- {file}: none")
        return "\n".join(found)

    def live(self, name: str) -> bool:
        return self.server.tmux("has-session", "-t", f"={name}").returncode == 0

    def pane(self, name: str, fmt: str) -> str:
        res = self.server.tmux("display-message", "-p", "-t", f"={name}:", fmt)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.removesuffix("\n")

    def words(self) -> list[list[str]]:
        return [line.split(" ")[1:] for line in live_tmux.events(self.events)]
